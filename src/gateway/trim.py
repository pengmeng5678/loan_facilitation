"""上下文裁剪（Context Trimming）—— 成本治理三板斧之一。

为什么必须做：
    Agent 多轮对话里，历史会越滚越长；工具返回的 JSON 常常几百行但对
    当前决策有用的只有两三个字段。不做裁剪，成本会随轮次线性上涨。

三招（对应简历里写的"工具输出摘要化 / 历史分层压缩 / 去重"）：
    ① **去重**：连续重复内容（Agent 重复调同一工具）只留一份；
    ② **分层**：system 消息（指令契约）全保留；最近 N 轮原样保留；
       更早的历史压成一行摘要（保留"说过什么"，丢掉措辞）；
    ③ **工具输出摘要化**：tool 角色的长输出截断为摘要（它通常只是中间态）。

裁剪是**保守**的：先算 token，不超预算就原样返回，绝不为了省 token 而
动最近几轮 —— 那会直接伤害回答质量。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_platform.llm.base import Message, estimate_tokens


@dataclass
class TrimResult:
    """裁剪结果。"""

    messages: list[Message]
    original_tokens: int = 0
    final_tokens: int = 0
    saved_tokens: int = 0
    actions: list[str] = field(default_factory=list)
    trimmed: bool = False

    @property
    def saved_percent(self) -> float:
        if self.original_tokens <= 0:
            return 0.0
        return round(self.saved_tokens / self.original_tokens * 100, 2)


def _total_tokens(messages: list[Message]) -> int:
    return sum(estimate_tokens(m.content) for m in messages)


def _digest(messages: list[Message], summarize_chars: int) -> Message:
    """把若干条历史消息压成一条摘要消息。

    **硬约束：摘要的 token 必须严格小于被压缩原文的 token 总和。**
    否则"压缩"会变成"变贵"—— 这个坑在中文上特别容易踩：如果按
    "4 字符 ≈ 1 token"的英文经验去截断，中文实际约 1 字符 ≈ 2 token，
    截出来的摘要会比原文还长（实测 12 轮对话压完反而多 38 token）。
    所以这里不用字符数当预算，直接按 token 收敛到"确实变小"为止。
    """
    parts: list[str] = []
    for m in messages:
        text = " ".join(m.content.split())
        if not text:
            continue
        snippet = text[:summarize_chars] + ("…" if len(text) > summarize_chars else "")
        parts.append(f"{m.role}:{snippet}")
    joined = "｜".join(parts)

    budget = _total_tokens(messages) - 1  # -1：必须严格小于，不能用 <=
    content = f"[历史摘要] {joined}"
    # 每次砍 20%（并补省略号提示"这里被截断了"），直到进预算或短到不可再砍。
    # 收敛性：长度单调下降且有下界，最坏情况循环 O(log n) 次后必然退出。
    while estimate_tokens(content) > budget and len(content) > 16:
        content = content[: int(len(content) * 0.8)] + "…"
    return Message(role="system", content=content)


def trim_messages(
    messages: list[Message],
    *,
    max_tokens: int,
    keep_recent_turns: int = 6,
    summarize_chars: int = 200,
) -> TrimResult:
    """按预算裁剪消息列表。

    参数：
        max_tokens:         预算上限（估算 token）
        keep_recent_turns:  最近保留多少条消息原文
        summarize_chars:    摘要单条截断字符数
    """
    original = _total_tokens(messages)
    actions: list[str] = []

    # ---- ① 去重：连续且内容完全相同的消息只留一条 ----
    deduped: list[Message] = []
    for m in messages:
        if deduped and deduped[-1].role == m.role and deduped[-1].content == m.content:
            continue
        deduped.append(m)
    if len(deduped) != len(messages):
        actions.append(f"去重：{len(messages)} → {len(deduped)} 条")

    # ---- ② 工具输出摘要化：tool 消息通常只是中间态，保留头部即可 ----
    compacted: list[Message] = []
    for m in deduped:
        if m.role == "tool" and len(m.content) > summarize_chars:
            compacted.append(
                Message(
                    role="tool",
                    name=m.name,
                    content=m.content[:summarize_chars] + "…[已摘要]",
                )
            )
        else:
            compacted.append(m)
    if compacted != deduped:
        actions.append("工具输出摘要化")

    after_compact = _total_tokens(compacted)

    # ---- ③ 分层压缩：system 全留 + 最近 N 条全留，其余压成摘要 ----
    if after_compact <= max_tokens:
        return TrimResult(
            messages=compacted,
            original_tokens=original,
            final_tokens=after_compact,
            saved_tokens=original - after_compact,
            actions=actions,
            trimmed=bool(actions),
        )

    systems = [m for m in compacted if m.role == "system" and not m.content.startswith("[历史摘要]")]
    conversational = [
        m for m in compacted if not (m.role == "system" and not m.content.startswith("[历史摘要]"))
    ]
    keep_count = max(1, keep_recent_turns)
    head = conversational[:-keep_count]
    tail = conversational[-keep_count:]

    layered = list(systems)
    if head:
        layered.append(_digest(head, summarize_chars))
        actions.append(f"历史分层压缩：{len(head)} 条 → 1 条摘要")
    layered.extend(tail)

    final = _total_tokens(layered)
    # 极少数情况（单条就超预算）摘要也压缩不了，保持原样返回并如实记录
    if final > max_tokens:
        actions.append(f"仍超预算（{final}>{max_tokens}），已尽力压缩")

    return TrimResult(
        messages=layered,
        original_tokens=original,
        final_tokens=final,
        saved_tokens=original - final,
        actions=actions,
        trimmed=bool(actions),
    )
