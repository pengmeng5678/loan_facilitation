"""流程预警与审批的通知渠道（把"躺着没人看"变成"主动找人"）。

**为什么需要它**：SOP 预警和高风险审批单原本只落库，靠主管自己打开
后台看 —— 而漏跟进的平均发现时间是"客户问起来的时候"。
通知渠道的价值不是技术，是**把被动查询变成主动触达**。

三个集成点（都在 `FlowOpsService`）：
    check_sop         本轮新 SOP 预警   → 一条 digest 通知主管
    request_high_risk 新建审批单        → 立刻通知审批人
    bootstrap         过期审批清理      → 通知"哪些单超时没人批"

**四条设计约束**：

1. **通知失败绝不拖垮业务**。Webhook 超时/挂了只记日志，SOP 扫描照常
   完成 —— 通知是"找人"的手段，不是流程的一部分。调用方统一走
   `FlowOpsService._safe_notify`（再包一层 try）。
2. **默认只写日志**（`LogNotifier`）：不配 webhook 时零外部依赖，
   本地 / 测试 / 内网试点都能跑；配了 `FLOW_NOTIFY_WEBHOOK_URL`
   才升级成 `WebhookNotifier`（企微 / 钉钉机器人）。
3. **载荷天然脱敏**：只带线索 ID、库里存的脱敏姓名、阶段、逾期分钟数
   —— 没有手机号、没有征信数据、没有合同金额。
4. **Protocol 而不是继承**：以后要接飞书 / 短信 / 电话，实现同一个
   `notify` 即可，`FlowOpsService` 一行都不用改。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger

logger = get_logger(__name__)


class Notifier(Protocol):
    """通知渠道协议：实现 `notify` 即插即用。"""

    def notify(self, event: str, *, title: str, fields: dict[str, Any]) -> None:
        """发送一条通知。

        event   事件标识（sop_alerts / approval_requested / approvals_expired）
        title   给人看的一行标题
        fields  结构化明细（键值对；list 值会渲染成多条）
        """
        ...


class LogNotifier:
    """默认渠道：写结构化日志。离线/测试/内网试点零配置可用。"""

    def notify(self, event: str, *, title: str, fields: dict[str, Any]) -> None:
        # WARNING 级：预警类事件值得在日志里被看到，但默认渠道本身不救命，
        # 定 WARNING 是为了 grep "通知[" 时能捞出全部历史
        logger.warning(
            "通知[%s] %s | %s",
            event, title,
            json.dumps(fields, ensure_ascii=False, default=str),
        )


def render_markdown(title: str, fields: dict[str, Any]) -> str:
    """把 (title, fields) 渲染成企微/钉钉都认的 markdown 文本。"""
    lines = [f"### {title}"]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            lines.append(f"**{key}**（{len(value)}）")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"> {key}：{value}")
    return "\n".join(lines)


class WebhookNotifier:
    """企微 / 钉钉机器人 webhook 通知。

    消息体按域名自动适配：
        企微（qyapi.weixin.qq.com）  {"msgtype":"markdown","markdown":{"content": ...}}
        钉钉（oapi.dingtalk.com）    {"msgtype":"markdown","markdown":{"title": ...,"text": ...}}

    **发送策略**：单条、同步、短超时（默认 3s，`FLOW_NOTIFY_TIMEOUT_SECONDS`
    可调）。不排队、不重试 —— 通知的价值随时间指数衰减，迟到的预警
    不如没有；失败由调用方的日志兜底。
    """

    def __init__(self, url: str, timeout: float = 3.0) -> None:
        self._url = url
        self._timeout = max(0.5, timeout)

    def _payload(self, title: str, fields: dict[str, Any]) -> dict[str, Any]:
        markdown = render_markdown(title, fields)
        if "dingtalk" in self._url:
            return {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}
        return {"msgtype": "markdown", "markdown": {"content": markdown}}

    def notify(self, event: str, *, title: str, fields: dict[str, Any]) -> None:
        import httpx  # 延迟导入：不配 webhook 的部署不该为它付 import 成本

        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(self._url, json=self._payload(title, fields))
            if resp.status_code >= 400:
                logger.warning(
                    "通知[%s] webhook 返回 %s：%s", event, resp.status_code, resp.text[:200],
                )
        except Exception:  # noqa: BLE001 - 通知失败只记日志，理由见文件头约束 1
            logger.exception("通知[%s] webhook 发送失败（不影响业务流程）", event)


def build_notifier(settings: Settings) -> Notifier:
    """按配置构造通知渠道：配了 webhook 地址才用 Webhook，否则 Log。"""
    url = (settings.flow_notify_webhook_url or "").strip()
    if not url:
        return LogNotifier()
    return WebhookNotifier(url, timeout=settings.flow_notify_timeout_seconds)


# --------------------------------------------------------------------------- #
# 事件载荷构造（service 层只负责调用，格式细节收在这里）
# --------------------------------------------------------------------------- #
def sop_alerts_fields(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """SOP 预警 digest 的明细。

    聚合而不是逐条发：一轮扫描可能命中几十条，逐条发会撞企微机器人
    频率限制（约 20 条/分钟），主管收到的也是同一件事的 N 条轰炸。
    """
    details = [
        f"{a.get('owner', '?')} · {a.get('customer_name', '?')} · "
        f"{a.get('stage', '?')} · {a.get('action', '?')} · 已逾期 {a.get('overdue_minutes', '?')} 分钟"
        for a in alerts
    ]
    return {"待处理预警": len(alerts), "明细": details, "处理入口": "GET /flow/sop/alerts"}


def approval_requested_fields(appr: dict[str, Any]) -> dict[str, Any]:
    """新建审批单的明细（审批人需要知道"批什么、为什么、批到什么时候"）。"""
    expires_at = appr.get("expires_at") or 0
    deadline = (
        datetime.fromtimestamp(expires_at).strftime("%m-%d %H:%M") if expires_at else "未设置"
    )
    return {
        "审批单": appr.get("approval_id", "?"),
        "动作": appr.get("action", "?"),
        "线索": appr.get("lead_id", "?"),
        "发起人": appr.get("requester", "?"),
        "原因": appr.get("reason", ""),
        "截止": f"{deadline}（超时不批自动失效，不会自动批准）",
        "处理入口": "POST /flow/approvals/{id}/decide",
    }


def approvals_expired_fields(approval_ids: list[str]) -> dict[str, Any]:
    """过期审批清理的明细。"""
    return {
        "过期审批单": approval_ids,
        "说明": "超时未批，已标记 expired；相关动作不会被执行，需人工重新发起",
    }
