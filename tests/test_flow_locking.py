"""分布式锁后端测试（SQLite / Redis 双后端，全离线）。

覆盖策略 —— 锁是"可靠性"的地基，测的就是四个语义：
    ① 互斥     同一把锁第二个持有者必须失败（lock_busy）
    ② owner    A 不能释放/续约 B 的锁
    ③ 租约     持锁者"崩了"（不释放）后，TTL 到期锁自动可被抢占
    ④ 可用性   Redis 不可用 → fail-closed（503 语义），绝不静默降级

Redis 用例用 FakeRedis（实现 redis 客户端用到的 API 子集 + TTL 模拟），
不需要真跑一个 Redis 服务。真实 Redis 的连通性用例标了 integration，
设置 FLOW_TEST_REDIS_URL 环境变量才会跑。
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from flow_ops.idempotency import LockGuard
from flow_ops.leads import FlowError
from flow_ops.locking import (
    LOCK_KEY_PREFIX,
    RedisLockBackend,
    SqliteLockBackend,
    build_lock_backend,
)
from flow_ops.store import FlowStore


# =========================================================================== #
# FakeRedis：redis 客户端用到的 API 子集 + TTL 过期模拟
# =========================================================================== #
class FakeRedis:
    """最小 Redis 模拟：set(nx/px) / get / eval(两种 Lua) / delete / ping。

    TTL 用"惰性过期"模拟：每次访问时把到期的 key 当不存在处理。
    真 Redis 是后台线程定期 + 惰性删除，对锁语义来说惰性判断足够测。
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._expire_at: dict[str, float] = {}
        self._lock = threading.Lock()

    # ---- 内部 ----
    def _live(self, key: str) -> bool:
        exp = self._expire_at.get(key)
        if exp is not None and time.monotonic() >= exp:
            self._data.pop(key, None)
            self._expire_at.pop(key, None)
            return False
        return key in self._data

    # ---- API 子集 ----
    def set(self, key: str, value: str, nx: bool = False, px: int | None = None) -> bool | None:
        with self._lock:
            if nx and self._live(key):
                return None  # redis-py 对 set(nx=True) 失败返回 None
            self._data[key] = value
            if px is not None:
                self._expire_at[key] = time.monotonic() + px / 1000.0
            return True

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data[key] if self._live(key) else None

    def delete(self, key: str) -> int:
        with self._lock:
            existed = self._live(key)
            self._data.pop(key, None)
            self._expire_at.pop(key, None)
            return 1 if existed else 0

    def eval(self, script: str, numkeys: int, *args) -> int:
        """只实现本模块用到的两条 Lua 语义（按脚本内容区分）。"""
        key = args[0]
        with self._lock:
            if "PEXPIRE" in script:  # 续约
                owner, ttl_ms = args[1], int(args[2])
                if self._live(key) and self._data.get(key) == owner:
                    self._expire_at[key] = time.monotonic() + ttl_ms / 1000.0
                    return 1
                return 0
            # 释放：比 owner 再 DEL
            owner = args[1]
            if self._live(key) and self._data.get(key) == owner:
                self._data.pop(key, None)
                self._expire_at.pop(key, None)
                return 1
            return 0

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


class BrokenRedis(FakeRedis):
    """模拟 Redis 挂了：所有命令都抛连接异常。"""

    def _boom(self, *_a, **_k):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    set = _boom  # type: ignore[assignment]
    get = _boom  # type: ignore[assignment]
    eval = _boom  # type: ignore[assignment]
    ping = _boom  # type: ignore[assignment]


def _make_redis_backend(fake: FakeRedis, **kwargs) -> RedisLockBackend:
    """绕过 __init__ 里的 import/from_url，直接注入 FakeRedis。"""
    backend = RedisLockBackend.__new__(RedisLockBackend)
    backend._key_prefix = LOCK_KEY_PREFIX
    backend._client = fake
    return backend


@pytest.fixture
def store(tmp_path):
    s = FlowStore(tmp_path / "flow.sqlite")
    yield s
    s.close()


@pytest.fixture
def sqlite_backend(store):
    return SqliteLockBackend(store)


@pytest.fixture
def redis_backend():
    return _make_redis_backend(FakeRedis())


# =========================================================================== #
# 一、SQLite 后端（单机默认）
# =========================================================================== #
class TestSqliteBackend:
    def test_acquire_and_release(self, sqlite_backend):
        assert sqlite_backend.acquire("k", owner="w1", ttl_seconds=30) is True
        assert sqlite_backend.release("k", owner="w1") is True

    def test_mutual_exclusion(self, sqlite_backend):
        assert sqlite_backend.acquire("k", owner="w1", ttl_seconds=30) is True
        # 第二个持有者必须失败
        assert sqlite_backend.acquire("k", owner="w2", ttl_seconds=30) is False

    def test_release_requires_owner(self, sqlite_backend):
        sqlite_backend.acquire("k", owner="w1", ttl_seconds=30)
        # 别人不能释放我的锁
        assert sqlite_backend.release("k", owner="w2") is False
        assert sqlite_backend.acquire("k", owner="w2", ttl_seconds=30) is False

    def test_expired_lock_can_be_preempted(self, sqlite_backend):
        # TTL=0 的锁"拿到即过期"：模拟持锁者崩溃后租约到期
        assert sqlite_backend.acquire("k", owner="w1", ttl_seconds=0) is True
        assert sqlite_backend.acquire("k", owner="w2", ttl_seconds=30) is True

    def test_renew_extends_lease(self, sqlite_backend):
        sqlite_backend.acquire("k", owner="w1", ttl_seconds=1)
        assert sqlite_backend.renew("k", owner="w1", ttl_seconds=60) is True
        # 续约后锁依然在（且 owner 还是我）
        assert sqlite_backend.acquire("k", owner="w2", ttl_seconds=30) is False
        assert sqlite_backend.renew("k", owner="w2", ttl_seconds=60) is False

    def test_cross_thread_mutual_exclusion(self, store):
        """SQLite 后端同一把锁在进程内多线程间互斥（连接级线程锁 + 锁表）。

        断言两件事：
            ① 四个线程都最终拿到了锁（抢不到的会重试，模拟调用方行为）；
            ② 任意时刻持锁者 ≤ 1（互斥语义本身）。
        """
        backend = SqliteLockBackend(store)
        acquired: list[str] = []
        holders = 0
        max_holders = 0
        guard = threading.Lock()

        def worker(name: str) -> None:
            nonlocal holders, max_holders
            for _ in range(200):  # 抢不到就重试（真实调用方也是重试）
                if backend.acquire("shared", owner=name, ttl_seconds=30):
                    with guard:
                        holders += 1
                        max_holders = max(max_holders, holders)
                        acquired.append(name)
                    time.sleep(0.02)  # 临界区
                    with guard:
                        holders -= 1
                    backend.release("shared", owner=name)
                    return
                time.sleep(0.005)

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(acquired) == 4  # 都最终拿到了（串行）
        assert max_holders == 1  # 但任意时刻只有一个持有者


# =========================================================================== #
# 二、Redis 后端（多 worker 生产）
# =========================================================================== #
class TestRedisBackend:
    def test_acquire_and_release(self, redis_backend):
        assert redis_backend.acquire("k", owner="w1", ttl_seconds=30) is True
        assert redis_backend.release("k", owner="w1") is True

    def test_key_is_namespaced(self, redis_backend):
        redis_backend.acquire("k", owner="w1", ttl_seconds=30)
        # 前缀隔离：同一套 Redis 给别的业务复用也不会撞 key
        assert redis_backend._client.get(f"{LOCK_KEY_PREFIX}k") == "w1"

    def test_mutual_exclusion(self, redis_backend):
        assert redis_backend.acquire("k", owner="w1", ttl_seconds=30) is True
        assert redis_backend.acquire("k", owner="w2", ttl_seconds=30) is False

    def test_release_requires_owner(self, redis_backend):
        redis_backend.acquire("k", owner="w1", ttl_seconds=30)
        assert redis_backend.release("k", owner="w2") is False
        # 锁还在，w2 依然拿不到
        assert redis_backend.acquire("k", owner="w2", ttl_seconds=30) is False

    def test_ttl_auto_release(self, redis_backend):
        # 持锁者"崩溃"（不调 release），租约到期后锁自动可被抢占
        assert redis_backend.acquire("k", owner="w1", ttl_seconds=1) is True
        assert redis_backend.acquire("k", owner="w2", ttl_seconds=30) is False
        time.sleep(0.05)  # FakeRedis 惰性过期，t=1s 时还没到期
        assert redis_backend.acquire("k", owner="w2", ttl_seconds=30) is False
        # 直接把租约改到期，模拟"时间过去 1 秒"
        redis_backend._client._expire_at[f"{LOCK_KEY_PREFIX}k"] = time.monotonic() - 1
        assert redis_backend.acquire("k", owner="w2", ttl_seconds=30) is True

    def test_renew_requires_owner(self, redis_backend):
        redis_backend.acquire("k", owner="w1", ttl_seconds=30)
        assert redis_backend.renew("k", owner="w2", ttl_seconds=60) is False
        assert redis_backend.renew("k", owner="w1", ttl_seconds=60) is True
        assert redis_backend.acquire("k", owner="w2", ttl_seconds=30) is False

    def test_fail_closed_when_redis_down(self):
        """Redis 挂了 → 抛 lock_unavailable（503 语义），绝不静默无锁放行。"""
        backend = _make_redis_backend(BrokenRedis())
        with pytest.raises(FlowError) as exc_info:
            backend.acquire("k", owner="w1", ttl_seconds=30)
        assert exc_info.value.code == "lock_unavailable"
        with pytest.raises(FlowError) as exc_info:
            backend.release("k", owner="w1")
        assert exc_info.value.code == "lock_unavailable"

    def test_ping_reports_down(self):
        assert _make_redis_backend(BrokenRedis()).ping() is False
        assert _make_redis_backend(FakeRedis()).ping() is True


# =========================================================================== #
# 三、LockGuard 门面（两种后端同一套接口）
# =========================================================================== #
class TestLockGuard:
    def test_guard_over_sqlite(self, sqlite_backend):
        guard = LockGuard(sqlite_backend, ttl_seconds=30)
        assert guard.backend_name == "SqliteLockBackend"
        with guard.hold("k", owner="w1") as owner:
            assert owner == "w1"
            with pytest.raises(FlowError) as exc_info:
                with guard.hold("k", owner="w2"):
                    pass
            assert exc_info.value.code == "lock_busy"
        # 退出后锁已释放
        with guard.hold("k", owner="w2"):
            pass

    def test_guard_over_redis(self, redis_backend):
        guard = LockGuard(redis_backend, ttl_seconds=30)
        assert guard.backend_name == "RedisLockBackend"
        with guard.hold("k", owner="w1"):
            with pytest.raises(FlowError):
                with guard.hold("k", owner="w2"):
                    pass

    def test_guard_releases_on_exception(self, sqlite_backend):
        """临界区抛异常，锁也必须释放（否则要等 TTL 到期，白等 30 秒）。"""
        guard = LockGuard(sqlite_backend, ttl_seconds=30)
        with pytest.raises(ValueError):
            with guard.hold("k", owner="w1"):
                raise ValueError("boom")
        assert sqlite_backend.acquire("k", owner="w2", ttl_seconds=30) is True

    def test_guard_release_failure_does_not_mask_business_error(self, sqlite_backend):
        """释放失败只记日志，不覆盖临界区里的业务异常。"""

        class ExplodingRelease(SqliteLockBackend):
            def release(self, key, *, owner):  # noqa: ANN001
                raise ConnectionError("release failed")

        guard = LockGuard(ExplodingRelease(sqlite_backend._store), ttl_seconds=30)
        with pytest.raises(ValueError, match="business"):
            with guard.hold("k", owner="w1"):
                raise ValueError("business")

    def test_guard_renew(self, sqlite_backend):
        guard = LockGuard(sqlite_backend, ttl_seconds=30)
        with guard.hold("k", owner="w1") as owner:
            assert guard.renew("k", owner=owner) is True
        # 锁已释放后续约失败
        assert guard.renew("k", owner="w1") is False

    def test_guard_accepts_store_directly(self, store):
        """向后兼容：传 FlowStore 也能用（内部包 SQLite 后端）。"""
        guard = LockGuard(store, ttl_seconds=30)
        with guard.hold("k", owner="w1"):
            with pytest.raises(FlowError):
                with guard.hold("k", owner="w2"):
                    pass


# =========================================================================== #
# 三点五、看门狗自动续约（长临界区防租约过期，opt-in）
# =========================================================================== #
class TestWatchdog:
    def test_watchdog_keeps_lock_beyond_ttl(self, sqlite_backend):
        """① 开看门狗后，持有远超 TTL 的时间锁仍归自己。

        ttl=1s 却持有 2.2s：没有看门狗时这把锁早就过期可被抢走
        （TTL 到期可抢占的语义见 TestRedisBackend.test_ttl_auto_release）。
        看门狗每 ttl/3 ≈ 0.33s 续约一次，租约永远不会先于业务结束。
        """
        guard = LockGuard(sqlite_backend, ttl_seconds=1)
        ctx = guard.hold("k", owner="w1", watchdog=True)
        with ctx:
            time.sleep(2.2)
            assert ctx.lock_lost is False  # 续约一直成功
            assert sqlite_backend.acquire("k", owner="w2", ttl_seconds=30) is False
        # 退出临界区后看门狗线程必须干净停止（不留后台线程给已释放的锁续命）
        assert not any(
            t.name.startswith("flow-lock-watchdog") for t in threading.enumerate()
        )

    def test_watchdog_detects_external_lock_deletion(self, redis_backend):
        """② 锁被外部删除：下一次续约失败 → ctx.lock_lost 能感知。"""
        guard = LockGuard(redis_backend, ttl_seconds=1)
        ctx = guard.hold("k", owner="w1", watchdog=True)
        with ctx:
            redis_backend._client.delete(f"{LOCK_KEY_PREFIX}k")  # 模拟运维误删/故障清锁
            time.sleep(0.6)  # > ttl/3：保证看门狗至少走完一轮续约
            assert ctx.lock_lost is True
        # 锁已不在：别人可以直接拿走
        assert redis_backend.acquire("k", owner="w2", ttl_seconds=30) is True

    def test_watchdog_without_flag_keeps_ttl_semantics(self, sqlite_backend):
        """对照组：默认不开看门狗，ttl 到期锁照常可被抢占（不破坏既有语义）。"""
        guard = LockGuard(sqlite_backend, ttl_seconds=0)
        with guard.hold("k", owner="w1"):  # ttl=0：拿到即过期，无看门狗续命
            pass
        assert sqlite_backend.acquire("k", owner="w2", ttl_seconds=30) is True


# =========================================================================== #
# 四、工厂函数（配置 → 后端）
# =========================================================================== #
class TestBuildLockBackend:
    def _settings(self, **overrides):
        from agent_platform.config import Settings

        base = dict(
            app_env="local", log_level="ERROR", llm_provider="echo",
            embedding_provider="local", embedding_dim=256,
            checkpointer_backend="memory", vector_backend="local",
        )
        base.update(overrides)
        return Settings(**base)

    def test_default_is_sqlite(self, store):
        backend = build_lock_backend(self._settings(), store)
        assert isinstance(backend, SqliteLockBackend)

    def test_explicit_sqlite(self, store):
        backend = build_lock_backend(self._settings(flow_lock_backend="sqlite"), store)
        assert isinstance(backend, SqliteLockBackend)

    def test_redis_requires_url(self, store):
        with pytest.raises(ValueError, match="REDIS_URL"):
            build_lock_backend(self._settings(flow_lock_backend="redis", redis_url=""), store)

    def test_redis_unreachable_fails_fast(self, store):
        """启动即探测：连不上直接炸，不等第一个写请求才 503。"""
        with pytest.raises(RuntimeError, match="连接失败"):
            build_lock_backend(
                self._settings(flow_lock_backend="redis", redis_url="redis://127.0.0.1:6390/0"),
                store,
            )

    def test_unknown_backend(self, store):
        """配置层是 Literal 拦第一道；工厂自己也要拦（防直接传对象绕过）。"""
        from types import SimpleNamespace

        with pytest.raises(ValueError, match="FLOW_LOCK_BACKEND"):
            build_lock_backend(SimpleNamespace(flow_lock_backend="etcd"), store)


# =========================================================================== #
# 五、真实 Redis 集成（需要 FLOW_TEST_REDIS_URL，默认跳过）
# =========================================================================== #
@pytest.mark.skipif(
    not os.getenv("FLOW_TEST_REDIS_URL"),
    reason="未配置 FLOW_TEST_REDIS_URL（真实 Redis 集成用例）",
)
class TestRealRedisIntegration:
    def test_real_redis_roundtrip(self):
        backend = RedisLockBackend(os.environ["FLOW_TEST_REDIS_URL"])
        try:
            assert backend.ping() is True
            key = f"itest-{time.time_ns()}"
            assert backend.acquire(key, owner="w1", ttl_seconds=30) is True
            assert backend.acquire(key, owner="w2", ttl_seconds=30) is False
            assert backend.release(key, owner="w2") is False
            assert backend.release(key, owner="w1") is True
            assert backend.acquire(key, owner="w2", ttl_seconds=30) is True
            backend.release(key, owner="w2")
        finally:
            backend.close()
