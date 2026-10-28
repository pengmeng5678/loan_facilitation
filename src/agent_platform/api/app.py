"""FastAPI 应用入口。

    uvicorn --app-dir src agent_platform.api.app:app --reload

六个服务挂载在同一进程的不同前缀下：
    /platform  项目五 编排平台（Agent 管理、运行、审批、观测）
    /data      项目四 对话数据中台（抽取、清洗脱敏、审核、导出、知识产物）
    /product   项目二 产品知识助手（问答、匹配、方案书）
    /coaching  项目三 话术陪练与考核（陪练、质检、话术挖掘）
    /marketing 项目七 素材推广与客户激活（素材、案例、激活、合规、归因）
    /voice     项目一 实时语音客服（进房凭证、云端配置、RTC 回调、内容安全）
    /flow      项目八 流程数字员工（线索 SOP、到店前准备、费用预估、HITL 审批、审计）
    /gateway   项目六 推理网关与评测（路由、成本、回放、评测准入）
生产也可按前缀拆成多个服务分别部署 —— 平台与业务本来就是解耦的。

API 文档：http://localhost:8000/docs
    Swagger UI 的静态资源随代码一起发货（见文件末尾「离线 API 文档」一节），
    国内网络打不开 cdn.jsdelivr.net 也能正常显示。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_platform.api.auth import install_api_key_auth
from agent_platform.api.metrics import router as metrics_router
from agent_platform.api.routers import router as platform_router
from agent_platform.config import get_settings
from agent_platform.errors import AgentPlatformError
from agent_platform.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# 离线 API 文档
# --------------------------------------------------------------------------- #
# FastAPI 默认的 /docs 会从 cdn.jsdelivr.net 拉 swagger-ui 的 JS/CSS。
# 国内网络下这个 CDN 经常连不上，表现是：**页面标题出来了，正文一片空白** ——
# HTML 是本地返回的（所以标签页标题正常），但 <script> 永远加载失败，
# SwaggerUIBundle 未定义，div#swagger-ui 里自然什么都不渲染。
# 浏览器 F12 → Console 会看到 "Uncaught ReferenceError: SwaggerUIBundle is not defined"。
#
# 所以把 swagger-ui-dist@5.17.14 的静态资源随代码一起发货（Apache-2.0，
# 许可证见 static/swagger-ui/swagger-ui-bundle.js.LICENSE.txt），
# 由本进程自己的 /static 提供 —— 零外网也能看文档，与本项目"离线可跑"的原则一致。
# 升级：整体替换 static/swagger-ui/ 下的文件，并同步改下面的版本号。
# --------------------------------------------------------------------------- #
_SWAGGER_UI_VERSION = "5.17.14"
_API_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _API_DIR / "static"
_SWAGGER_DIR = _STATIC_DIR / "swagger-ui"
_DOCS_URL = "/docs"
_STATIC_URL = "/static"

#: Web 工作台（web/ 目录，Vite 构建产物）。构建后由本进程服务于 /ui ——
#: 一条 uvicorn 命令同时起 API 与控制台，部署形态最简。
#: 没构建（纯后端开发）时整个挂载静默跳过，不影响任何 API 行为。
_WEB_DIST_DIR = _API_DIR.resolve().parents[2] / "web" / "dist"
_WEB_URL = "/ui"


# =========================================================================== #
# 健康检查与根路径
# =========================================================================== #
health_router = APIRouter(tags=["ops"])


@health_router.get("/health", summary="健康检查（供 K8s / compose 探针使用）")
def health() -> dict[str, str]:
    return {"status": "ok"}


@health_router.get("/ready", summary="就绪检查（确认依赖已装配）")
def ready() -> dict[str, Any]:
    """就绪探测：确认平台与各业务服务都已装配完成。

    **刻意不返回异常详情**：这个接口通常暴露在公网/探针下，
    `{type}: {message}` 会把内部结构（库路径、依赖名、报错文本）送给
    任何调用方 —— 排障看服务端日志（下面已经打了完整堆栈），
    响应体只回答"好/没好"，这才是探针该有的信息量。
    """
    try:
        from app_context import get_app_context

        ctx = get_app_context()
        return {"status": "ready", **ctx.summary()}
    except Exception:  # noqa: BLE001
        logger.exception("就绪检查：应用装配失败（详情仅记录在服务端日志）")
        return {"status": "not_ready"}


@health_router.get("/", summary="服务信息")
def index() -> dict[str, Any]:
    settings = get_settings()
    return {
        "service": "助贷 AI 中台",
        "version": "1.0.0",
        "docs": "/docs",
        "web": _WEB_URL if _WEB_DIST_DIR.is_dir() else None,
        "endpoints": {
            "platform": getattr(settings, "api_prefix_platform", "/platform"),
            "data": getattr(settings, "api_prefix_data", "/data"),
            "product": "/product",
            "coaching": "/coaching",
            "marketing": settings.api_prefix_marketing,
            "gateway": settings.api_prefix_gateway,
            "voice": settings.api_prefix_voice,
            "flow": settings.api_prefix_flow,
        },
    }


def _mount_web_dist(app: FastAPI) -> None:
    """把 Vite 构建产物挂到 /ui（存在才挂）。

    设计取舍：
    * 挂在 /ui 而不是 /：根路径的 JSON 服务信息保留，语义不冲突；
    * base 路径由 web/vite.config.ts 的 `base: "/ui/"` 保证一致；
    * dist 不存在（纯后端开发 / CI 未构建前端）时静默跳过 ——
      API 与文档行为完全不受影响。
    """
    if not _WEB_DIST_DIR.is_dir():
        logger.info(
            "Web 工作台：未找到构建产物 %s（npm run build 后访问 /ui）", _WEB_DIST_DIR
        )
        return
    app.mount(_WEB_URL, StaticFiles(directory=str(_WEB_DIST_DIR), html=True), name="web")
    logger.info("Web 工作台：已挂载 %s → %s", _WEB_URL, _WEB_DIST_DIR)


def _mount_docs(app: FastAPI) -> None:
    """挂载 `/static` 与 `/docs`，让 API 文档不依赖外网 CDN。

    设计取舍：
    * 本地资源存在 → 全部走本地（默认情况，零外网）；
    * 本地资源缺失（打包漏带 `static/`、或以某种方式裁剪了静态文件）→
      自动回退到 CDN 并打 warning。宁可依赖外网，也不要让 `/docs` 直接 500，
      因为"文档打不开"会让人误以为服务挂了。
    """
    bundle = _SWAGGER_DIR / "swagger-ui-bundle.js"
    stylesheet = _SWAGGER_DIR / "swagger-ui.css"
    offline = bundle.is_file() and stylesheet.is_file()

    if offline:
        # 注意：StaticFiles 只是挂在 /static 下，不会抢占业务前缀
        app.mount(_STATIC_URL, StaticFiles(directory=str(_STATIC_DIR)), name="static")
        js_url = f"{_STATIC_URL}/swagger-ui/swagger-ui-bundle.js"
        css_url = f"{_STATIC_URL}/swagger-ui/swagger-ui.css"
        favicon_url = f"{_STATIC_URL}/swagger-ui/favicon.png"
        logger.info("API 文档：使用本地 Swagger UI（v%s，不依赖外网）", _SWAGGER_UI_VERSION)
    else:
        cdn = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{_SWAGGER_UI_VERSION}"
        js_url = f"{cdn}/swagger-ui-bundle.js"
        css_url = f"{cdn}/swagger-ui.css"
        favicon_url = "https://fastapi.tiangolo.com/img/favicon.png"
        logger.warning(
            "API 文档：未找到本地静态资源 %s，回退到 CDN（需要外网；国内网络可能白屏）",
            _SWAGGER_DIR,
        )

    @app.get(_DOCS_URL, include_in_schema=False)
    def swagger_ui() -> HTMLResponse:
        """Swagger UI —— 可以直接 "Try it out" 调真实接口。"""
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            swagger_js_url=js_url,
            swagger_css_url=css_url,
            swagger_favicon_url=favicon_url,
            # 折叠过长的 schema，接口多的时候首屏更清爽
            swagger_ui_parameters={"docExpansion": "list", "defaultModelsExpandDepth": 0},
        )


# =========================================================================== #
# 应用工厂
# =========================================================================== #
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时预热装配（避免第一个请求承担初始化开销），关闭时释放资源。"""
    configure_logging()
    scheduler = None
    try:
        from app_context import get_app_context

        ctx = get_app_context()
        logger.info("服务启动完成：%s", ctx.summary())
        # SOP 调度器（项目八）：没配 FLOW_SCHEDULER_ENABLED 时是 None，
        # 这里就是"定时扫描漏跟进"在生产里唯一的启动位置
        scheduler = ctx.flow_scheduler
        if scheduler is not None:
            scheduler.start()
    except Exception:  # noqa: BLE001 - 装配失败不阻塞进程，通过 /ready 暴露问题
        logger.exception("应用装配失败，/ready 将返回 not_ready")
    yield
    if scheduler is not None:
        await scheduler.stop()
    logger.info("服务关闭")


def create_app() -> FastAPI:
    """构建 FastAPI 应用。"""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="助贷 AI 中台",
        description=(
            "企业多智能体编排平台（项目五）+ 助贷产品知识助手与智能方案匹配（项目二）"
            "+ 助贷对话数据中台（项目四）"
            "+ 业务员话术陪练与 AI 考核（项目三）"
            "+ 销售素材推广与客户激活 AI（项目七）"
            "+ 大模型推理网关与 Agent 可观测评测平台（项目六）"
            "+ AI 实时语音对话智能客服（项目一）"
            "+ 助贷业务流程数字员工（项目八）"
        ),
        version="1.0.0",
        lifespan=lifespan,
        # 关掉 FastAPI 自带的那套（它写死了 jsdelivr CDN），改用下面的离线实现。
        # Redoc 同理需要外网加载 redoc.standalone.js，这里一并关掉：
        # 只保留 /docs + /openapi.json，少一个会白屏的入口比多一个好。
        docs_url=None,
        redoc_url=None,
    )

    # ---- 统一异常处理：把平台异常映射成结构化 HTTP 错误 ----
    @app.exception_handler(AgentPlatformError)
    async def _platform_error_handler(request: Request, exc: AgentPlatformError) -> JSONResponse:
        logger.warning("平台异常 %s: %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=400,
            content={"error": type(exc).__name__, "detail": str(exc)},
        )

    # ---- 路由挂载 ----
    app.include_router(health_router)
    # /metrics：Prometheus 拉取式监控（白名单路径，见 api/auth.py 说明）
    app.include_router(metrics_router)

    prefix_platform = getattr(settings, "api_prefix_platform", "/platform")
    app.include_router(platform_router, prefix=prefix_platform)

    # ---- MCP 端点（ToolSpec 生态 → 标准 MCP 服务器，见 mcp_adapter.py）----
    # 延迟取注册中心：装配在 lifespan 里完成，路由注册时 ctx 还没建好。
    # 端点在 /platform 前缀下，自动被 API Key 中间件覆盖 —— MCP 通道不设免鉴权特例。
    try:
        from agent_platform.mcp_adapter import MCPToolServer, mount_mcp_http

        def _get_registry() -> Any:
            from app_context import get_app_context

            return get_app_context().runtime.tools

        def _get_server() -> MCPToolServer:
            # 服务器对象无状态，按请求构建即可（注册中心每次现取，工具动态注册立即可见）
            return MCPToolServer(_get_registry(), name="zhudai-agent-platform")

        mount_mcp_http(app, path=f"{prefix_platform}/mcp", get_server=_get_server)
    except Exception:  # noqa: BLE001
        logger.exception("MCP 端点加载失败")

    # 业务路由延迟导入：即使某个业务包有问题，平台本身仍可起（可观测性优先）
    try:
        from data_platform.api import router as data_router

        app.include_router(data_router)
    except Exception:  # noqa: BLE001
        logger.exception("数据中台路由加载失败")

    try:
        from product_assistant.api import router as product_router

        app.include_router(product_router)
    except Exception:  # noqa: BLE001
        logger.exception("产品知识助手路由加载失败")

    try:
        from coaching.api import router as coaching_router

        app.include_router(coaching_router)
    except Exception:  # noqa: BLE001
        logger.exception("话术陪练路由加载失败")

    try:
        from marketing.api import router as marketing_router

        app.include_router(marketing_router)
    except Exception:  # noqa: BLE001
        logger.exception("素材推广路由加载失败")

    try:
        from gateway.api import router as gateway_router

        app.include_router(gateway_router)
    except Exception:  # noqa: BLE001
        logger.exception("推理网关路由加载失败")

    try:
        from voice_service.api import router as voice_router

        app.include_router(voice_router)
    except Exception:  # noqa: BLE001
        logger.exception("语音客服路由加载失败")

    try:
        from flow_ops.api import router as flow_router

        app.include_router(flow_router)
    except Exception:  # noqa: BLE001
        logger.exception("流程数字员工路由加载失败")

    # ---- API 鉴权（默认拒绝 + 显式白名单，见 api/auth.py 文件头）----
    # 装在最后：中间件的注册顺序与路由无关（Starlette 的 add_middleware 是后注册的先执行），
    # 但放在路由之后语义上更直观 —— "先有路由，再决定谁能访问"。
    install_api_key_auth(app, settings)

    # ---- API 文档（本地静态资源，不依赖外网 CDN）----
    _mount_docs(app)

    # ---- Web 工作台（web/dist 存在时挂到 /ui）----
    _mount_web_dist(app)

    return app


app = create_app()
