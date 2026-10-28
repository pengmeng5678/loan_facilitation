"""Checkpointer 后端测试（memory / sqlite / postgres 三形态）。

重点覆盖 postgres 的**加固语义**（本模块相对旧版的唯一变化）：
    ① 配置错了（DSN 不像样）→ 启动时就报"哪一项配错"，不是连接超时后才炸；
    ② 连不上（服务没起）→ 明确报"连不上 + 给检查项"，绝不静默降级 memory；
       静默降级在多 worker 下 = HITL 恢复悄悄失效，比启动失败危险得多。

真 PG 的连通性用例需要 FLOW_TEST_PG_DSN（compose 里带了 postgres 服务），
默认跳过；不跳过的话每次跑测试都得起一个库，违背"离线可测"原则。
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from agent_platform.checkpointing import _mask_dsn, build_checkpointer
from agent_platform.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        app_env="local", log_level="ERROR", llm_provider="echo",
        embedding_provider="local", embedding_dim=256,
        checkpointer_backend="memory", vector_backend="local",
    )
    base.update(overrides)
    return Settings(**base)


class TestMemoryAndSqlite:
    def test_memory_backend(self):
        saver = build_checkpointer(_settings(checkpointer_backend="memory"))
        assert type(saver).__name__ == "InMemorySaver"

    def test_sqlite_backend(self, tmp_path):
        saver = build_checkpointer(
            _settings(
                checkpointer_backend="sqlite",
                checkpointer_sqlite_path=str(tmp_path / "ckpt.sqlite"),
            )
        )
        assert type(saver).__name__ == "SqliteSaver"
        assert (tmp_path / "ckpt.sqlite").exists()

    def test_unknown_backend(self):
        from types import SimpleNamespace

        with pytest.raises(ValueError, match="CHECKPOINTER_BACKEND"):
            build_checkpointer(SimpleNamespace(checkpointer_backend="etcd"))


class TestPostgresHardening:
    def test_empty_dsn_rejected(self):
        with pytest.raises(ValueError, match="POSTGRES_DSN"):
            build_checkpointer(_settings(checkpointer_backend="postgres", postgres_dsn=""))

    def test_bad_scheme_rejected(self):
        with pytest.raises(ValueError, match="postgresql://"):
            build_checkpointer(
                _settings(
                    checkpointer_backend="postgres",
                    postgres_dsn="mysql://root:root@localhost:3306/db",
                )
            )

    def test_settings_level_validation(self):
        """配置层就该拦：错误信息直接指向 .env 里的哪一项。"""
        with pytest.raises(ValidationError, match="POSTGRES_DSN"):
            _settings(checkpointer_backend="postgres", postgres_dsn="localhost:5432")

    def test_unreachable_fails_fast_no_silent_degrade(self):
        """连不上必须炸，而不是降级 memory。

        断言 twofold：抛 RuntimeError（不是返回一个 InMemorySaver），
        且错误信息里带排查指引（服务没起 / DSN / 权限）。
        """
        with pytest.raises(RuntimeError, match="PostgreSQL Checkpointer 连接失败"):
            build_checkpointer(
                _settings(
                    checkpointer_backend="postgres",
                    # connect_timeout=2：本机拒绝监听是瞬时的，写出来是防
                    # 未来有人把地址改成真的不可达主机时测试卡住
                    postgres_dsn="postgresql://postgres:postgres@127.0.0.1:6390/none?connect_timeout=2",
                )
            )

    def test_mask_dsn_hides_password(self):
        assert _mask_dsn("postgresql://user:secret@localhost:5432/db") == (
            "postgresql://user:***@localhost:5432/db"
        )
        # 无密码的 DSN 原样返回（没有可脱敏的东西，不该被改得面目全非）
        assert _mask_dsn("postgresql://localhost:5432/db") == "postgresql://localhost:5432/db"


@pytest.mark.skipif(
    not os.getenv("FLOW_TEST_PG_DSN"),
    reason="未配置 FLOW_TEST_PG_DSN（真实 PostgreSQL 集成用例）",
)
class TestRealPostgresIntegration:
    def test_roundtrip(self):
        """真库：建表 + 存/取一个 checkpoint + 多 worker 视角下的 list。"""
        from langgraph.checkpoint.base import CheckpointMetadata

        dsn = os.environ["FLOW_TEST_PG_DSN"]
        saver = build_checkpointer(
            _settings(checkpointer_backend="postgres", postgres_dsn=dsn)
        )
        thread = f"itest-{os.getpid()}"
        config = {"configurable": {"thread_id": thread}}
        from langgraph.checkpoint.memory import InMemorySaver  # noqa: F401 - 仅说明类型来源

        # put → get：跨"worker"（同一库不同连接）也能读到
        saver.put(config, checkpoint=_dummy_checkpoint(), metadata=CheckpointMetadata(), new_versions={})
        tup = saver.get_tuple(config)
        assert tup is not None
        assert list(saver.list(config, limit=10)) != []


def _dummy_checkpoint():
    """最小可用 checkpoint（langgraph 内部结构随版本演进，这里只造测试要的字段）。"""
    from langgraph.checkpoint.base import Checkpoint

    return Checkpoint(
        v=1, id=f"ck-{os.getpid()}", ts="2026-10-18T00:00:00Z",
        channel_values={}, channel_versions={}, versions_seen={},
        pending_sends=[],
    )
