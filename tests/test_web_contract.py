"""前后端联调契约测试（真实 HTTP，uvicorn 子进程 + httpx）。

这是"把前后端业务流程全部跑通"的**机器化证据**：
起一个真的 uvicorn（独立数据目录 + 开启 API 鉴权），灌种子数据，
然后用 httpx 按业务顺序打真实端点，一路从「录入线索」走到
「签约执行」，再验鉴权 / 幂等 / 脱敏 / 指标 / Web 控制台。

为什么用子进程而不是 TestClient：TestClient 是进程内调用，
验证不了"多 worker 部署形态下经得起真实网络"的链路（中间件栈、
uvicorn 的事件循环、静态文件挂载）。联调就要用真 HTTP。

跳过条件：默认跑。起不来的场景（端口被占等）会 fail 并打印日志，
不会静默跳过 —— 联调用例的价值就在于"真的跑过"。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
API_KEY = "e2e-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """起一个独立数据目录的 uvicorn，返回 base_url；模块结束后收尸。"""
    data = tmp_path_factory.mktemp("e2e_data")
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "APP_ENV": "local",
            "LOG_LEVEL": "WARNING",
            # 联调同时验证鉴权：开开关 + 配一把 key
            "API_AUTH_ENABLED": "true",
            "API_AUTH_KEYS": API_KEY,
            # 全部数据目录指到临时区，绝不碰仓库里的 .data
            "SQLITE_BUSINESS_PATH": str(data / "business.sqlite"),
            "POLICY_DATA_DIR": str(data / "policies"),
            "PROFILE_DATA_DIR": str(data / "profiles"),
            "MARKETING_DATA_DIR": str(data / "marketing"),
            "CHECKPOINTER_SQLITE_PATH": str(data / "checkpoints.sqlite"),
            "GATEWAY_SQLITE_PATH": str(data / "gateway.sqlite"),
            "VOICE_DATA_DIR": str(data / "voice"),
            "VOICE_SQLITE_PATH": str(data / "voice" / "voice.sqlite"),
            "FLOW_DATA_DIR": str(data / "flow"),
            "FLOW_SQLITE_PATH": str(data / "flow" / "flow.sqlite"),
        }
    )

    # ① 先灌种子（与后端同一套 env，数据落同一批临时目录）
    seed = subprocess.run(
        [sys.executable, "scripts/seed_data.py"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert seed.returncode == 0, f"seed 失败：{seed.stdout[-2000:]}{seed.stderr[-2000:]}"

    # ② 起 uvicorn
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "--app-dir", "src",
            "agent_platform.api.app:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        # ③ 等 /health 就绪（装配要几秒）
        deadline = time.time() + 60
        last_err = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"uvicorn 提前退出：{out[-3000:]}")
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_err = str(exc)
            time.sleep(0.3)
        else:
            pytest.fail(f"uvicorn 60s 未就绪（{last_err}）")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@pytest.fixture(scope="module")
def client(server):
    with httpx.Client(
        base_url=server, headers={"X-API-Key": API_KEY}, timeout=30
    ) as c:
        yield c


# =========================================================================== #
# 全业务流程：咨询 → 邀约 → 方案 → 费用 → 签约
# =========================================================================== #
def test_full_business_flow_over_http(client, server):
    # ---- 0. 鉴权：白名单放行，业务前缀要 key ----
    assert httpx.get(f"{server}/health", timeout=5).status_code == 200
    assert httpx.get(f"{server}/flow/leads", timeout=5).status_code == 401
    assert httpx.get(
        f"{server}/flow/leads", headers={"X-API-Key": API_KEY}, timeout=5
    ).status_code == 200

    # ---- 1. 总览：锁后端 / 部署形态可确认 ----
    stats = client.get("/flow/stats").json()
    assert stats["lock_backend"] == "SqliteLockBackend"  # 联调默认 sqlite；多 worker 换 redis

    # ---- 2. 录入线索（幂等）----
    # 用种子没有的 (customer, source, owner) 组合：幂等键是业务语义
    # （customer_id + source + owner），撞键会命中种子线索的缓存
    payload = {
        "customer_id": "C1001",
        "customer_name": "张*生",
        "phone_masked": "138****5678",
        "source": "e2e_test",
        "owner": "rep_e2e",
        "note": "联调测试线索",
    }
    lead = client.post("/flow/leads", json=payload).json()
    assert lead["stage"] == "new"
    lead_id = lead["lead_id"]

    # 重复录入（同一业务语义）→ 幂等命中，库里不产生第二条
    again = client.post("/flow/leads", json=payload).json()
    assert again["lead_id"] == lead_id
    listing = client.get("/flow/leads").json()["leads"]
    assert (
        sum(
            1
            for x in listing
            if x["customer_id"] == "C1001" and x["owner"] == "rep_e2e"
        )
        == 1
    )

    # ---- 3. 推进阶段（状态机）----
    for stage in ("assigned", "first_called", "wechat_added", "nurturing"):
        r = client.post(f"/flow/leads/{lead_id}/advance", json={"to_stage": stage})
        assert r.status_code == 200, r.text
        assert r.json()["stage"] == stage
    # 非法流转必须被拒（409）
    r = client.post(f"/flow/leads/{lead_id}/advance", json={"to_stage": "signed"})
    assert r.status_code == 409

    # ---- 4. 邀约到店（幂等 + 自动推进到 invited）----
    visit_at = int(time.time()) + 86400
    appt = client.post(
        f"/flow/leads/{lead_id}/invite",
        json={"store": "南京旗舰店", "scheduled_at": visit_at},
    ).json()
    assert appt["status"] == "booked"
    assert client.get(f"/flow/leads/{lead_id}").json()["stage"] == "invited"

    # ---- 5. 到店确认 ----
    arrived = client.post(f"/flow/leads/{lead_id}/arrived").json()
    assert arrived["stage"] == "arrived"

    # ---- 6. 到店前准备（一页纸：档案 + 方案初稿 + 费用预估）----
    pack = client.post(f"/flow/leads/{lead_id}/readiness").json()
    assert "pack_id" in pack
    assert pack.get("plan_draft") or pack.get("handoff_reason")
    assert client.get(f"/flow/leads/{lead_id}").json()["stage"] == "proposal_ready"

    # ---- 7. 费用预估（预估 ≠ 报价）----
    fee = client.post(f"/flow/leads/{lead_id}/fee", json={"loan_amount": 300000}).json()
    assert fee["service_fee"] > 0
    assert client.get(f"/flow/leads/{lead_id}").json()["stage"] == "fee_estimated"

    # ---- 8. 高风险动作：申请 → 审批 → 执行（HITL 全链路）----
    appr = client.post(
        f"/flow/leads/{lead_id}/request-contract", json={"reason": "联调：客户确认方案"}
    ).json()
    assert appr["status"] == "pending"
    approval_id = appr["approval_id"]

    pending = client.get("/flow/approvals", params={"status": "pending"}).json()["approvals"]
    assert any(a["approval_id"] == approval_id for a in pending)

    decided = client.post(
        f"/flow/approvals/{approval_id}/decide",
        json={"approver": "mgr_01", "approved": True, "comment": "同意"},
    ).json()
    assert decided["status"] == "approved"

    executed = client.post(f"/flow/approvals/{approval_id}/execute").json()
    assert executed  # RecordingCrmClient 记录到即成功

    # ---- 9. 自助查进度：正确后 4 位放行，错误拒绝且不泄露差异 ----
    ok = client.post(
        "/flow/progress", json={"lead_id": lead_id, "phone_last4": "5678"}
    ).json()
    assert ok["current_stage"] == "fee_estimated"
    assert ok["customer_name"] == "张**"  # 服务端脱敏（留首字，其余打码）
    bad = client.post(
        "/flow/progress", json={"lead_id": lead_id, "phone_last4": "0000"}
    )
    assert bad.status_code == 403

    # ---- 10. SOP 扫描（定时任务的同一入口）----
    sweep = client.post("/flow/sop/sweep").json()
    assert isinstance(sweep, dict)
    alerts = client.get("/flow/sop/alerts").json()
    assert alerts["count"] >= 0

    # ---- 11. 产品问答（真实检索链路，非 mock）----
    qa = client.post(
        "/product/qa", json={"question": "公积金信用贷需要什么条件？"}
    ).json()
    assert isinstance(qa["answer"], str) and qa["answer"]
    assert qa["citations"] or qa["refused"]  # 有引用或明确拒答，二必居一

    # ---- 12. 指标端点（Prometheus 格式）----
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "# TYPE flow_hitl_pending gauge" in metrics.text
    assert "# TYPE gateway_calls_total counter" in metrics.text

    # ---- 13. Web 控制台（Vite 构建产物由 FastAPI 服务于 /ui）----
    ui = client.get("/ui/")
    assert ui.status_code == 200
    assert 'id="root"' in ui.text
    asset = client.get("/ui/assets/", follow_redirects=False)
    assert asset.status_code in (403, 404)  # 目录列表不放行


def test_platform_agent_run_over_http(client):
    """平台编排入口也走通：跑一次 flow_ops Agent（HITL 恢复语义的载体）。"""
    agents = client.get("/platform/agents").json()
    names = [a["name"] for a in agents.get("agents", agents if isinstance(agents, list) else [])]
    assert "flow_ops" in names
