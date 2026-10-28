"""通知渠道测试（P0-3：预警/审批从"躺着没人看"变成"主动找人"）。

分两层：
    A. 渠道本体（纯逻辑 / 离线）—— LogNotifier、WebhookNotifier 的消息体、
       build_notifier 的选路；
    B. 服务集成 —— FlowOpsService 的三个通知点：SOP 预警 digest、
       审批建单、过期清理；以及"通知炸了不拖垮业务"这条铁律。
"""

from __future__ import annotations

import time

import pytest

from agent_platform.config import Settings
from flow_ops.domain import LeadStage
from flow_ops.notifier import (
    LogNotifier,
    WebhookNotifier,
    build_notifier,
    render_markdown,
)
from flow_ops.service import FlowOpsService


class RecordingNotifier:
    """测试专用通知器：记下收到的每一条，不发任何请求。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def notify(self, event: str, *, title: str, fields: dict) -> None:
        self.events.append({"event": event, "title": title, "fields": fields})

    def of(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]


class ExplodingNotifier:
    """模拟"IM 机器人挂了"：通知必然抛错。"""

    def notify(self, event: str, *, title: str, fields: dict) -> None:
        raise RuntimeError("webhook unreachable")


def _service_settings(tmp_path) -> Settings:
    return Settings(
        app_env="local", log_level="ERROR", llm_provider="echo",
        embedding_provider="local", embedding_dim=256,
        checkpointer_backend="memory", vector_backend="local",
        flow_data_dir=str(tmp_path / "flow"),
        flow_sqlite_path=str(tmp_path / "flow" / "flow.sqlite"),
    )


@pytest.fixture
def service(tmp_path):
    """注入了 RecordingNotifier 的流程服务（通知内容可断言）。"""
    svc = FlowOpsService(
        settings=_service_settings(tmp_path),
        plan_provider=lambda cid: {},
        notifier=RecordingNotifier(),
    )
    svc.bootstrap()
    yield svc
    svc.close()


def _make_overdue(service: FlowOpsService) -> str:
    """造一条"分了业务员但 40 分钟没首呼"的线索，返回 lead_id。"""
    lead = service.intake_lead(
        customer_id="C9001", customer_name="测试客户",
        phone_masked="138****5678", source="douyin", owner="rep_01",
    )
    service.advance(lead["lead_id"], LeadStage.ASSIGNED)
    stored = service.engine.store.get_lead(lead["lead_id"])
    stored.stage_entered_at = time.time() - 40 * 60  # 首呼 SLA 默认 30 分钟
    service.engine.store.update_lead(stored)
    return lead["lead_id"]


# =========================================================================== #
# A. 渠道本体
# =========================================================================== #
class TestLogNotifier:
    def test_logs_event_and_title(self, caplog):
        with caplog.at_level("WARNING", logger="flow_ops.notifier"):
            LogNotifier().notify(
                "sop_alerts", title="SOP 逾期预警 ×1",
                fields={"待处理预警": 1, "明细": ["rep_01 · 测试客户 · assigned · first_call · 已逾期 10 分钟"]},
            )
        assert "通知[sop_alerts]" in caplog.text
        assert "SOP 逾期预警" in caplog.text


class TestWebhookNotifier:
    def test_wecom_payload_uses_content_key(self):
        n = WebhookNotifier("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc")
        payload = n._payload("标题", {"k": "v"})
        assert payload["msgtype"] == "markdown"
        assert "content" in payload["markdown"]
        assert "标题" in payload["markdown"]["content"]

    def test_dingtalk_payload_uses_title_and_text(self):
        n = WebhookNotifier("https://oapi.dingtalk.com/robot/send?access_token=xyz")
        payload = n._payload("标题", {"k": "v"})
        assert payload["markdown"]["title"] == "标题"
        assert "text" in payload["markdown"]

    def test_unreachable_webhook_does_not_raise(self):
        """通知失败只记日志 —— 这条是文件头约束 1 的回归防线。"""
        n = WebhookNotifier("https://127.0.0.1:9/unreachable", timeout=0.5)
        # 不该抛异常（连不上的地址会走 except 分支打日志）
        n.notify("sop_alerts", title="t", fields={})

    def test_timeout_floor(self):
        """超时下限 0.5s：配 0 会变成"永远等不到"而不是"立刻失败"。"""
        assert WebhookNotifier("https://example.com", timeout=0.1)._timeout == 0.5


def test_render_markdown_lists_become_bullets():
    text = render_markdown("标题", {"待处理预警": 2, "明细": ["a", "b"]})
    assert "### 标题" in text
    assert "> 待处理预警：2" in text        # 标量一行
    assert "**明细**（2）" in text           # list 带数量
    assert "- a" in text and "- b" in text   # list 逐条


def test_render_markdown_scalar_line():
    text = render_markdown("标题", {"审批单": "ap_1"})
    assert "> 审批单：ap_1" in text


class TestBuildNotifier:
    def test_defaults_to_log_without_url(self, tmp_path):
        notifier = build_notifier(_service_settings(tmp_path))
        assert isinstance(notifier, LogNotifier)

    def test_webhook_when_configured(self, tmp_path):
        settings = _service_settings(tmp_path)
        settings.flow_notify_webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"
        notifier = build_notifier(settings)
        assert isinstance(notifier, WebhookNotifier)
        assert notifier._timeout == settings.flow_notify_timeout_seconds


# =========================================================================== #
# B. 服务集成
# =========================================================================== #
class TestServiceNotification:
    def test_sop_alerts_notified_as_one_digest(self, service):
        _make_overdue(service)
        result = service.check_sop()
        assert result["count"] == 1

        events = service.notifier.of("sop_alerts")
        assert len(events) == 1  # digest：一轮一条，不是逐条轰炸
        fields = events[0]["fields"]
        assert fields["待处理预警"] == 1
        assert any("rep_01" in line and "first_call" in line for line in fields["明细"])

    def test_no_notification_when_quiet(self, service):
        """没有新预警就不发通知 —— 通知噪音比没有通知更糟。"""
        assert service.check_sop()["count"] == 0
        assert service.notifier.events == []

    def test_second_sweep_does_not_re_notify(self, service):
        """重复预警不重复通知（sweep 本身去重，通知跟着受益）。"""
        _make_overdue(service)
        service.check_sop()
        service.check_sop()
        assert len(service.notifier.of("sop_alerts")) == 1

    def test_high_risk_request_notifies_immediately(self, service):
        lead = service.intake_lead(
            customer_id="C9002", customer_name="测试客户",
            phone_masked="138****0000", source="referral", owner="rep_02",
        )
        service.request_high_risk(
            "send_contract", lead_id=lead["lead_id"], requester="agent", reason="客户已确认方案",
        )
        events = service.notifier.of("approval_requested")
        assert len(events) == 1
        fields = events[0]["fields"]
        assert fields["动作"] == "send_contract"
        assert fields["线索"] == lead["lead_id"]
        assert fields["原因"] == "客户已确认方案"
        assert "不会自动批准" in fields["截止"]  # 把铁律写进通知里

    def test_bootstrap_notifies_expired_approvals(self, service):
        """重启时清掉的过期单要通知：这些事"没人批"，需要人重新发起。

        用 ttl_hours=0 造"建单即过期"的审批单 —— expires_at 建单时锁定、
        update_approval 不落这个字段（想改过期时间只能重新建单），
        这是审批门的刻意设计：有效期不能事后被悄悄改掉。
        """
        lead = service.intake_lead(
            customer_id="C9003", customer_name="测试客户",
            phone_masked="138****1111", source="douyin", owner="rep_03",
        )
        service.engine.request_high_risk(
            "confirm_fee", lead_id=lead["lead_id"], requester="agent", ttl_hours=0,
        )
        service.notifier.events.clear()

        service.bootstrap()
        events = service.notifier.of("approvals_expired")
        assert len(events) == 1
        assert len(events[0]["fields"]["过期审批单"]) == 1

    def test_notifier_failure_does_not_break_sweep(self, tmp_path):
        """IM 挂了，SOP 扫描照常完成 —— 通知不是流程的一部分。"""
        svc = FlowOpsService(
            settings=_service_settings(tmp_path),
            plan_provider=lambda cid: {},
            notifier=ExplodingNotifier(),
        )
        try:
            lead = svc.intake_lead(
                customer_id="C9004", customer_name="测试客户",
                phone_masked="138****2222", source="douyin", owner="rep_04",
            )
            svc.advance(lead["lead_id"], LeadStage.ASSIGNED)
            stored = svc.engine.store.get_lead(lead["lead_id"])
            stored.stage_entered_at = time.time() - 40 * 60
            svc.engine.store.update_lead(stored)

            result = svc.check_sop()  # 不该抛
            assert result["count"] == 1

            svc.request_high_risk("send_contract", lead_id=lead["lead_id"])  # 也不该抛
        finally:
            svc.close()

    def test_default_notifier_is_log(self, tmp_path):
        """不显式注入时按配置自建（无 webhook → LogNotifier），生产零配置可用。"""
        svc = FlowOpsService(settings=_service_settings(tmp_path))
        try:
            assert isinstance(svc.notifier, LogNotifier)
        finally:
            svc.close()
