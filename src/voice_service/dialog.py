"""语音对话引擎：RTC 云端把 ASR 文本打过来之后，我们在这里决定"说什么"。

一轮的完整动作：

    客户说话
      ↓ RTC 云端 ASR（双向流，中间结果不回传给我们，只给最终文本）
      ↓ POST /voice/callback/chat（OpenAI 协议，stream=true）
    ┌────────────────────────────────────────────────────────────┐
    │ ① 客户话语安全检查：要求造假 / 要求承诺 → 直接拒绝并转人工   │
    │ ② RAG 检索（1~3 条，含相关性闸门）                          │
    │ ③ 组 messages：system(=人设+知识，**一条**) + 历史 + 本轮问题 │
    │ ④ 流式生成 → 按句切分 → 逐句过内容安全 → 逐句吐给 TTS        │
    │ ⑤ 整轮结束后再全量复检一次（防跨句红线词）                  │
    │ ⑥ 落库：轮次 + 延迟拆解（RAG/首字/总时长）                  │
    └────────────────────────────────────────────────────────────┘
      ↓ SSE data: {"choices":[{"delta":{"content":"..."}}]}
    RTC 云端 TTS 合成 → 推流回房间 → 客户听到

三个关键工程决策（面试必问）：

**决策 1：没有知识依据就不调模型。**
    检索为空时直接回"这个我得让专员跟您确认"，而不是让模型自由发挥。
    语音场景没有"用户往上翻再读一遍"的机会，编错了就是投诉。
    副作用是首字延迟从 2s 变成 0.1s —— 客户听到的是"秒回"，体验反而更好。

**决策 2：system 只有一条，知识拼在里面。**
    分开写 system + 单独一条"参考资料"消息，会让每次的 token 前缀在
    "参考资料"处才开始稳定；合并成一条、且知识块按 chunk_id 固定排序，
    才能让前缀缓存（Prefix Caching）真正命中，首字延迟从 5s 降到 2s 内。

**决策 3：逐句过审，而不是整轮过审。**
    整轮生成完再审 = 违规内容已经被 TTS 念出去了。所以按句切分，
    每句出口前先过内容安全；命中红线立即换成兜底话术并终止本轮。
    代价是"跨句拼出来的红线词"逐句查不到 —— 所以最后还要再做一次全量复检，
    命中就把整轮替换掉（此时可能已经念出去一半，但至少后面不再继续）。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from agent_platform.llm.base import LLMProvider, Message, estimate_tokens
from agent_platform.logging_config import get_logger
from voice_service.agent_config import clamp_max_tokens
from voice_service.censor import HANDOFF_LINE, Censor
from voice_service.domain import (
    LatencyBreakdown,
    RetrievalResult,
    VoiceScene,
    VoiceTurn,
)
from voice_service.prompt import PromptRegistry
from voice_service.rag import VoiceRetriever

logger = get_logger(__name__)

# 句末标点：中文语音合成按句读最自然，也正好是"可以开始念"的最小单位
SENTENCE_ENDS = "。！？!?；;\n"
# 长句内的次级断点（一句话太长时也要尽早开口，否则 TTS 憋着不说话）
CLAUSE_ENDS = "，,、：:"
MAX_PENDING_CHARS = 42


# =========================================================================== #
# 流式切句
# =========================================================================== #
class SentenceSplitter:
    """把 token 流切成"可以立刻念"的句子。

    为什么不直接把每个 token 送给 TTS：
        TTS 按 token 合成会把"3.2%"念成"三 点 二 百分号"，音调全散。
        按句合成音质最好，但按整段合成又要等全生成完 —— 所以按句切是最优解。
    """

    def __init__(self, *, max_pending: int = MAX_PENDING_CHARS) -> None:
        self.buffer = ""
        self.max_pending = max_pending

    def feed(self, text: str) -> list[str]:
        """喂入增量文本，返回已能成句的片段列表。"""
        self.buffer += text
        out: list[str] = []
        while True:
            idx = self._find_cut()
            if idx < 0:
                break
            sentence = self.buffer[: idx + 1].strip()
            self.buffer = self.buffer[idx + 1 :]
            if sentence:
                out.append(sentence)
        return out

    def _find_cut(self) -> int:
        for i, ch in enumerate(self.buffer):
            if ch in SENTENCE_ENDS:
                return i
        # 没有句末标点但已经很长了：退一步在逗号处切（至少能开口说话）
        if len(self.buffer) >= self.max_pending:
            for i in range(len(self.buffer) - 1, -1, -1):
                if self.buffer[i] in CLAUSE_ENDS:
                    return i
            return self.max_pending - 1
        return -1

    def flush(self) -> str:
        """收尾：把剩下的半句也吐出来（不能说一半就结束）。"""
        rest, self.buffer = self.buffer.strip(), ""
        return rest


# =========================================================================== #
# 流式事件
# =========================================================================== #
@dataclass
class StreamEvent:
    """一个流式事件：要么是一段文字，要么是"本轮结束 + 元数据"。"""

    delta: str = ""
    done: bool = False
    turn: VoiceTurn | None = None
    replaced: bool = False
    """True 表示本轮输出被内容安全替换过（前端/审计要看得见）。"""


@dataclass
class CancelToken:
    """打断令牌。

    用户在 AI 说话时插话，RTC 云端会自己停掉 TTS；
    但我们这边的生成循环还在跑（还在花钱、还在占线程），
    所以 API 层检测到客户端断开 / 收到打断指令时，把它置位，
    生成循环在**下一个句子边界**立刻退出。
    """

    cancelled: bool = False
    reason: str = ""

    def cancel(self, reason: str = "") -> None:
        self.cancelled = True
        self.reason = reason


# =========================================================================== #
# 应答合成
# =========================================================================== #
class ExtractiveComposer:
    """离线应答：直接引用政策条款，不做自由生成。

    为什么离线也要能"像样地回答"：
        演示和 CI 都跑在 echo 模型上，如果这时 AI 只会说
        "[离线模式] 已收到请求"，那语音链路根本没法验证（也看不出好坏）。
        抽取式应答把命中的条款原文组织成口语，既天然无幻觉，又能完整跑通
        "流式 → 切句 → 内容安全 → TTS 分片"这条链路。

    接真实模型后由 ModelComposer 接管，两者输出形态一致（都是文本流）。
    """

    # 结尾推进语：语音场景必须每轮都推进到下一步，不能停在"回答完了"
    CTA = "具体以银行审批为准，我加您微信把细则发过去。"
    # 语音总长度硬上限：约 25 秒，超过这个长度客户一定开始走神
    MAX_CHARS = 150
    # 单条片段长度上限（两条拼起来才不会超总预算）
    SNIPPET_CHARS = 55

    def compose(self, scene: VoiceScene, retrieval: RetrievalResult, question: str) -> Iterator[str]:
        # 注意排序口径与 prompt 不同：**prompt 里按 chunk_id 排（为了前缀缓存），
        # 这里按 score 排（为了回答质量）**。两者服务的目标不同，不能混为一谈。
        ranked = sorted(retrieval.chunks, key=lambda c: c.score, reverse=True)
        snippets: list[str] = []
        for chunk in ranked:
            snippet = self._first_sentence(self._clean(chunk.text), self.SNIPPET_CHARS)
            if snippet and snippet not in snippets:
                snippets.append(snippet)
            if len(snippets) >= 2:
                break

        if not snippets:
            yield HANDOFF_LINE
            return

        pieces = ["政策上写的是："]
        pieces.append(snippets[0])
        if len(snippets) > 1:
            pieces.append("另外，")
            pieces.append(snippets[1])
        pieces.append(self.CTA)

        budget = self.MAX_CHARS
        for piece in pieces:
            if budget <= 0:
                break
            text = piece[:budget]
            budget -= len(text)
            yield text

    @staticmethod
    def _clean(text: str) -> str:
        """去掉不适合念给客户听的内部信息（产品编号、来源文件等）。"""
        import re

        cleaned = re.sub(r"（[^）]*(产品编号|演示银行|来源)[^）]*）", "", text or "")
        cleaned = re.sub(r"\([^)]*(product|source)[^)]*\)", "", cleaned, flags=re.I)
        return " ".join(cleaned.split())

    @staticmethod
    def _first_sentence(text: str, limit: int = 70) -> str:
        clean = " ".join((text or "").split())
        for i, ch in enumerate(clean):
            if ch in SENTENCE_ENDS:
                return clean[: i + 1] if i + 1 <= limit else clean[:limit] + "。"
        return clean[:limit] + ("。" if clean else "")


class ModelComposer:
    """在线应答：把 prompt 交给模型，流式读取。"""

    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm

    def compose(
        self,
        messages: list[Message],
        *,
        temperature: float,
        max_tokens: int,
    ) -> Iterator[str]:
        yield from self.llm.stream_complete(
            messages, temperature=temperature, max_tokens=max_tokens
        )


# =========================================================================== #
# 对话引擎
# =========================================================================== #
@dataclass
class DialogDeps:
    llm: LLMProvider
    retriever: VoiceRetriever
    prompts: PromptRegistry
    censor: Censor
    history_turns: int = 6
    max_tokens_cap: int = 300
    prefer_model: bool = True
    """True 时只要不是 echo 就走模型；False 强制走抽取式（评测对比用）。"""


@dataclass
class TurnRequest:
    scene: VoiceScene
    user_text: str
    session_id: str = ""
    round_id: int = 1
    history: list[VoiceTurn] = field(default_factory=list)
    extra: str = ""
    cancel: CancelToken | None = None
    prompt_version: str = ""
    """指定提示词版本（评测/灰度用）；留空则取 active 版本。"""


class VoiceDialogEngine:
    """语音对话引擎（无状态，可并发；状态都在 session store 里）。"""

    def __init__(self, deps: DialogDeps) -> None:
        self.deps = deps
        self.extractive = ExtractiveComposer()
        self.model = ModelComposer(deps.llm)

    # ------------------------------------------------------------------ #
    @property
    def _use_model(self) -> bool:
        """判断是否走真实模型。

        注意 `llm.name` 的判定：项目六的网关在底层是 echo 时会把 name 透传成
        "echo"（见 gateway.py），否则离线环境会误判成"接了真实模型"，
        导致演示时突然冒出一堆占位话术。
        """
        if not self.deps.prefer_model:
            return False
        return not str(getattr(self.deps.llm, "name", "")).startswith("echo")

    # ------------------------------------------------------------------ #
    def stream_turn(self, req: TurnRequest) -> Iterator[StreamEvent]:
        """跑一轮对话，流式吐事件。"""
        started = time.perf_counter()
        scene = req.scene
        question = (req.user_text or "").strip()

        # ---- ① 客户话语安全：要求造假/要求承诺 → 明确拒绝 + 转人工 ----
        user_verdict = self.deps.censor.check_user_text(question)
        if user_verdict.blocked:
            logger.warning("客户提出高风险诉求：%s", question[:60])
            answer = user_verdict.safe_reply or HANDOFF_LINE
            turn = self._finish(
                req,
                answer=answer,
                refused=True,
                needs_human=True,
                citations=[],
                prompt_version="",
                latency=LatencyBreakdown(total_ms=(time.perf_counter() - started) * 1000, chunks=1),
            )
            yield StreamEvent(delta=answer)
            yield StreamEvent(done=True, turn=turn)
            return

        # ---- ② RAG ----
        retrieval = self.deps.retriever.retrieve(
            question, limit=scene.knowledge_limit, tags=scene.knowledge_tags
        )
        rag_ms = retrieval.elapsed_ms

        # ---- ③ 无依据：不调模型，直接转人工（省时且不失真）----
        if not retrieval.hit:
            answer = self._handoff_answer(question)
            turn = self._finish(
                req,
                answer=answer,
                refused=True,
                needs_human=True,
                citations=[],
                prompt_version="",
                latency=LatencyBreakdown(
                    rag_ms=rag_ms,
                    total_ms=(time.perf_counter() - started) * 1000,
                    chunks=1,
                ),
            )
            logger.info("语音拒答（无知识依据）：%s", question[:40])
            yield StreamEvent(delta=answer)
            yield StreamEvent(done=True, turn=turn)
            return

        # ---- ④ 组 messages ----
        prompt = self.deps.prompts.get(scene.prompt_key, req.prompt_version)
        system_prompt = prompt.render(knowledge_block=retrieval.context_block, extra=req.extra)
        max_tokens = clamp_max_tokens(min(scene.max_tokens or prompt.max_tokens, self.deps.max_tokens_cap))
        temperature = scene.temperature if scene.temperature is not None else prompt.temperature
        messages = self._build_messages(system_prompt, req.history, question)

        # ---- ⑤ 流式生成 + 逐句过审 ----
        splitter = SentenceSplitter()
        pieces: list[str] = []
        emitted: list[str] = []
        ttft_ms = 0.0
        blocked = False

        for text in self._iterate_answer(scene, retrieval, question, messages, temperature, max_tokens):
            if req.cancel and req.cancel.cancelled:
                logger.info("本轮被打断：%s", req.cancel.reason or "用户插话")
                break
            if not text:
                continue
            if ttft_ms <= 0.0:
                ttft_ms = (time.perf_counter() - started) * 1000 - rag_ms
            pieces.append(text)
            for sentence in splitter.feed(text):
                verdict = self.deps.censor.check(sentence)
                if verdict.blocked:
                    # 命中红线：这一句不念，改成兜底话术，并且不再继续生成
                    safe = verdict.safe_reply or HANDOFF_LINE
                    emitted.append(safe)
                    blocked = True
                    yield StreamEvent(delta=safe, replaced=True)
                    break
                emitted.append(verdict.sanitized_text)
                yield StreamEvent(delta=verdict.sanitized_text)
            if blocked:
                break

        if not blocked:
            tail = splitter.flush()
            if tail:
                verdict = self.deps.censor.check(tail)
                if verdict.blocked:
                    safe = verdict.safe_reply or HANDOFF_LINE
                    emitted.append(safe)
                    blocked = True
                    yield StreamEvent(delta=safe, replaced=True)
                else:
                    emitted.append(verdict.sanitized_text)
                    yield StreamEvent(delta=verdict.sanitized_text)

        answer = "".join(emitted).strip()
        if not answer:
            answer = HANDOFF_LINE

        # ---- ⑥ 整轮全量复检（防跨句拼出来的红线词）----
        final_verdict = self.deps.censor.check(answer)
        needs_human = blocked or final_verdict.needs_human
        if final_verdict.blocked and not blocked:
            answer = final_verdict.safe_reply or HANDOFF_LINE
            blocked = True
            logger.warning("整轮复检命中红线，整轮替换：%s", final_verdict.to_text())

        interrupted = bool(req.cancel and req.cancel.cancelled)
        turn = self._finish(
            req,
            answer=answer,
            refused=False,
            needs_human=needs_human,
            citations=retrieval.citations,
            prompt_version=f"{prompt.key}@{prompt.version}",
            latency=LatencyBreakdown(
                rag_ms=rag_ms,
                llm_ttft_ms=max(0.0, ttft_ms),
                llm_total_ms=max(0.0, (time.perf_counter() - started) * 1000 - rag_ms),
                total_ms=(time.perf_counter() - started) * 1000,
                chunks=len(emitted),
            ),
            interrupted=interrupted,
        )
        if blocked:
            turn.answer_text = answer
        logger.info(
            "语音轮次完成 scene=%s 首字=%.0fms 总=%.0fms 引用=%d 替换=%s",
            scene.scene_id,
            turn.latency.llm_ttft_ms,
            turn.latency.total_ms,
            len(turn.citations),
            blocked,
        )
        yield StreamEvent(done=True, turn=turn, replaced=blocked)

    # ------------------------------------------------------------------ #
    def ask(self, req: TurnRequest) -> VoiceTurn:
        """非流式入口（文本模拟一轮，用于 Swagger 调试与自动评测）。"""
        turn: VoiceTurn | None = None
        for event in self.stream_turn(req):
            if event.done:
                turn = event.turn
        if turn is None:  # pragma: no cover - stream_turn 保证会产出 done
            raise RuntimeError("对话引擎未产出结果")
        return turn

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _iterate_answer(
        self,
        scene: VoiceScene,
        retrieval: RetrievalResult,
        question: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
    ) -> Iterator[str]:
        """产出应答文本流：离线走抽取式，在线走模型。"""
        if self._use_model:
            yield from self.model.compose(messages, temperature=temperature, max_tokens=max_tokens)
        else:
            yield from self.extractive.compose(scene, retrieval, question)

    def _build_messages(
        self, system_prompt: str, history: list[VoiceTurn], question: str
    ) -> list[Message]:
        """组装 messages。

        顺序：system（含知识）→ 最近 N 轮历史 → 本轮问题。
        历史里**只放文本**，不放引用/延迟 —— 那些对模型没用，纯占 token。
        """
        messages = [Message(role="system", content=system_prompt)]
        recent = history[-self.deps.history_turns :] if self.deps.history_turns > 0 else []
        for turn in recent:
            if turn.user_text:
                messages.append(Message(role="user", content=turn.user_text))
            if turn.answer_text:
                messages.append(Message(role="assistant", content=turn.answer_text))
        messages.append(Message(role="user", content=question))
        return messages

    def _handoff_answer(self, question: str) -> str:
        """无依据时的转人工话术。

        不写"抱歉我不知道"—— 客户会觉得这家公司不专业。
        写法是"先承认这是个需要确认的具体问题，再给出确定的下一步"。
        """
        return (
            "这个我得让专员跟您确认一下，不同银行口径不太一样，我不想说错。"
            "您留个联系方式，我让专员十分钟内回您。"
            + (HANDOFF_LINE if question else "")
        )

    def _finish(
        self,
        req: TurnRequest,
        *,
        answer: str,
        refused: bool,
        needs_human: bool,
        citations: list[str],
        prompt_version: str,
        latency: LatencyBreakdown,
        interrupted: bool = False,
    ) -> VoiceTurn:
        return VoiceTurn(
            round_id=req.round_id,
            user_text=req.user_text,
            answer_text=answer,
            refused=refused,
            needs_human=needs_human,
            interrupted=interrupted,
            citations=citations,
            prompt_version=prompt_version,
            latency=latency,
            at=int(time.time()),
        )

    # ------------------------------------------------------------------ #
    def estimate_prompt_tokens(self, system_prompt: str, history: list[VoiceTurn], question: str) -> int:
        """估算本轮 prompt token（用于成本与上下文预算告警）。"""
        messages = self._build_messages(system_prompt, history, question)
        return sum(estimate_tokens(m.content) for m in messages)
