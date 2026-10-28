"""HTTP API 集成测试。

用 FastAPI TestClient 打真实路由，验证三个服务在同进程下都能通过 HTTP 访问，
以及 HITL 的"挂起 → 查询待办 → 审批 → 恢复"完整闭环。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_platform.api.app import app


@pytest.fixture
def client(app_context_fixture) -> TestClient:
    """已注入夹具上下文的测试客户端。"""
    return TestClient(app)


# =========================================================================== #
# 运维
# =========================================================================== #
def test_health_and_root(client):
    assert client.get("/health").json() == {"status": "ok"}
    index = client.get("/").json()
    assert index["version"] == "1.0.0"
    # 八个前缀都要在服务信息里可见（运维靠它确认部署了哪几个服务）
    assert index["endpoints"] == {
        "platform": "/platform",
        "data": "/data",
        "product": "/product",
        "coaching": "/coaching",
        "marketing": "/marketing",
        "voice": "/voice",
        "flow": "/flow",
        "gateway": "/gateway",
    }


def test_ready_reports_assembled_services(client):
    payload = client.get("/ready").json()
    assert payload["status"] == "ready"
    assert set(payload["agents"]) == {
        "product_assistant", "coaching", "marketing", "voice_cs", "gateway", "flow_ops",
    }
    assert payload["tools"] > 0


def test_docs_served_offline_without_cdn(client):
    """Swagger UI 必须由本进程提供静态资源。

    背景：FastAPI 默认从 cdn.jsdelivr.net 加载 swagger-ui，国内网络连不上，
    表现是"标签页标题正常、正文一片空白"（HTML 本地返回、JS 永远加载失败）。
    这条用例锁死"不许再回退到外网 CDN"这个约束。
    """
    html = client.get("/docs")
    assert html.status_code == 200
    body = html.text
    assert "SwaggerUIBundle" in body
    assert "cdn.jsdelivr.net" not in body
    assert "/static/swagger-ui/swagger-ui-bundle.js" in body

    # 光 HTML 对不够 —— 资源本身取不到同样是白屏
    js = client.get("/static/swagger-ui/swagger-ui-bundle.js")
    assert js.status_code == 200
    assert len(js.content) > 100_000
    css = client.get("/static/swagger-ui/swagger-ui.css")
    assert css.status_code == 200
    assert ".swagger-ui" in css.text


# =========================================================================== #
# 平台
# =========================================================================== #
def test_platform_lists_agents_and_tools(client):
    agents = client.get("/platform/agents").json()
    names = {a["name"] for a in agents["agents"]}
    assert names == {
        "product_assistant", "coaching", "marketing", "voice_cs", "gateway", "flow_ops",
    }

    tools = client.get("/platform/tools").json()
    assert tools["count"] == len(tools["tools"])
    assert any(t["name"] == "platform.send_external_message" for t in tools["tools"])
    # 项目六/项目七/项目一的工具也挂到了平台上（平台不关心它们是谁写的）
    assert any(t["name"] == "marketing.generate_materials" for t in tools["tools"])
    assert any(t["name"] == "voice.start_session" for t in tools["tools"])
    assert any(t["name"] == "gateway.route_preview" for t in tools["tools"])
    # 项目八的工具也挂到了平台上（高风险工具平台会自动插入 HITL 检查点）
    assert any(t["name"] == "flow.intake_lead" for t in tools["tools"])
    assert any(t["name"] == "flow.request_contract" for t in tools["tools"])


def test_platform_run_unknown_agent_returns_404(client):
    resp = client.post("/platform/agents/not_exist/run", json={"input": "hi"})
    assert resp.status_code == 404


def test_platform_metrics_endpoint(client):
    client.post(
        "/platform/agents/coaching/run",
        json={"input": "帮我挖掘高转化话术"},
    )
    metrics = client.get("/platform/metrics").json()
    assert "metrics" in metrics
    assert metrics["agents"] == [
        "product_assistant", "coaching", "marketing", "voice_cs", "gateway", "flow_ops",
    ]


# =========================================================================== #
# 项目二
# =========================================================================== #
def test_product_qa_with_citation(client):
    resp = client.post("/product/qa", json={"question": "测试公积金信用贷的利率是多少"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["refused"] is False
    assert payload["citations"]


def test_product_qa_refusal(client):
    resp = client.post("/product/qa", json={"question": "帮我预测明天股票涨跌"})
    assert resp.status_code == 200
    assert resp.json()["refused"] is True


def test_product_match_and_no_match(client):
    ok = client.post("/product/match", json={"customer_id": "CU001"}).json()
    assert ok["matched"] is True
    assert ok["primary"]["product_code"].startswith("BANK-T-")

    none = client.post("/product/match", json={"customer_id": "CU002"}).json()
    assert none["matched"] is False
    assert none["handoff_reason"]


def test_product_match_unknown_customer_returns_404(client):
    assert client.post("/product/match", json={"customer_id": "NOPE"}).status_code == 404


def test_product_proposal_and_conflict(client):
    ok = client.post("/product/proposal", json={"customer_id": "CU001"})
    assert ok.status_code == 200
    assert ok.json()["markdown"]

    # 无匹配产品时返回 409（不硬配，交给上游走人工）
    conflict = client.post("/product/proposal", json={"customer_id": "CU002"})
    assert conflict.status_code == 409


def test_product_catalog_and_graph(client):
    products = client.get("/product/products").json()
    assert products["count"] >= 2
    # 过期产品不应出现在目录里
    codes = {p["code"] for p in products["products"]}
    assert "BANK-T-OLD-001" not in codes

    graph = client.get("/product/products/BANK-T-GJJ-001/graph").json()
    assert "BANK-T-DBT-001" in graph["substitutes"]


# =========================================================================== #
# 项目三
# =========================================================================== #
def test_coaching_full_flow(client):
    personas = client.get("/coaching/personas").json()
    assert personas["count"] >= 2

    session = client.post(
        "/coaching/sessions", json={"persona_id": "P-T-HARD", "trainee": "学员甲"}
    ).json()
    session_id = session["session_id"]
    assert session["opening"]

    reply = client.post(
        f"/coaching/sessions/{session_id}/reply",
        json={"text": "我先帮您做资质初审：公积金缴存多久、征信查询几次？"},
    ).json()
    assert reply["customer_reply"]

    replay = client.get(f"/coaching/sessions/{session_id}/replay").json()
    assert len(replay["messages"]) >= 3

    card = client.post(f"/coaching/sessions/{session_id}/finish").json()
    assert 0 <= card["total_score"] <= 100
    assert "report_text" in card


def test_coaching_qc_single_and_all(client):
    one = client.post("/coaching/qc", json={"conversation_id": "TCONV-2"}).json()
    assert one["needs_alert"] is True
    assert one["score"] < 100

    all_reports = client.post("/coaching/qc", json={}).json()
    assert all_reports["count"] >= 2
    assert all_reports["needs_alert"] >= 1


def test_coaching_mining_and_calibrate(client):
    mined = client.post("/coaching/mining").json()
    assert "mined" in mined

    calib = client.post(
        "/coaching/calibrate",
        json={"judge_scores": [80, 70], "human_scores": [78, 72], "tolerance": 10},
    ).json()
    assert calib["samples"] == 2


def test_coaching_alert_endpoint_triggers_hitl(client):
    """告警接口走平台高风险工具 → 返回 interrupted 等待审批。"""
    resp = client.post(
        "/coaching/qc/alert",
        params={"conversation_id": "TCONV-2", "supervisor": "张主管"},
    ).json()
    assert resp["status"] == "interrupted"
    assert resp["pending_approval"]["tool"] == "platform.send_external_message"


# =========================================================================== #
# HITL 完整闭环（跨 HTTP）
# =========================================================================== #
def test_hitl_full_cycle_over_http(client):
    run = client.post(
        "/platform/agents/coaching/run",
        json={
            "input": "对 TCONV-2 做合规质检并告警主管",
            "context": {"conversation_id": "TCONV-2", "supervisor": "张主管"},
        },
    ).json()
    assert run["status"] == "interrupted"
    thread_id = run["thread_id"]

    # 审批中心能看到待办
    pending = client.get("/platform/approvals").json()
    assert pending["count"] >= 1

    # 会话状态可查
    state = client.get(f"/platform/runs/{thread_id}").json()
    assert state["agent"] == "coaching"

    # 审批后恢复
    resumed = client.post(
        f"/platform/runs/{thread_id}/resume",
        json={"approved": True, "decided_by": "张主管", "comment": "已核实"},
    ).json()
    assert resumed["status"] == "completed"
    assert resumed["approvals"][0]["approved"] is True
    assert resumed["output"]["alert_sent"] is True
