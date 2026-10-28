"""MCP 工具层：CRM / 企业微信 OpenAPI 抽象（项目八"流程自动化"的对接层）。

简历里写的 "MCP 工具化对接 CRM / 企微 / 进件系统"，落到代码就是这一层：
外部系统的每个动作被封装成一个**带治理元数据**的工具（幂等性、
风险等级、超时），流程引擎只跟工具打交道，不关心对面是 REST 还是 gRPC。

**离线优先**：`RecordingCrmClient` 记录调用、返回确定性的假响应，
于是"流程编排逻辑"可以在零凭证环境下完整测试 —— 对应真实系统时
只需要替换 client 实现，流程代码一行不改。

**为什么每个动作都返回 dict 而不是抛异常**：流程引擎需要把
"CRM 挂了"变成"这条线索标记为待重试 + 告警"，而不是让整个
SOP 扫描崩掉。外部系统不可用是常态，不是异常。
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from agent_platform.logging_config import get_logger

logger = get_logger(__name__)


class CrmClient(Protocol):
    """CRM / 企微客户端协议。真实实现走 OpenAPI，测试用 Recording。"""

    def assign_owner(self, lead_id: str, *, owner: str, idem_key: str) -> dict[str, Any]: ...

    def log_call(self, lead_id: str, *, owner: str, outcome: str, note: str, idem_key: str) -> dict[str, Any]: ...

    def send_wechat_message(self, customer_id: str, *, text: str, idem_key: str) -> dict[str, Any]: ...

    def create_appointment(
        self, lead_id: str, *, store: str, scheduled_at: float, idem_key: str
    ) -> dict[str, Any]: ...

    def notify_supervisor(self, *, owner: str, message: str, idem_key: str) -> dict[str, Any]: ...


class RecordingCrmClient:
    """离线 CRM：只记录、返回确定性假响应。dry-run 与测试共用。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 动作（签名与 Protocol 一致；真实实现把 self._record 换成 HTTP 调用）
    # ------------------------------------------------------------------ #
    def assign_owner(self, lead_id: str, *, owner: str, idem_key: str) -> dict[str, Any]:
        return self._record("assign_owner", lead_id=lead_id, owner=owner, idem_key=idem_key)

    def log_call(
        self, lead_id: str, *, owner: str, outcome: str, note: str, idem_key: str
    ) -> dict[str, Any]:
        return self._record(
            "log_call", lead_id=lead_id, owner=owner, outcome=outcome, note=note, idem_key=idem_key
        )

    def send_wechat_message(self, customer_id: str, *, text: str, idem_key: str) -> dict[str, Any]:
        # 刻意回显长度而不是内容：日志里不落客户沟通原文（隐私）
        return self._record(
            "send_wechat_message", customer_id=customer_id, text_length=len(text), idem_key=idem_key
        )

    def create_appointment(
        self, lead_id: str, *, store: str, scheduled_at: float, idem_key: str
    ) -> dict[str, Any]:
        return self._record(
            "create_appointment", lead_id=lead_id, store=store,
            scheduled_at=scheduled_at, idem_key=idem_key,
        )

    def notify_supervisor(self, *, owner: str, message: str, idem_key: str) -> dict[str, Any]:
        return self._record("notify_supervisor", owner=owner, message_length=len(message), idem_key=idem_key)

    # ------------------------------------------------------------------ #
    # 记录
    # ------------------------------------------------------------------ #
    def _record(self, action: str, **kwargs: Any) -> dict[str, Any]:
        call = {"action": action, **kwargs, "ts": time.time(), "ok": True}
        self.calls.append(call)
        logger.info("CRM[%s]：%s", action, {k: v for k, v in kwargs.items() if k != "note"})
        return {"ok": True, "action": action, "echo": {k: v for k, v in kwargs.items()}}

    def calls_of(self, action: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["action"] == action]

    def reset(self) -> None:
        self.calls.clear()
