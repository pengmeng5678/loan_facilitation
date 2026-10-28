"""状态持久化（Checkpointer）工厂。

对应简历里的"有状态编排引擎"：
    LangGraph 的 Checkpointer 负责"线程级状态快照"——每执行完一个节点就落一次盘。
    于是长任务可以：
        - 中断后从断点恢复（用户关掉页面再回来接着聊）；
        - 高风险节点挂起等人工审批，审批后 resume；
        - 出问题时可回放完整决策轨迹（审计）。

**三种后端**：
    memory    进程内，重启即失，仅用于测试
    sqlite    单文件落盘，零依赖，本地/单机生产可用（默认）
    postgres  多实例共享状态，真正的生产部署形态

**为什么 postgres 分支不再"失败就降级 memory"**：
    HITL 审批的恢复语义是"A 把请求挂起，B 拿着同一个 thread_id resume"。
    多 worker 部署下如果 checkpointer 悄悄降级成 memory，每个 worker 各有一份
    互不相通的状态 —— 表现是"审批通过了，Agent 却像没发生过一样重来"，
    而这种故障没有任何报错，只有业务结果错。**宁可启动失败，不能静默丢状态。**
"""

from __future__ import annotations

from typing import Any

from agent_platform.config import Settings, get_settings
from agent_platform.logging_config import get_logger

logger = get_logger(__name__)

#: 合法的 PG DSN scheme（psycopg 两种都认）
_PG_SCHEMES = ("postgresql://", "postgres://")


def _validate_pg_dsn(dsn: str) -> str:
    """校验 DSN 形态。配错了就现在炸，不要等连接超时。"""
    dsn = (dsn or "").strip()
    if not dsn:
        raise ValueError(
            "CHECKPOINTER_BACKEND=postgres 但未配置 POSTGRES_DSN。"
            "示例：postgresql://postgres:postgres@localhost:5432/zhudai_ai"
        )
    if not dsn.startswith(_PG_SCHEMES):
        raise ValueError(
            f"POSTGRES_DSN 格式不正确：{dsn!r}。"
            "必须以 postgresql:// 或 postgres:// 开头"
            "（示例：postgresql://user:pass@host:5432/dbname）"
        )
    return dsn


def build_checkpointer(settings: Settings | None = None) -> Any:
    """按配置构建 Checkpointer。

    三种后端：
        memory   —— 进程内，重启即失，仅用于测试
        sqlite   —— 单文件落盘，零依赖，本地/单机生产可用（默认）
        postgres —— 多实例共享状态，真正的生产部署形态
    """
    settings = settings or get_settings()
    backend = settings.checkpointer_backend

    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("Checkpointer：memory（进程内，重启丢状态）")
        return InMemorySaver()

    if backend == "sqlite":
        try:
            # langgraph-checkpoint 自带 sqlite saver（新版模块路径）
            from langgraph.checkpoint.sqlite import SqliteSaver

            path = settings.checkpoint_sqlite_abs_path
            path.parent.mkdir(parents=True, exist_ok=True)
            # 注意：SqliteSaver 需要一个长连接的 sqlite3 连接，不能自动关闭
            import sqlite3

            conn = sqlite3.connect(str(path), check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()  # 建表（幂等）
            logger.info("Checkpointer：sqlite（%s）", path)
            return saver
        except Exception as exc:  # noqa: BLE001 - 环境缺 sqlite saver 时优雅降级
            logger.warning("sqlite checkpointer 初始化失败（%s），降级为 memory", exc)
            from langgraph.checkpoint.memory import InMemorySaver

            return InMemorySaver()

    if backend == "postgres":
        return _build_postgres_saver(settings)

    raise ValueError(f"未知的 CHECKPOINTER_BACKEND：{backend}")


def _build_postgres_saver(settings: Settings) -> Any:
    """构建 PostgresSaver（多 worker 共享状态）。

    三个失败点全部 fail-fast，错误信息直接给出修复动作：
        ① DSN 形态不对      → ValueError（配置错误，改 .env 就能好）
        ② 包装缺失          → RuntimeError（pip install，给命令）
        ③ 连不上 / 建表失败 → RuntimeError（服务没起，给检查项）
    """
    dsn = _validate_pg_dsn(settings.postgres_dsn)

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        raise RuntimeError(
            "CHECKPOINTER_BACKEND=postgres 需要 PostgreSQL 驱动："
            "pip install 'psycopg[binary]>=3.1' langgraph-checkpoint-postgres>=2.0"
            "（或 pip install -e '.[prod]'）"
        ) from exc

    try:
        # from_conn_string 返回上下文管理器；这里手动 __enter__ 让连接
        # 随进程生命周期常驻（退出时由操作系统回收，与 SqliteSaver 长连接同思路）
        cm = PostgresSaver.from_conn_string(dsn)
        saver = cm.__enter__()
        saver.setup()  # 建表（幂等，多 worker 并发启动也安全）
    except Exception as exc:  # noqa: BLE001 - 连接/建表失败，转成可操作的报错
        raise RuntimeError(
            f"PostgreSQL Checkpointer 连接失败：{exc}。"
            "请检查：① POSTGRES_DSN 指向的库已启动（docker compose up -d postgres）；"
            "② 账号有建表权限；③ 网络/防火墙放通了 5432。"
            "单机演示请把 CHECKPOINTER_BACKEND 改回 sqlite。"
        ) from exc

    logger.info("Checkpointer：postgres（%s，多 worker 共享状态已生效）", _mask_dsn(dsn))
    return saver


def _mask_dsn(dsn: str) -> str:
    """日志里脱敏 DSN 的密码部分（postgresql://user:pass@host → user:***@host）。"""
    if "@" not in dsn:
        return dsn  # 无认证信息，无密可脱
    scheme, _, rest = dsn.partition("://")
    userinfo, _, hostpart = rest.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return f"{scheme}://{user}:***@{hostpart}"
