"""平台 API 层对外出口。

约定：每个服务的路由模块暴露一个 `router`，本包把它导出为 `platform_router`
以区别于 project/product/coaching 的路由，避免同名冲突。
"""

from agent_platform.api.routers import router as platform_router

__all__ = ["platform_router"]
