"""SOP 定时调度器测试（P0-2：常驻扫描，让漏跟进在超时那一刻被发现）。

环境里没有 pytest-asyncio，所以用 `asyncio.run()` 现场驱动启停 ——
不为测试给依赖清单长胖（与 conftest "测试必须自包含"同一原则）。
调度器只依赖服务的 `check_sop()` 一个方法，测试传替身即可，
不碰真实数据库。
"""

from __future__ import annotations

import asyncio
import threading

from agent_platform.config import Settings
from flow_ops.idempotency import LockGuard
from flow_ops.locking import SqliteLockBackend
from flow_ops.scheduler import MIN_INTERVAL_SECONDS, SopScheduler, build_scheduler
from flow_ops.store import FlowStore


class FakeFlowService:
    """记录调用的 FlowOpsService 替身。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def check_sop(self, *, actor: str = "system") -> dict:
        self.calls.append(actor)
        return {"alerts": [], "count": 0, "open_total": 0}


class CountingService:
    """统计 check_sop 真实执行次数的替身（多副本防重断言用）。"""

    def __init__(self) -> None:
        self.executions = 0
        self._mutex = threading.Lock()
        #: 置位后 check_sop 会阻塞，模拟"一轮扫描正在跑"
        self.block = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()

    def check_sop(self, *, actor: str = "system") -> dict:
        with self._mutex:
            self.executions += 1
        if self.block.is_set():
            self.entered.set()
            self.release.wait(5)  # 上限兜底：测试卡死比失败更难查
        return {"alerts": [], "count": 0, "open_total": 0}


class FlakyFlowService:
    """第一轮炸、之后正常的服务（验证"单轮失败不自毙"）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def check_sop(self, *, actor: str = "system") -> dict:
        self.calls.append(actor)
        if len(self.calls) == 1:
            raise RuntimeError("database is locked")
        return {"alerts": [], "count": 0, "open_total": 0}


def _settings(**overrides) -> Settings:
    base = dict(
        app_env="local", log_level="ERROR", llm_provider="echo",
        embedding_provider="local", embedding_dim=256,
        checkpointer_backend="memory", vector_backend="local",
    )
    base.update(overrides)
    return Settings(**base)


# =========================================================================== #
# 工厂：默认不起后台任务
# =========================================================================== #
class TestBuildScheduler:
    def test_disabled_by_default(self):
        """默认关闭：本地/测试/demo 不起任何后台任务。"""
        assert build_scheduler(_settings(), FakeFlowService()) is None

    def test_enabled_returns_scheduler_with_interval(self):
        settings = _settings(
            flow_scheduler_enabled=True, flow_scheduler_interval_seconds=120
        )
        sched = build_scheduler(settings, FakeFlowService())
        assert isinstance(sched, SopScheduler)
        assert sched.interval_seconds == 120
        assert sched.running is False  # 工厂只负责建，不负责启

    def test_enabled_wires_engine_lock_guard(self):
        """生产装配：扫描互斥锁复用引擎的 LockGuard（不另建锁后端）。"""
        from types import SimpleNamespace

        engine_locks = object()  # 哨兵：只验证"接的是同一把"，不验证锁行为
        service = SimpleNamespace(
            engine=SimpleNamespace(locks=engine_locks),
            check_sop=lambda *, actor="system": {},
        )
        settings = _settings(flow_scheduler_enabled=True)
        assert build_scheduler(settings, service)._locks is engine_locks

    def test_enabled_without_engine_falls_back_to_no_lock(self):
        """替身服务没有 engine 属性：退化为无锁（单副本语义），装配不炸。"""
        settings = _settings(flow_scheduler_enabled=True)
        sched = build_scheduler(settings, FakeFlowService())
        assert sched is not None
        assert sched._locks is None


# =========================================================================== #
# 调度器行为
# =========================================================================== #
class TestSopScheduler:
    def test_interval_floor(self):
        """间隔下限 10s：别让"配置手滑"把数据库打满。"""
        sched = SopScheduler(FakeFlowService(), interval_seconds=1)
        assert sched.interval_seconds == MIN_INTERVAL_SECONDS

    def test_start_runs_immediate_sweep_then_stops(self):
        """启动后先补扫一轮（停机期间积累的逾期），stop 后循环退出。"""
        fake = FakeFlowService()
        sched = SopScheduler(fake, interval_seconds=10)

        async def _drive():
            sched.start()
            assert sched.running is True
            # 让出控制权等第一轮扫完（to_thread 需要几次事件循环轮转）
            for _ in range(50):
                if fake.calls:
                    break
                await asyncio.sleep(0.01)
            await sched.stop()

        asyncio.run(_drive())
        assert fake.calls == ["scheduler"]  # actor=scheduler：审计可区分定时/手动
        assert sched.running is False

    def test_stop_is_idempotent_and_safe_without_start(self):
        sched = SopScheduler(FakeFlowService())
        asyncio.run(sched.stop())  # 没启动过就停：不抛
        assert sched.running is False

        async def _drive():
            sched.start()
            await asyncio.sleep(0.01)
            await sched.stop()
            await sched.stop()  # 重复停：不抛

        asyncio.run(_drive())
        assert sched.running is False

    def test_sweep_failure_does_not_propagate(self):
        """IM/数据库抖一轮，扫描返回空结果而不是把调用方炸掉。"""
        flaky = FlakyFlowService()
        sched = SopScheduler(flaky)

        async def _drive():
            first = await sched.sweep_once()
            second = await sched.sweep_once()
            return first, second

        first, second = asyncio.run(_drive())
        assert first == {}                 # 失败轮：吞掉
        assert second["count"] == 0        # 下一轮：照常工作
        assert len(flaky.calls) == 2

    def test_sweep_after_stop_is_skipped(self):
        """停止信号已置位：不再发起新扫描（停机过程不添乱）。"""
        fake = FakeFlowService()
        sched = SopScheduler(fake)

        async def _drive():
            await sched.stop()
            return await sched.sweep_once()

        assert asyncio.run(_drive()) == {}
        assert fake.calls == []


# =========================================================================== #
# 多副本防重（扫描互斥短租约）
# =========================================================================== #
class TestSweepMutex:
    def _guard(self, tmp_path):
        """两个"副本"共享的锁后端（同一 SQLite 锁表 = 同一互斥域）。"""
        store = FlowStore(tmp_path / "sop-sweep.sqlite")
        return store, LockGuard(SqliteLockBackend(store), ttl_seconds=30)

    def test_two_replicas_same_moment_only_one_sweeps(self, tmp_path):
        """两个调度器同跑：同一时刻只有一轮扫描真正执行（计数器断言）。

        为什么要手动卡住第一轮：`sweep_once` 抢锁后 `await to_thread(...)`，
        而 to_thread 对"快到几乎瞬时"的任务可能**不让出事件循环**
        （Python 3.13 实测如此）—— 两个副本就变成串行执行，第二个来抢锁时
        第一个早就释放了，测试永远测不到互斥。所以这里让 A 的临界区阻塞，
        保证 B **一定在 A 持锁期间**尝试抢锁，断言才和"多副本同时扫"的
        真实场景等价（否则测的是调度顺序，不是锁）。
        """
        store, guard = self._guard(tmp_path)
        try:
            svc = CountingService()
            svc.block.set()
            replica_a = SopScheduler(svc, interval_seconds=300, lock_guard=guard)
            replica_b = SopScheduler(svc, interval_seconds=300, lock_guard=guard)

            async def _drive():
                task_a = asyncio.create_task(replica_a.sweep_once())
                # 线程里等 A 真正进入临界区，再从事件循环发起 B
                await asyncio.to_thread(svc.entered.wait, 5)
                result_b = await replica_b.sweep_once()
                svc.release.set()
                return await task_a, result_b

            result_a, result_b = asyncio.run(_drive())
            assert result_a  # A 扫了
            assert result_b == {}  # B 撞上锁，本轮跳过
            assert svc.executions == 1
        finally:
            store.close()

    def test_lock_released_after_round_next_replica_takes_over(self, tmp_path):
        """上一轮结束即释放锁（with 的 finally 语义）：另一副本下一轮能接管。"""
        store, guard = self._guard(tmp_path)
        try:
            svc = CountingService()
            replica_a = SopScheduler(svc, interval_seconds=300, lock_guard=guard)
            replica_b = SopScheduler(svc, interval_seconds=300, lock_guard=guard)

            async def _drive():
                first = await replica_a.sweep_once()
                second = await replica_b.sweep_once()
                return first, second

            asyncio.run(_drive())
            assert svc.executions == 2  # 两轮都真正执行了（先后串行，互不吞掉）
        finally:
            store.close()

    def test_sweep_lock_ttl_is_half_interval_capped_at_60(self):
        """租约 = min(间隔一半, 60s)：副本崩溃后另一副本最多等这么久接管。

        大间隔不能把租约拖大（崩溃接管白等），小间隔也不能没有租约
        （防重窗口至少覆盖一轮扫描）。
        """
        svc = CountingService()
        assert SopScheduler(svc, interval_seconds=300)._sweep_lock_ttl == 60
        assert SopScheduler(svc, interval_seconds=600)._sweep_lock_ttl == 60
        assert SopScheduler(svc, interval_seconds=10)._sweep_lock_ttl == 5
        assert SopScheduler(svc, interval_seconds=10).interval_seconds == MIN_INTERVAL_SECONDS

    def test_scheduler_without_lock_guard_keeps_single_replica_behavior(self):
        """lock_guard=None（测试替身/旧调用方）：不加锁，扫描照常执行。"""
        svc = CountingService()
        sched = SopScheduler(svc, interval_seconds=300)
        assert asyncio.run(sched.sweep_once())["count"] == 0
        assert svc.executions == 1


# =========================================================================== #
# 装配层集成
# =========================================================================== #
def test_scheduler_absent_in_default_context(app_context_fixture):
    """默认配置装配出的上下文不带调度器，/ready 的 summary 也如实反映。"""
    assert app_context_fixture.flow_scheduler is None
    assert app_context_fixture.summary()["flow_scheduler"] is None


def test_context_builds_but_does_not_start_scheduler(settings, checkpointer):
    """打开开关：装配层建调度器（不启动——start 要事件循环，归 lifespan）。"""
    import app_context as ac

    settings.flow_scheduler_enabled = True
    settings.flow_scheduler_interval_seconds = 30
    ctx = ac.build_app_context(settings=settings, checkpointer=checkpointer)
    try:
        assert ctx.flow_scheduler is not None
        assert ctx.flow_scheduler.running is False
        assert ctx.flow_scheduler.interval_seconds == 30
        summary = ctx.summary()["flow_scheduler"]
        assert summary["enabled"] is True and summary["running"] is False
    finally:
        ctx.flow_ops.close()
        ctx.gateway_service.close()


# =========================================================================== #
# FastAPI lifespan 集成（生产启动路径）
# =========================================================================== #
def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    """`with TestClient(app)` 触发 lifespan：进上下文启动、出上下文停止。

    这是"定时扫描"在生产里唯一的启动位置，必须锁死：
    哪天有人把 lifespan 里的两行删了，扫描就静默退化成手动模式。
    """
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app_context as ac
    from agent_platform.api import app as app_module

    fake = FakeFlowService()
    sched = SopScheduler(fake, interval_seconds=10)
    ctx = SimpleNamespace(
        summary=lambda: {"agents": [], "tools": 0}, flow_scheduler=sched
    )
    monkeypatch.setattr(ac, "get_app_context", lambda: ctx)
    monkeypatch.setattr(
        app_module, "get_settings",
        lambda: _settings(log_level="WARNING"),
    )

    with TestClient(app_module.create_app()) as client:
        assert client.get("/health").status_code == 200
        assert sched.running is True
    assert sched.running is False
