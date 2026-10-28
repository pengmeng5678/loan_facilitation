"""MCP 适配层测试。

覆盖四块（对应 mcp_adapter.py 的四个设计决策）：
    ① 生命周期    —— initialize 版本协商 / ping / 通知不应答
    ② 工具发现    —— tools/list 的名称映射、schema 透传、高风险工具默认隐藏
    ③ 工具调用    —— 正常调用 / 未知工具（协议错误）/ 执行失败（in-band isError）
    ④ 双传输      —— stdio 按行消息流 + Streamable HTTP（POST JSON / 202 / 405 / 406）

治理边界单独验证：expose_high_risk=False 时高风险工具既不可见也不可调 ——
"协议适配不能成为 HITL 治理的旁路"。
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_platform.mcp_adapter import (
    MCPProtocolError,
    MCPToolServer,
    mcp_tool_name,
    mount_mcp_http,
)
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec


def _tool(name: str, *, risk: str = "low", idempotent: bool = True, fail: bool = False) -> ToolSpec:
    def _handler(**kwargs: object) -> dict[str, object]:
        if fail:
            raise RuntimeError("下游超时")
        return {"echo": kwargs, "tool": name}

    return ToolSpec(
        name=name,
        description=f"测试工具 {name}",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=_handler,
        tags=("test",),
        risk_level=risk,  # type: ignore[arg-type]
        idempotent=idempotent,
    )


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_tool("calc.quote"))                      # 低风险幂等
    reg.register(_tool("api.send_sms", idempotent=False))  # 中风险非幂等
    reg.register(_tool("crm.send_contract", risk="high"))  # 高风险（HITL 领地）
    return reg


def _server(**kwargs: object) -> MCPToolServer:
    return MCPToolServer(_registry(), **kwargs)  # type: ignore[arg-type]


def _rpc(method: str, msg_id: int | str, params: dict | None = None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


# =========================================================================== #
# ① 生命周期
# =========================================================================== #
def test_initialize_negotiates_supported_version():
    resp = _server().handle_message(
        _rpc("initialize", 1, {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}})
    )
    assert resp["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"]


def test_initialize_unknown_version_falls_back_to_latest():
    resp = _server().handle_message(_rpc("initialize", 1, {"protocolVersion": "1999-01-01"}))
    assert resp["result"]["protocolVersion"] == "2025-06-18"


def test_ping_and_unknown_method():
    s = _server()
    assert s.handle_message(_rpc("ping", 2))["result"] == {}
    resp = s.handle_message(_rpc("resources/list", 3))
    assert resp["error"]["code"] == -32601  # Method not found


def test_notifications_are_not_answered():
    assert _server().handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


# =========================================================================== #
# ② 工具发现
# =========================================================================== #
def test_list_tools_maps_names_and_hides_high_risk():
    tools = {t["name"]: t for t in _server().list_tools()}
    assert set(tools) == {"calc__quote", "api__send_sms"}  # 点号映射 + 高风险隐藏
    assert tools["calc__quote"]["inputSchema"]["required"] == ["query"]
    assert tools["calc__quote"]["annotations"]["readOnlyHint"] is True
    assert tools["api__send_sms"]["annotations"]["readOnlyHint"] is False


def test_high_risk_visible_when_explicitly_enabled():
    tools = {t["name"] for t in _server(expose_high_risk=True).list_tools()}
    assert "crm__send_contract" in tools


def test_allowed_scopes_the_tool_list():
    server = MCPToolServer(_registry(), allowed={"calc.quote"})
    assert [t["name"] for t in server.list_tools()] == ["calc__quote"]


def test_mcp_tool_name_rejects_nothing_but_maps_dots():
    assert mcp_tool_name("a.b.c") == "a__b__c"
    assert mcp_tool_name("plain") == "plain"


# =========================================================================== #
# ③ 工具调用
# =========================================================================== #
def test_call_tool_ok_returns_structured_content():
    out = _server().call_tool("calc__quote", {"query": "利率"})
    # MCP 规范：isError 是可选字段，成功时可省略（语义默认 false）
    assert not out.get("isError")
    assert out["structuredContent"]["ok"] is True
    assert out["structuredContent"]["data"]["echo"] == {"query": "利率"}
    assert out["content"][0]["type"] == "text"


def test_call_unknown_tool_is_protocol_error():
    with pytest.raises(MCPProtocolError) as ei:
        _server().call_tool("nope__missing", {})
    assert ei.value.code == -32602


def test_call_high_risk_is_rejected_by_default():
    with pytest.raises(MCPProtocolError) as ei:
        _server().call_tool("crm__send_contract", {"query": "x"})
    assert ei.value.code == -32602
    assert "HITL" in ei.value.message  # 拒绝理由指向治理链路，而不是一句干巴巴的 denied


def test_call_execution_error_is_in_band_not_protocol_error():
    """工具内部失败 ≠ 协议失败：模型需要看到错误文本才能自修正。"""
    reg = ToolRegistry()
    reg.register(_tool("flaky.tool", fail=True))
    out = MCPToolServer(reg).call_tool("flaky__tool", {"query": "x"})
    assert out["isError"] is True
    assert "RuntimeError" in out["content"][0]["text"]


def test_handle_message_routes_tools_call():
    resp = _server().handle_message(
        _rpc("tools/call", 9, {"name": "calc__quote", "arguments": {"query": "利率"}})
    )
    assert resp["result"]["structuredContent"]["ok"] is True
    bad = _server().handle_message(_rpc("tools/call", 10, {"arguments": {}}))
    assert bad["error"]["code"] == -32602


# =========================================================================== #
# ④a stdio 传输
# =========================================================================== #
def test_stdio_roundtrip_and_notification_silence():
    lines = [
        json.dumps(_rpc("initialize", 1, {"protocolVersion": "2025-06-18"}), ensure_ascii=False),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}, ensure_ascii=False),
        "not a json line",  # 解析失败 → -32700，但进程不中断
        json.dumps(_rpc("tools/list", 2), ensure_ascii=False),
        "",  # 空行跳过
    ]
    out = io.StringIO()
    handled = _server().serve_stdio(io.StringIO("\n".join(lines)), out)
    responses = [json.loads(x) for x in out.getvalue().splitlines()]

    assert handled == 4  # initialize / 通知 / 坏行 / tools_list；空行跳过不计入
    assert len(responses) == 3  # 通知与空行不产生应答
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert responses[1]["error"]["code"] == -32700
    tools = responses[2]["result"]["tools"]
    assert {t["name"] for t in tools} == {"calc__quote", "api__send_sms"}


# =========================================================================== #
# ④b Streamable HTTP 传输
# =========================================================================== #
def _http_app() -> TestClient:
    app = FastAPI()
    mount_mcp_http(app, path="/mcp", get_server=lambda: _server())
    return TestClient(app)


_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_http_initialize_and_tools_call():
    client = _http_app()
    r = client.post("/mcp", json=_rpc("initialize", 1, {"protocolVersion": "2025-06-18"}), headers=_HEADERS)
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-06-18"

    r = client.post(
        "/mcp",
        json=_rpc("tools/call", 2, {"name": "calc__quote", "arguments": {"query": "利率"}}),
        headers=_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["result"]["structuredContent"]["ok"] is True


def test_http_notification_only_returns_202():
    r = _http_app().post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=_HEADERS
    )
    assert r.status_code == 202


def test_http_rejects_missing_accept_header():
    r = _http_app().post("/mcp", json=_rpc("ping", 1))
    assert r.status_code == 406


def test_http_get_is_405_for_stateless_server():
    assert _http_app().get("/mcp", headers=_HEADERS).status_code == 405


def test_http_batch_yields_batch_response():
    batch = [
        _rpc("initialize", 1, {"protocolVersion": "2025-06-18"}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _rpc("ping", 2),
    ]
    r = _http_app().post("/mcp", json=batch, headers=_HEADERS)
    body = r.json()
    assert isinstance(body, list) and len(body) == 2  # 通知不占应答位
    assert body[1]["result"] == {}
