"""对话切块器（把消息流变成"可审核的对话块"）。

为什么必须切块，而不是整段会话直接入库：
    · 一次微信会话可能横跨几天，混着好几个话题；
    · 训练/检索要的是"一个完整话题"，不是"一段聊天流水"。

三件确定性处理：
    ① **时间窗切分**：相邻消息间隔 > 5 分钟就认为换了个话题（可配置）；
    ② **同角色合并**：连续多条同一角色合并成一段（微信里常见"连发三条"）；
    ③ **去重**：同块内重复内容丢弃；跨块重复（转发/刷屏）整块标记 duplicate。

**块 id 由内容派生**（`sha1(session|start_ts|首句)`），不是随机 uuid ——
同一批原始数据重复清洗会得到同一批 block_id，暂存区因此天然幂等：
重跑不产生重复待审条目，评审结论也不会被冲掉。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from data_platform.domain import ChatSession, ConversationBlock, RawMessage
from data_platform.filters import ConversationFilter
from data_platform.masking import MaskingEngine


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class ConversationBuilder:
    """消息 → 对话块的确定性流水线。"""

    def __init__(self, *, window_minutes: float = 5.0, masker: MaskingEngine,
                 conv_filter: ConversationFilter) -> None:
        self.window_seconds = max(window_minutes, 0.1) * 60
        self.masker = masker
        self.filter = conv_filter

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def build(
        self,
        session: ChatSession,
        *,
        seen_signatures: set[str] | None = None,
    ) -> list[ConversationBlock]:
        """清洗一个会话，返回对话块列表（已脱敏、已分类、已去重）。"""
        seen = seen_signatures if seen_signatures is not None else set()
        messages = self.sort_messages(session.messages)
        blocks: list[ConversationBlock] = []

        for window in self.split_by_window(messages):
            merged = self.merge_roles(window)
            merged = self.dedupe_within(merged)
            if not merged:
                continue

            # 脱敏在"成块"之前完成：块里存的永远是脱敏后的文本，
            # 避免下游任何路径拿到原文（而不是靠"记得在导出时脱敏"）。
            masked_msgs, findings, price_hits = self.masker.mask_messages(merged, layer="clean")
            signature = self.signature(masked_msgs)
            duplicate = signature in seen
            if not duplicate:
                seen.add(signature)

            block = self.filter.build_block(
                block_id=self.block_id(session.session_id, masked_msgs),
                session_id=session.session_id,
                customer_id=session.customer_id,
                customer_name=session.customer_name,
                messages=masked_msgs,
                started_at=masked_msgs[0].ts,
                ended_at=masked_msgs[-1].ts,
                duplicate=duplicate,
                price_hits=price_hits,
                findings=findings,
            )
            blocks.append(block)
        return blocks

    # ------------------------------------------------------------------ #
    # 三步处理
    # ------------------------------------------------------------------ #
    @staticmethod
    def sort_messages(messages: Iterable[RawMessage]) -> list[RawMessage]:
        """按时间排序，丢掉空内容消息（媒体占位保留，交给分类器判断）。"""
        msgs = [m for m in messages if (m.content or "").strip()]
        return sorted(msgs, key=lambda m: (m.ts, m.msg_id))

    def split_by_window(self, messages: Sequence[RawMessage]) -> list[list[RawMessage]]:
        """按时间窗切分：间隔超过 window_seconds 就断开。"""
        if not messages:
            return []
        windows: list[list[RawMessage]] = [[messages[0]]]
        for prev, cur in zip(messages, messages[1:], strict=False):
            gap = cur.ts - prev.ts
            if gap > self.window_seconds:
                windows.append([cur])
            else:
                windows[-1].append(cur)
        return windows

    @staticmethod
    def merge_roles(messages: Sequence[RawMessage]) -> list[RawMessage]:
        """合并连续同角色消息。

        合并后保留第一条的 ts 和 sender，内容用换行连接 ——
        这样"连发三条报价"变成一段完整表述，模型读起来才连贯。
        """
        merged: list[RawMessage] = []
        for msg in messages:
            if merged and merged[-1].role == msg.role:
                prev = merged[-1]
                merged[-1] = prev.model_copy(update={
                    "content": f"{prev.content}\n{msg.content}",
                    "ts": msg.ts,
                    "msg_id": f"{prev.msg_id}+{msg.msg_id}",
                })
            else:
                merged.append(msg)
        return merged

    @staticmethod
    def dedupe_within(messages: Sequence[RawMessage]) -> list[RawMessage]:
        """块内去重：归一化（去空白、小写）后完全相同的只留第一条。"""
        seen: set[str] = set()
        out: list[RawMessage] = []
        for msg in messages:
            key = "".join(msg.content.split()).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(msg)
        return out

    # ------------------------------------------------------------------ #
    # 标识
    # ------------------------------------------------------------------ #
    @staticmethod
    def block_id(session_id: str, messages: Sequence[RawMessage]) -> str:
        """内容派生的稳定 id（同数据重跑 → 同 id）。"""
        if not messages:
            return f"blk_{_sha1(session_id)[:12]}"
        head = "".join(messages[0].content.split())[:40]
        raw = f"{session_id}|{messages[0].ts:.0f}|{head}"
        return f"blk_{_sha1(raw)[:12]}"

    @staticmethod
    def signature(messages: Sequence[RawMessage]) -> str:
        """整块内容签名（跨块去重用）。"""
        text = "\n".join(f"{m.role}:{''.join(m.content.split())}" for m in messages)
        return _sha1(text)
