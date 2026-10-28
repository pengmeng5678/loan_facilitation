"""平台内置工具集。

这些是"与业务无关、任何 Agent 都可能用到"的基础工具。业务工具
（产品检索、客户建档、会话质检…）由各业务服务自行注册 —— 这正是
平台"提供能力、不定义业务"的边界体现。
"""

from __future__ import annotations

import time
from typing import Any

from agent_platform.tools.spec import ToolSpec


class MemoryStore:
    """极简的会话级 KV 存储。

    真实生产应替换为 Redis（带 TTL / 分布式锁）。这里用进程内 dict，
    是为了让本地演示与测试零依赖，同时保留清晰的替换点。
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def keys(self) -> list[str]:
        return list(self._data.keys())


def build_builtin_tools(store: MemoryStore | None = None) -> list[ToolSpec]:
    """构造平台内置工具列表。"""
    memory = store or MemoryStore()

    def _now() -> dict[str, Any]:
        return {"timestamp": time.time(), "iso": time.strftime("%Y-%m-%d %H:%M:%S")}

    def _memory_set(key: str, value: str) -> dict[str, Any]:
        memory.set(key, value)
        return {"stored": True, "key": key}

    def _memory_get(key: str) -> dict[str, Any]:
        return {"key": key, "value": memory.get(key), "found": memory.get(key) is not None}

    def _estimate_tokens(text: str) -> dict[str, Any]:
        from agent_platform.llm.base import estimate_tokens

        return {"tokens": estimate_tokens(text), "chars": len(text)}

    def _handoff_to_human(reason: str, urgency: str = "normal") -> dict[str, Any]:
        """转人工：平台级的兜底出口。

        任何 Agent 走到了能力的边界（低置信度、超预算、无匹配结果），
        都应该显式调用它，而不是硬编一个答案 —— 这是"AI 不做承诺"的工程落地。
        """
        return {
            "handoff": True,
            "reason": reason,
            "urgency": urgency,
            "queue": "human_expert",
            "created_at": time.time(),
        }

    def _send_external_message(target: str, content: str) -> dict[str, Any]:
        """发送外部消息（企微 / 短信）。

        这是 high 风险 + 非幂等 的典型工具：一旦发错无法撤回，
        所以平台会在编排层对它强制插入 HITL 审批检查点。
        """
        return {
            "sent": True,
            "target": target,
            "content_preview": content[:50],
            "message_id": f"msg_{int(time.time() * 1000)}",
        }

    return [
        ToolSpec(
            name="platform.now",
            description="获取当前时间。用于计算时效、超时判断。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_now,
            tags=("platform", "read"),
        ),
        ToolSpec(
            name="platform.memory_set",
            description="把一条键值写进会话记忆，供后续步骤读取。",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "记忆键"},
                    "value": {"type": "string", "description": "记忆值"},
                },
                "required": ["key", "value"],
            },
            handler=_memory_set,
            tags=("platform", "memory", "write"),
        ),
        ToolSpec(
            name="platform.memory_get",
            description="读取会话记忆中的值。",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            handler=_memory_get,
            tags=("platform", "memory", "read"),
        ),
        ToolSpec(
            name="platform.estimate_tokens",
            description="估算一段文本的 token 数，用于成本与上下文预算评估。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=_estimate_tokens,
            tags=("platform", "read"),
        ),
        ToolSpec(
            name="platform.handoff_to_human",
            description=(
                "把当前任务转交人工专家。当出现：无匹配结果、置信度过低、"
                "超出能力边界、或触发熔断时，调用它而不是自己编答案。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "转人工原因"},
                    "urgency": {"type": "string", "description": "normal | high"},
                },
                "required": ["reason"],
            },
            handler=_handoff_to_human,
            tags=("platform", "escalation", "write"),
            risk_level="medium",
        ),
        ToolSpec(
            name="platform.send_external_message",
            description=(
                "向客户或业务员发送外部消息（企业微信 / 短信）。"
                "因为对外可见且不可撤回，属于高风险动作，必须经过人工审批。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "接收方标识"},
                    "content": {"type": "string", "description": "消息内容"},
                },
                "required": ["target", "content"],
            },
            handler=_send_external_message,
            tags=("platform", "message", "write"),
            risk_level="high",
            idempotent=False,  # 非幂等：重复发送会造成骚扰，必须靠审批 + 幂等键兜底
        ),
    ]
