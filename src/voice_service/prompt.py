"""提示词注册表（Prompt Registry）：版本化 + 可灰度 + 可回滚。

为什么语音客服必须做提示词版本管理：
    改一句"回答不超过 80 字"，可能让拒答率从 8% 涨到 20%（模型开始乱猜），
    也可能让首字延迟从 2s 变成 5s（模型被要求思考更多）。
    这种改动必须能回答三个问题：**改了什么、上线了没有、上线后变好还是变坏**。
    所以这里做三件事：
        ① 每次保存 = 一个新版本（不覆盖历史）；
        ② 只有一个 active 版本对外生效，切换是显式的；
        ③ 配合 evaluation.py 的 Golden Set，切版本前先跑回归。

语音场景的提示词和文本场景差别很大，核心就三条（写在 prompt 里不是"话术偏好"，
是**物理约束**）：
    * 短 —— 人说 4-5 字/秒，超过 1 分钟用户必打断；
    * 口语 —— 列表/编号/Markdown 会被 TTS 逐字念出来（"第一点 冒号"很难听）；
    * 不承诺 —— 助贷的红线，任何"保证/百分百"都是违规。

还有一个隐蔽的工程点：**知识块必须放在固定位置、按固定顺序拼接**。
    LLM 服务商的 Prefix Caching 只对"完全相同的前缀"命中。
    如果知识片段按相似度排序，同一句话今天拼出来是 A-B、明天是 B-A，
    前缀就变了，缓存全失效，首字延迟直接退回 5 秒以上。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agent_platform.logging_config import get_logger
from voice_service.agent_config import clamp_max_tokens
from voice_service.domain import KnowledgeChunk, PromptVersion

logger = get_logger(__name__)

# 知识块的边界标记：让模型清楚"哪些是资料、哪些是指令"，
# 也能在日志里一眼看出这段 prompt 有没有带上知识（防止 RAG 静默失效）。
KNOWLEDGE_BEGIN = "【参考知识库开始】"
KNOWLEDGE_END = "【参考知识库结束】"


def build_knowledge_block(chunks: list[KnowledgeChunk], *, max_chars: int = 900) -> str:
    """把检索片段拼成知识块（**按 chunk_id 升序**，不按相似度）。

    ORDER BY chunk_id 而不是 score 的三个理由：
        1. 前缀缓存要求稳定 —— 相似度排序在分数接近时会抖动；
        2. 可复现 —— 同一个问题今天和明天的 prompt 完全一致，评测才有意义；
        3. 可追溯 —— 抓到一条 badcase，能根据 chunk_id 精确定位到政策文件哪一段。
    """
    if not chunks:
        return ""
    lines = [KNOWLEDGE_BEGIN]
    used = 0
    for idx, chunk in enumerate(sorted(chunks, key=lambda c: c.chunk_id), start=1):
        text = " ".join(chunk.text.split())
        remain = max_chars - used
        if remain <= 40:  # 剩得太少就别塞半句话了，宁可少一条
            break
        snippet = text[:remain]
        lines.append(f"{idx}. {snippet}")
        used += len(snippet)
    lines.append(KNOWLEDGE_END)
    return "\n".join(lines)


# =========================================================================== #
# 内置提示词（首次启动时写盘，之后以文件为准）
# =========================================================================== #
_COMMON_RULES = """\
【说话方式】
- 你在打电话，不是发微信。用短句、口语，一条回复控制在 80 字以内、最多 3 句。
- 不要用列表、编号、星号、Markdown 或表情符号，TTS 会把符号念出来。
- 不要重复客户的话，不要"您好，感谢您的来电"这类废话开场。

【底线】
- 只依据下方【参考知识库】回答。知识库里没有的，直接说"这个我得让专员跟您确认"。
- 绝对不做任何结果承诺：不说保证、百分百、包过、必下、无视征信。
  批不批、批多少，一律说"以银行最终审批为准"。
- 不索要验证码、密码、完整身份证号或银行卡号；需要核验就引导线下或官方渠道。
- 不诱导以贷养贷、不引导同时申请多家。客户提这类想法，先提示风险。
- 不评价同行，不承诺替客户"包装资料"。"""

DEFAULT_PROMPTS: dict[str, dict[str, Any]] = {
    "consultant": {
        "system_prompt": (
            "你是助贷公司的语音接待顾问，正在和客户通电话。你的任务是快速判断客户资质、"
            "给出方向性建议、并推进到下一步（加微信或约到店）。\n\n"
            + _COMMON_RULES
            + "\n\n【参考知识库】\n{{knowledge}}\n\n"
            "【补充信息】\n{{extra}}"
        ),
        "temperature": 0.3,
        "max_tokens": 220,
        "note": "v1 通用接待：先问需求与资质，再给方向，最后推进下一步",
    },
    "amount": {
        "system_prompt": (
            "你是助贷公司的额度与资质初判顾问，正在和客户通电话。"
            "客户最关心'我能贷多少'，但额度只能给区间、不能给承诺。\n\n"
            + _COMMON_RULES
            + "\n\n【额度口径】\n"
            "- 先说影响额度的三个主要因素（公积金/社保缴存、负债与查询、房产或保单）。\n"
            "- 需要给数字时只给区间，并且紧跟一句'最终以银行审批为准'。\n"
            "- 客户追问'到底能批多少'时，回答：需要先看征信和缴存明细才能判断。\n\n"
            "【参考知识库】\n{{knowledge}}\n\n【补充信息】\n{{extra}}"
        ),
        "temperature": 0.2,
        "max_tokens": 200,
        "note": "v1 额度初判：只给区间 + 必带免责，禁止承诺具体额度",
    },
    "docs": {
        "system_prompt": (
            "你是助贷公司的资料清单顾问，正在和客户通电话。客户已经决定办理，"
            "你要把需要准备的资料说清楚，并提醒注意事项。\n\n"
            + _COMMON_RULES
            + "\n\n【资料口径】\n"
            "- 一次最多说 3 样资料，多了记不住；剩下的说'我发微信给您'. \n"
            "- 必须强调资料要真实，不能造假。\n\n"
            "【参考知识库】\n{{knowledge}}\n\n【补充信息】\n{{extra}}"
        ),
        "temperature": 0.2,
        "max_tokens": 200,
        "note": "v1 资料清单：一次最多三样，强调真实性",
    },
}


# =========================================================================== #
# 注册表
# =========================================================================== #
class PromptRegistry:
    """提示词版本注册表（JSON 持久化，进程内缓存）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._versions: dict[str, list[PromptVersion]] = {}

    # ------------------------------------------------------------------ #
    # 存储
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        """从磁盘加载；不存在则用内置提示词初始化。"""
        if not self.path.exists():
            self._versions = {key: [self._builtin(key, spec)] for key, spec in DEFAULT_PROMPTS.items()}
            self.save()
            logger.info("提示词库初始化：%d 个键（内置 v1）", len(self._versions))
            return

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        loaded: dict[str, list[PromptVersion]] = {}
        for key, items in raw.items():
            loaded[key] = [PromptVersion(**item) for item in items]
        # 内置键缺失时补上（新增场景不需要手工改 json）
        for key, spec in DEFAULT_PROMPTS.items():
            if key not in loaded:
                loaded[key] = [self._builtin(key, spec)]
        self._versions = loaded
        logger.info("提示词库加载：%s", {k: len(v) for k, v in loaded.items()})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {key: [v.model_dump(mode="json") for v in items] for key, items in self._versions.items()}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _builtin(key: str, spec: dict[str, Any]) -> PromptVersion:
        return PromptVersion(
            key=key,
            version="v1",
            system_prompt=spec["system_prompt"],
            status="active",
            note=spec.get("note", "内置版本"),
            created_at=int(time.time()),
            temperature=float(spec.get("temperature", 0.3)),
            max_tokens=clamp_max_tokens(int(spec.get("max_tokens", 220))),
        )

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def keys(self) -> list[str]:
        return sorted(self._versions)

    def versions(self, key: str) -> list[PromptVersion]:
        return list(self._versions.get(key, []))

    def get(self, key: str, version: str = "") -> PromptVersion:
        """取指定版本；version 为空时取 active 版本。"""
        items = self._versions.get(key) or []
        if not items:
            raise KeyError(f"提示词不存在：{key}")
        if version:
            for item in items:
                if item.version == version:
                    return item
            raise KeyError(f"提示词 {key} 没有版本 {version}")
        for item in items:
            if item.status == "active":
                return item
        return items[-1]

    # ------------------------------------------------------------------ #
    # 变更
    # ------------------------------------------------------------------ #
    def add(
        self,
        key: str,
        system_prompt: str,
        *,
        note: str = "",
        temperature: float = 0.3,
        max_tokens: int = 220,
        activate: bool = False,
    ) -> PromptVersion:
        """新增一个候选版本（默认不生效，需要显式 activate）。"""
        items = self._versions.setdefault(key, [])
        version = f"v{len(items) + 1}"
        prompt = PromptVersion(
            key=key,
            version=version,
            system_prompt=system_prompt,
            status="candidate",
            note=note,
            created_at=int(time.time()),
            temperature=temperature,
            max_tokens=clamp_max_tokens(max_tokens),
        )
        items.append(prompt)
        if activate:
            self._set_active(key, version)
        self.save()
        logger.info("提示词新增：%s@%s（activate=%s）", key, version, activate)
        return self.get(key, version)

    def activate(self, key: str, version: str) -> PromptVersion:
        """切到指定版本（旧版本自动转 archived，保留可回滚）。"""
        self.get(key, version)  # 先校验存在
        self._set_active(key, version)
        self.save()
        logger.info("提示词切换：%s → %s", key, version)
        return self.get(key, version)

    def _set_active(self, key: str, version: str) -> None:
        for item in self._versions.get(key, []):
            if item.version == version:
                item.status = "active"
            elif item.status == "active":
                item.status = "archived"

    def stats(self) -> dict[str, Any]:
        return {
            "keys": len(self._versions),
            "versions": sum(len(v) for v in self._versions.values()),
            "active": {k: self.get(k).version for k in self._versions},
        }
