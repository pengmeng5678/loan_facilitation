"""MCP（Model Context Protocol）适配层 —— 把平台 ToolSpec 生态暴露为标准 MCP 服务器。

为什么要有这一层：
    平台内部工具协议是 ToolSpec（name / description / JSON Schema parameters /
    risk_level / idempotent）。MCP 是 2025 年起 Agent 工具互操作的事实标准——
    把注册中心里的工具按 MCP 规范暴露出去，任何支持 MCP 的客户端
    （Claude Desktop / Cursor / 其他 Agent 框架）都能直接发现并调用本平台工具，
    反过来，外部 MCP 服务器也能包装成 ToolSpec 注册进来。

实现说明（为什么手写协议而不是用官方 SDK）：
    MCP 的传输层就是 **JSON-RPC 2.0**（stdio 为按行分隔的消息流，
    Streamable HTTP 为 POST + JSON 响应），生命周期只有三步：
    initialize → notifications/initialized → tools/list / tools/call。
    本项目"零依赖离线可跑"的原则优先于引依赖，核心方法百行内可覆盖，
    且协议是自己实现一遍才能在面试里讲透的东西。
    生产若需要 sampling / roots 等高级能力，再评估引入官方 `mcp` 包。

**治理边界（本模块最重要的设计决策）**：
    MCP 通道默认**不暴露 high 风险工具**（expose_high_risk=False）。
    原因：平台对高风险动作的治理是"编排层强制 HITL 审批"——而 MCP 直连
    工具绕过了编排图，等于绕过了审批。所以默认策略是：高风险工具既不出现在
    tools/list 里，调用也直接报错；确有需要时由运维显式打开并自担合规责任。
    "协议适配不能成为治理旁路"，这句话就是这条边界的面试讲法。

协议版本：支持 2025-06-18 / 2025-03-26 / 2024-11-05，按客户端版本协商。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from agent_platform.logging_config import get_logger
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolResult, ToolSpec

logger = get_logger(__name__)

#: 支持的协议版本（新 → 旧）。客户端带来哪个就协商哪个；都不认识时回最新。
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 标准错误码（MCP 复用）
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


def mcp_tool_name(spec_name: str) -> str:
    """ToolSpec 名 → MCP 工具名。

    MCP 规范要求工具名匹配 ^[a-zA-Z0-9_-]{1,128}$，不允许点号；
    平台的命名空间习惯是 `namespace.action`，统一把 `.` 映射为 `__`
    （双下划线，避免单点/单下划线两种写法撞名）。
    """
    return spec_name.replace(".", "__")


class MCPToolServer:
    """把 ToolRegistry 包装成 MCP 服务器（传输无关：stdio / HTTP 共用同一套分发逻辑）。

    参数：
        registry:         平台工具注册中心
        name / version:   serverInfo（initialize 应答里返回给客户端）
        allowed:          可选的工具名白名单（复用注册中心"作用域即安全边界"的语义）
        expose_high_risk: 是否暴露 high 风险工具（默认 False，见文件头治理边界）
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        name: str = "zhudai-agent-platform",
        version: str = "1.0.0",
        allowed: set[str] | None = None,
        expose_high_risk: bool = False,
    ) -> None:
        self.registry = registry
        self.name = name
        self.version = version
        self.allowed = allowed
        self.expose_high_risk = expose_high_risk

    # ------------------------------------------------------------------ #
    # 工具描述（tools/list）
    # ------------------------------------------------------------------ #
    def _exportable_tools(self) -> list[ToolSpec]:
        tools = self.registry.all()
        if self.allowed is not None:
            tools = [t for t in tools if t.name in self.allowed]
        if not self.expose_high_risk:
            tools = [t for t in tools if t.risk_level != "high"]
        return sorted(tools, key=lambda t: t.name)

    def list_tools(self) -> list[dict[str, Any]]:
        """ToolSpec → MCP 工具描述符（name / description / inputSchema / annotations）。"""
        out: list[dict[str, Any]] = []
        for spec in self._exportable_tools():
            out.append(
                {
                    "name": mcp_tool_name(spec.name),
                    "description": spec.description,
                    "inputSchema": spec.parameters
                    or {"type": "object", "properties": {}},
                    # annotations 是给客户端 UI 看的提示位，把平台的治理元数据带出去
                    "annotations": {
                        "title": spec.name,
                        "readOnlyHint": spec.idempotent,
                        "destructiveHint": spec.risk_level == "high",
                        "idempotentHint": spec.idempotent,
                    },
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # 工具调用（tools/call）
    # ------------------------------------------------------------------ #
    def call_tool(self, mcp_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """执行一次工具调用，返回 MCP 的 tool result。

        错误分两层（协议分层，不要混）：
            - 协议错误（未知工具 / 参数不合法）→ JSON-RPC error（调用方直接抛 MCPProtocolError）
            - 执行错误（工具内部失败）→ in-band result：isError=True + 错误文本
              （MCP 规范：工具执行失败不是协议失败，模型需要看到错误内容才能自修正）
        """
        spec_name = mcp_name.replace("__", ".", 1) if "__" in mcp_name else mcp_name
        try:
            spec = self.registry.get(spec_name)
        except Exception:  # noqa: BLE001 - 未知工具统一按协议错误处理
            raise MCPProtocolError(_INVALID_PARAMS, f"Unknown tool: {mcp_name}") from None
        if spec.risk_level == "high" and not self.expose_high_risk:
            raise MCPProtocolError(
                _INVALID_PARAMS,
                f"工具 {spec.name} 为高风险动作，MCP 通道默认不开放："
                "请走平台编排链路（强制 HITL 审批），或在服务端显式配置 expose_high_risk",
            )

        result: ToolResult = self.registry.invoke(spec_name, arguments or {}, allowed=self.allowed)
        payload = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
        out: dict[str, Any] = {"content": [{"type": "text", "text": payload}]}
        if result.ok:
            out["structuredContent"] = {"ok": True, "data": result.data}
        else:
            out["isError"] = True
            out["structuredContent"] = {"ok": False, "error": result.error}
        return out

    # ------------------------------------------------------------------ #
    # JSON-RPC 分发（stdio 与 HTTP 共用）
    # ------------------------------------------------------------------ #
    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条已解析的 JSON-RPC 消息。

        返回应答（request）；通知（无 id）返回 None——**通知不需要应答**，
        这是 stdio / HTTP 两个传输层共同遵守的约定。
        """
        method = msg.get("method")
        msg_id = msg.get("id")
        if method is None:
            return self._error(msg_id, _INVALID_REQUEST, "缺少 method 字段")

        # ---- 生命周期 ----
        if method == "initialize":
            client_version = (msg.get("params") or {}).get("protocolVersion", "")
            negotiated = (
                client_version
                if client_version in SUPPORTED_PROTOCOL_VERSIONS
                else SUPPORTED_PROTOCOL_VERSIONS[0]
            )
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                    "instructions": (
                        "本服务器暴露助贷 AI 中台的工具生态；"
                        "高风险工具默认不出现在列表中（须走平台 HITL 审批链路）。"
                    ),
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        # ---- 工具能力 ----
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": self.list_tools()}}
        if method == "tools/call":
            params = msg.get("params") or {}
            tool_name = params.get("name")
            if not tool_name or not isinstance(tool_name, str):
                return self._error(msg_id, _INVALID_PARAMS, "tools/call 缺少工具名")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return self._error(msg_id, _INVALID_PARAMS, "arguments 必须是对象")
            try:
                result = self.call_tool(tool_name, arguments)
            except MCPProtocolError as exc:
                return self._error(msg_id, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - 适配层兜底，不向客户端泄漏堆栈
                logger.exception("MCP tools/call 内部错误：%s", tool_name)
                return self._error(msg_id, _INTERNAL_ERROR, f"internal error: {type(exc).__name__}")
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}

        if method.startswith("notifications/"):
            return None  # 通知：初始化完成 / 取消等，一律不应答

        return self._error(msg_id, _METHOD_NOT_FOUND, f"Method not found: {method}")

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }

    # ------------------------------------------------------------------ #
    # stdio 传输（MCP 官方传输之一：按行分隔的 JSON-RPC 消息流）
    # ------------------------------------------------------------------ #
    def serve_stdio(
        self,
        stdin: Any = None,
        stdout: Any = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> int:
        """stdio 主循环。stdin/stdout 可注入（测试用 io.StringIO）。

        返回处理的请求数。解析失败回 -32700（不中断进程——一行坏消息
        不该杀死整个服务器，这是长驻进程的基本素养）。
        """
        import sys

        reader = stdin if stdin is not None else sys.stdin
        writer = stdout if stdout is not None else sys.stdout
        handled = 0
        for line in reader:
            if stop is not None and stop():
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                resp = self._error(None, _PARSE_ERROR, f"Parse error: {exc}")
            else:
                resp = self.handle_message(msg)
            if resp is not None:
                writer.write(json.dumps(resp, ensure_ascii=False) + "\n")
                writer.flush()
            handled += 1
        return handled


class MCPProtocolError(Exception):
    """协议层错误（区别于工具执行错误，见 call_tool 的分层说明）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# =========================================================================== #
# Streamable HTTP 传输（MCP 2025-03-26 引入的 HTTP 传输）
# =========================================================================== #
def mount_mcp_http(
    app: Any,
    *,
    path: str,
    get_server: Callable[[], MCPToolServer],
) -> None:
    """把 MCP 端点挂到 FastAPI 应用（POST JSON-RPC；单请求-单响应的 JSON 模式）。

    说明：
    - 客户端必须带 `Accept: application/json, text/event-stream`（规范要求），
      服务端选择 JSON 模式应答（SSE 为可选项，本服务器是**无状态**的，
      没有 session/流式推送需求，不该为了规范条文引入 SSE 复杂度）；
    - 通知-only 的 POST 返回 202 Accepted（规范行为：无需应答也别当错误）；
    - 鉴权：端点挂在业务前缀（如 /platform/mcp）下，自动被平台的
      API Key 中间件覆盖 —— MCP 通道不设免鉴权特例。
    """

    def _decode(body: bytes) -> Any:
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError(_PARSE_ERROR, f"Parse error: {exc}") from None

    def _process(msg: dict[str, Any]) -> dict[str, Any] | None:
        return get_server().handle_message(msg)

    @app.post(path, include_in_schema=False)
    async def mcp_post(request: Request) -> Response:
        accept = request.headers.get("accept", "")
        if "application/json" not in accept and "text/event-stream" not in accept:
            return JSONResponse(
                status_code=406,
                content={"error": "Not Acceptable", "detail": "Accept 头必须包含 application/json 或 text/event-stream"},
            )
        try:
            payload = _decode((await request.body()) or b"")
        except MCPProtocolError as exc:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": exc.code, "message": exc.message}},
            )

        messages = payload if isinstance(payload, list) else [payload]
        if not messages or not all(isinstance(m, dict) for m in messages):
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": _INVALID_REQUEST, "message": "无效的 JSON-RPC 消息"}},
            )

        responses = [r for m in messages if (r := _process(m)) is not None]
        if not responses:
            # 全部是通知：按规范返回 202（已接受但无应答体）
            return Response(status_code=202)
        body: Any = responses[0] if len(responses) == 1 else responses
        return JSONResponse(content=body)

    @app.get(path, include_in_schema=False)
    def mcp_get() -> Response:
        # 无状态服务器不提供 SSE 流：明确 405，让客户端回落 POST 模式
        return JSONResponse(
            status_code=405,
            content={"error": "Method Not Allowed", "detail": "本服务器为无状态 JSON 模式，仅支持 POST"},
        )

    logger.info("MCP 端点已挂载：POST %s（Streamable HTTP，JSON 模式）", path)


__all__ = [
    "MCPProtocolError",
    "MCPToolServer",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "mcp_tool_name",
    "mount_mcp_http",
]
