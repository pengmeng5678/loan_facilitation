"""API 鉴权中间件（生产硬开关，对应"零鉴权 = 后台敞开"的修复）。

**为什么是中间件而不是逐路由加 Depends**：
七个服务前缀（/platform /product /coaching /marketing /voice /flow /gateway）
加上以后新增的业务路由，任何一个漏挂依赖就是一条无鉴权入口。
中间件在进程最外圈统一收口，**默认拒绝、显式白名单**——
漏开的代价是"多要一个 key"，漏挂的代价是"裸奔"。

**支持两种凭证**（任选其一）：
    X-API-Key: <key>            # 脚本 / 服务端调用
    Authorization: Bearer <key> # 浏览器 / Swagger 调试

**白名单（不带 key 也放行）**，以及为什么：
    /health /ready   K8s / compose 探针 —— 探针要 key 等于没有探针
    /                 服务信息（无敏感数据）
    /docs /openapi.json /static/*  交互式文档与静态资源：
                     生产通常在反代层（Nginx/APISIX）再收紧，这里放开是
                     为了让"打开 /docs 就能试接口"的调试体验不被鉴权毁掉
    /metrics          Prometheus 采集。让 scraper 带 API Key 很别扭，
                     且指标本身不含客户级敏感数据；生产应在反代层按
                     来源 IP / 内网网段收紧（与 /health 同级的运维面）
    /voice/callback/*  语音回调有**独立的、更强的**鉴权：
                     VOICE_CALLBACK_API_KEY（生产必填，见 voice_service/api.py）。
                     豁免不是因为不需要鉴权，而是它有自己的门；
                     生产不配那个 key 时 voice 服务自己会打 warning。

**默认关闭**（api_auth_enabled=False）：本地开发、单元测试、demo 零配置可跑。
生产打开方式：`API_AUTH_ENABLED=true` + `API_AUTH_KEYS=key1,key2`。

**时序安全**：密钥比较用 `hmac.compare_digest`，不因"前几位对了"
而缩短比较时间——长度泄露在密钥场景下可以接受，逐字节时序不行。
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger

logger = get_logger(__name__)

#: 完全开放的路径（探针、文档与监控采集）
_OPEN_PATHS: frozenset[str] = frozenset(
    {"/health", "/ready", "/", "/docs", "/openapi.json", "/redoc", "/metrics"}
)

#: 前缀开放（静态资源）
_OPEN_PREFIXES: tuple[str, ...] = ("/static/",)

#: 自带独立鉴权的路径（voice 回调用 VOICE_CALLBACK_API_KEY，见文件头说明）
_SELF_AUTHED_PREFIXES: tuple[str, ...] = ("/voice/callback/",)


def _extract_key(request: Request) -> str | None:
    """从请求里取凭证：先 X-API-Key，再 Authorization: Bearer。"""
    key = request.headers.get("X-API-Key")
    if key and key.strip():
        return key.strip()
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


class ApiKeyAuth:
    """密钥校验器（纯逻辑，不依赖 FastAPI，便于单测）。"""

    def __init__(self, keys: frozenset[str]) -> None:
        self._keys = keys

    @classmethod
    def from_settings(cls, settings: Settings) -> ApiKeyAuth:
        return cls(settings.api_auth_key_set())

    @property
    def enabled(self) -> bool:
        return bool(self._keys)

    def is_open(self, path: str) -> bool:
        """该路径是否豁免中间件鉴权。"""
        if path in _OPEN_PATHS:
            return True
        if any(path.startswith(p) for p in _OPEN_PREFIXES):
            return True
        return any(path.startswith(p) for p in _SELF_AUTHED_PREFIXES)

    def verify(self, provided: str | None) -> bool:
        if not provided or not self._keys:
            return False
        # compare_digest 防逐字节时序侧信道；any() 短路不影响安全性
        # （比较的是完整密钥，不是"猜出前缀就缩短比较"）
        return any(hmac.compare_digest(provided, k) for k in self._keys)


def install_api_key_auth(app: FastAPI, settings: Settings) -> ApiKeyAuth | None:
    """给 FastAPI 应用装鉴权中间件。

    返回 ApiKeyAuth 实例（便于测试断言）；未启用时返回 None 且不装中间件
    —— 本地/测试路径零开销、零行为变化。
    """
    if not settings.api_auth_enabled:
        logger.info("API 鉴权：未启用（API_AUTH_ENABLED=false），所有路径开放")
        return None

    auth = ApiKeyAuth.from_settings(settings)
    if not auth.enabled:
        # 开了开关却没配密钥 = 把所有请求挡在门外（fail-closed）。
        # 这比"当作没开"安全得多：忘配密钥时应该立刻发现，而不是悄悄裸奔。
        logger.warning(
            "API 鉴权：已启用但未配置 API_AUTH_KEYS —— 所有受保护路径将返回 401（fail-closed）"
        )

    class _ApiKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            if auth.is_open(request.url.path):
                return await call_next(request)
            if not auth.verify(_extract_key(request)):
                # 固定文案：不区分"没带"和"带错"，不提示哪个环境变量
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "detail": "缺少或无效的 API Key"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

    app.add_middleware(_ApiKeyMiddleware)
    logger.info("API 鉴权：已启用，保护全部业务前缀（白名单：探针 / 文档 / 语音回调）")
    return auth
