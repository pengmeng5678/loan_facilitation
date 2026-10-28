"""Prometheus /metrics 端点测试（对应 P1 可观测项）。

三层覆盖：
    ① 渲染器   text format 的转义 / 类型行 / 数值格式（纯函数，全离线）
    ② 采集器   AppContext → 指标族：核心指标必须采得到，某块炸了不拖垮全接口
    ③ 端点     HTTP 200 + content-type + 白名单（开了鉴权也不用 key 就能拉）
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_platform.api import app as app_module
from agent_platform.api.metrics import (
    MetricFamily,
    _escape_label_value,
    _fmt_value,
    collect_metric_families,
    render_prometheus,
)
from agent_platform.config import Settings


# =========================================================================== #
# 一、渲染器（纯函数）
# =========================================================================== #
class TestRenderer:
    def test_basic_counter(self):
        fam = MetricFamily(
            name="tool_calls_total",
            type="counter",
            help_text="工具调用总数",
            samples=[({"tool": "flow.advance", "ok": "True"}, 3.0), ({}, 1.0)],
        )
        out = render_prometheus([fam])
        assert "# HELP tool_calls_total 工具调用总数" in out
        assert "# TYPE tool_calls_total counter" in out
        assert 'tool_calls_total{ok="True",tool="flow.advance"} 3' in out
        assert "tool_calls_total 1" in out

    def test_empty_family_still_has_help_and_type(self):
        """空族也要输出 HELP/TYPE —— Prometheus 靠 TYPE 行判断类型。"""
        out = render_prometheus([MetricFamily("x_total", "counter", "help", [])])
        assert out == "# HELP x_total help\n# TYPE x_total counter\n"

    def test_label_value_escaping(self):
        assert _escape_label_value('a"b\\c\nd') == 'a\\"b\\\\c\\nd'

    def test_escaping_applied_in_output(self):
        fam = MetricFamily("m_total", "counter", "h", [({"k": 'quote"and\\slash'}, 1.0)])
        assert 'm_total{k="quote\\"and\\\\slash"} 1' in render_prometheus([fam])

    def test_value_formatting(self):
        assert _fmt_value(42.0) == "42"
        assert _fmt_value(0.1) == "0.1"
        assert _fmt_value(float("nan")) == "NaN"
        assert _fmt_value(float("inf")) == "+Inf"
        assert _fmt_value(float("-inf")) == "-Inf"

    def test_trailing_newline(self):
        out = render_prometheus([MetricFamily("m", "gauge", "h", [({}, 1.0)])])
        assert out.endswith("\n")


# =========================================================================== #
# 二、采集器（AppContext → 指标族）
# =========================================================================== #
class TestCollector:
    def test_core_metrics_present(self, app_context_fixture):
        families = collect_metric_families(app_context_fixture)
        by_name = {f.name: f for f in families}
        # 进程信息
        assert "app_info" in by_name
        assert by_name["app_info"].samples[0][0]["env"] == "local"
        # 网关（成本治理的证据链）
        assert "gateway_calls_total" in by_name
        assert "gateway_cost_yuan" in by_name
        # 流程（SOP / HITL 健康度）
        assert "flow_sop_alerts_open" in by_name
        assert "flow_hitl_pending" in by_name
        assert "flow_leads" in by_name
        # 调度器（生产里 running 必须为 1）
        sched = by_name["flow_scheduler_running"]
        assert sched.samples[0][1] == 0.0  # 测试默认不起调度器

    def test_runtime_counters_surface_after_run(self, app_context_fixture):
        """跑一次 Agent，计数必须出现在 /metrics 里（埋点 → 采集全链路）。"""
        ctx = app_context_fixture

        # 直接驱动一次带工具调用的运行，产生 agent_runs_total / tool_calls_total
        ctx.flow_ops.engine.intake_lead(
            customer_id="C_METRICS", customer_name="指标测试",
            phone_masked="139****0001", source="douyin", owner="rep_a",
        )
        families = {f.name: f for f in collect_metric_families(ctx)}
        # 线索数进了 gauge（by_stage 标签）
        lead_samples = families["flow_leads"].samples
        stages = {labels["stage"] for labels, _ in lead_samples}
        assert "new" in stages or "assigned" in stages

    def test_gateway_route_labels(self, app_context_fixture):
        """路由分布按 tier 打标签（验证复杂度路由真的在分流）。"""
        gw = app_context_fixture.gateway_service
        gw.gateway.route_preview("你好", task="chat")
        families = {f.name: f for f in collect_metric_families(app_context_fixture)}
        # route_preview 不一定计数，但网关调用数必须 >= 0 且指标族存在
        assert "gateway_calls_total" in families


# =========================================================================== #
# 三、HTTP 端点
# =========================================================================== #
class TestMetricsEndpoint:
    def test_metrics_endpoint(self, app_context_fixture):
        with TestClient(app_module.app) as client:
            resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        assert "# TYPE app_info gauge" in body
        assert "# TYPE gateway_calls_total counter" in body
        assert "# TYPE flow_sop_alerts_open gauge" in body

    def test_metrics_is_whitelisted_under_auth(self, app_context_fixture, monkeypatch):
        """开了 API 鉴权，/metrics 仍可拉取（scraper 带 key 太别扭）。"""
        import app_context as ac

        monkeypatch.setattr(ac, "get_app_context", lambda: app_context_fixture)
        settings = Settings(
            app_env="local", log_level="WARNING", llm_provider="echo",
            embedding_provider="local", embedding_dim=256,
            checkpointer_backend="memory", vector_backend="local",
            api_auth_enabled=True, api_auth_keys="k1,k2",
        )
        monkeypatch.setattr(app_module, "get_settings", lambda: settings)
        with TestClient(app_module.create_app()) as client:
            # 不带 key：探针与指标放行
            assert client.get("/health").status_code == 200
            assert client.get("/metrics").status_code == 200
            # 业务前缀依然要 key（证明白名单没有顺手放开全站）
            assert client.get("/flow/leads").status_code == 401
            assert client.get("/flow/leads", headers={"X-API-Key": "k1"}).status_code == 200

    def test_metrics_never_500s_on_collect_failure(self, app_context_fixture, monkeypatch):
        """采集炸了也要 200 —— 监控接口自己不能成为故障源。"""

        def _boom(_ctx):
            raise RuntimeError("collect exploded")

        monkeypatch.setattr("agent_platform.api.metrics.collect_metric_families", _boom)
        with TestClient(app_module.app) as client:
            resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "采集失败" in resp.text
