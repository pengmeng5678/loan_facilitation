"""分布式锁后端（多 worker 部署的互斥保障）。

**为什么需要它**：SQLite 版锁（store.try_acquire_lock）只在本进程内有效 ——
多个 worker 进程各连各的 SQLite 文件时，"锁"对彼此完全不可见，
两个 worker 可以同时推进同一条线索（重复写预约、重复发通知）。
单机多线程够用，多进程必须换 Redis。

**两种后端，一套接口**：
    sqlite  默认。零外部依赖，单机/本地/测试直接用（FlowStore 自带的锁表）。
    redis   生产多 worker。SET NX PX 抢占 + Lua owner 校验释放 + 续约。

**为什么 Redis 不可用时选择 fail-closed（抛错）而不是降级 SQLite**：
    降级会给人"锁还在工作"的错觉，而跨进程互斥其实已经失效 ——
    这比明确失败危险得多。宁可返回 503 让调用方重试，也不能静默失去保护。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from agent_platform.logging_config import get_logger
from flow_ops.leads import FlowError

logger = get_logger(__name__)

#: Redis key 前缀。带命名空间是为了同一套 Redis 还能给别的业务用（缓存、限流），
#: 彼此不撞 key。
LOCK_KEY_PREFIX = "flow:lock:"

#: 默认后端。sqlite = 离线/单机零依赖；redis = 多 worker 生产。
DEFAULT_BACKEND = "sqlite"

# --------------------------------------------------------------------------- #
# Lua 脚本：释放 / 续约都必须校验 owner
# --------------------------------------------------------------------------- #
# 为什么必须用 Lua 而不是 "GET 比一下再 DEL"：两条命令之间可能被别的客户端
# 插入动作（锁刚好过期被抢、再删就把别人的锁放了）。Lua 在 Redis 里原子执行，
# 比较和删除之间没有窗口。这是 Redlock 论文之后业界的标准做法。
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""


@runtime_checkable
class LockBackend(Protocol):
    """锁后端协议。三个方法都是同步的（临界区只有几十毫秒，不值得上异步）。"""

    def acquire(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        """尝试加锁。成功 True；被占用 / 不可用分别由调用方区分。"""
        ...

    def release(self, key: str, *, owner: str) -> bool:
        """释放锁（owner 不匹配返回 False，绝不误放）。"""
        ...

    def renew(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        """续约（owner 不匹配 / 锁已不在 → False）。"""
        ...

    def close(self) -> None:
        """释放底层连接（可选实现）。"""
        ...


class SqliteLockBackend:
    """SQLite 锁后端（包一层 FlowStore，让接口统一）。

    与直接调 store.try_acquire_lock 完全等价 —— 包一层的价值是
    LockGuard 只需要认识 LockBackend 一种东西，后端可替换。
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def acquire(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        return bool(self._store.try_acquire_lock(key, owner=owner, ttl_seconds=ttl_seconds, now=time.time()))

    def release(self, key: str, *, owner: str) -> bool:
        return bool(self._store.release_lock(key, owner=owner))

    def renew(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        # SQLite 续约 = 原地续期（owner 不匹配不动别人的锁）
        return bool(self._store.renew_lock(key, owner=owner, ttl_seconds=ttl_seconds, now=time.time()))

    def close(self) -> None:  # noqa: B027 - store 归 service 所有，这里不关
        """刻意空实现：FlowStore 的生命周期由 FlowOpsService 管理。"""


class RedisLockBackend:
    """Redis 锁后端（多 worker 生产形态）。

    三句话说明白实现：
        ① 加锁  = SET key owner NX PX ttl  —— NX 保证只有一个客户端能成功，
           PX 给锁上租约，持锁者崩了也不会死锁；
        ② 释放  = Lua 脚本"比 owner 再 DEL" —— 原子，不会误放别人的锁；
        ③ 续约  = Lua 脚本"比 owner 再 PEXPIRE" —— 长任务可以边跑边续租。

    连接参数（超时）全部显式：锁路径上的任何网络等待都会拖住 HTTP 请求，
    宁快失败重试，不要慢成功。
    """

    def __init__(
        self,
        redis_url: str,
        *,
        key_prefix: str = LOCK_KEY_PREFIX,
        connect_timeout: float = 1.0,
        socket_timeout: float = 1.0,
    ) -> None:
        try:
            import redis as redis_lib
        except ImportError as exc:  # pragma: no cover - 取决于部署环境
            raise RuntimeError(
                "flow_lock_backend=redis 需要 redis 客户端：pip install redis"
            ) from exc
        self._key_prefix = key_prefix
        # decode_responses=True：GET 回来直接是 str，Lua 比较不需要自己 decode
        self._client = redis_lib.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _full_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    def _guard(self, op: str, exc: Exception, key: str) -> FlowError:
        """连接类异常 → fail-closed 的领域错误。

        503 的语义是"服务暂时不可用，请重试"—— 对锁来说这正是想要的行为：
        重试时幂等键没变，重复提交会被幂等保护拦住，不会双写。
        """
        logger.error("Redis 锁 %s 失败（key=%s）：%s", op, key, exc)
        return FlowError(
            "lock_unavailable",
            "锁服务暂不可用（Redis 连接失败），请稍后重试；"
            "本次未做任何修改，重试是安全的",
        )

    # ------------------------------------------------------------------ #
    # LockBackend 实现
    # ------------------------------------------------------------------ #
    def acquire(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        full = self._full_key(key)
        try:
            # NX = 仅当 key 不存在才设置（互斥）；PX = 毫秒租约（防死锁）
            ok = self._client.set(full, owner, nx=True, px=int(ttl_seconds * 1000))
        except Exception as exc:  # noqa: BLE001 - redis 异常类型较多，统一收敛
            raise self._guard("acquire", exc, key) from exc
        if ok:
            logger.debug("锁已获取：%s（owner=%s ttl=%ds）", full, owner, ttl_seconds)
        return bool(ok)

    def release(self, key: str, *, owner: str) -> bool:
        full = self._full_key(key)
        try:
            # 原子校验 owner 再删除；返回值 1=删了 0=不是你的锁（或已过期）
            removed = self._client.eval(_RELEASE_LUA, 1, full, owner)
        except Exception as exc:  # noqa: BLE001
            raise self._guard("release", exc, key) from exc
        return bool(removed)

    def renew(self, key: str, *, owner: str, ttl_seconds: int) -> bool:
        full = self._full_key(key)
        try:
            ok = self._client.eval(_RENEW_LUA, 1, full, owner, int(ttl_seconds * 1000))
        except Exception as exc:  # noqa: BLE001
            raise self._guard("renew", exc, key) from exc
        return bool(ok)

    def ping(self) -> bool:
        """连通性探测（/ready 与部署前自检用，不影响业务路径）。"""
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            logger.debug("Redis 连接关闭失败（忽略）", exc_info=True)


class WatchdogRenewer:
    """锁看门狗：后台线程按 ttl/3 周期续约，直到被停止或锁丢失。

    **为什么需要看门狗**：TTL 锁有个两难 —— TTL 设短了，业务还没跑完
    锁就过期，别人趁虚而入（互斥被打破）；设长了，持锁者真崩了，别人
    要白等整个 TTL 才能接管。看门狗的解法是"TTL 保持短 + 活着的人
    持续续租"：每 ttl/3 续约一次（即使错过一两次仍有余量），租约到期
    只会发生在"持锁方确实失联"的场景。与 Redisson 的 watchdog 同一思想。

    **为什么做成 opt-in（默认关）**：本项目的写临界区都是几十毫秒级
    （状态推进 + 落库），默认 30s TTL 绰绰有余；给短临界区开看门狗是
    白白多一条后台线程、多一类失效模式（看门狗自己卡住了要不要告警？）。
    只有真正的长临界区（批量任务、外部 IO）才值得显式开启。

    **锁丢失如何感知**：续约返回 False（owner 不匹配 / 锁已不在）或
    续约本身抛异常（Redis 不可达 —— 锁服务挂了等价于锁不可信）时，
    立即停止续约并回调 on_lost，由持有方决定放弃执行还是转告警。
    """

    def __init__(
        self,
        backend: LockBackend,
        key: str,
        *,
        owner: str,
        ttl_seconds: int,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self._backend = backend
        self._key = key
        self._owner = owner
        self._ttl = ttl_seconds
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动看门狗线程（重复调用无害）。daemon=True：进程退出不因它挂住。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"flow-lock-watchdog:{self._key}", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        # 首次续约先等 ttl/3：锁刚拿到时租约是满的，立刻续是浪费一次往返。
        # 周期下限 50ms：TTL 被配成 0/1 时别把 CPU 打满
        interval = max(self._ttl / 3.0, 0.05)
        while not self._stop.wait(interval):
            try:
                renewed = self._backend.renew(self._key, owner=self._owner, ttl_seconds=self._ttl)
            except Exception:  # noqa: BLE001 - Redis 异常类型多，统一按"续约失败"收敛
                renewed = False
            if not renewed:
                logger.error(
                    "锁看门狗续约失败，停止续约（key=%s owner=%s）—— 锁可能已被抢占",
                    self._key, self._owner,
                )
                if self._on_lost is not None:
                    try:
                        self._on_lost()
                    except Exception:  # noqa: BLE001 - 回调失败不能让线程带病继续跑
                        logger.exception("锁看门狗 on_lost 回调异常（key=%s）", self._key)
                return  # 立即停止：锁已不属于自己，再续约是给别人续命

    def stop(self) -> None:
        """停止看门狗线程并等待退出。

        为什么释放锁前必须先停：不停止的话存在"释放与续约赛跑" ——
        极端时序下刚 DEL 完锁又被续约线程 PEXPIRE 回来（虽然 owner 校验
        让这几乎不可能，但把窗口关死比论证它小更省心）。join 的超时按
        "一个续约周期 + 一次网络往返"给足，正常情况下线程在毫秒级退出。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(self._ttl / 3.0, 0.05) + 3.0)
        self._thread = None


def build_lock_backend(settings: Any, store: Any) -> LockBackend:
    """按配置构建锁后端。

    配置只有一个开关：FLOW_LOCK_BACKEND = sqlite（默认） | redis。
    选 redis 但 REDIS_URL 没配 → 直接报错（而不是悄悄连 localhost）：
    多 worker 部署里"锁没了"是数据级事故，启动时就必须炸出来。
    """
    backend = (getattr(settings, "flow_lock_backend", DEFAULT_BACKEND) or DEFAULT_BACKEND).lower()

    if backend == "sqlite":
        return SqliteLockBackend(store)

    if backend == "redis":
        redis_url = (getattr(settings, "redis_url", "") or "").strip()
        if not redis_url:
            raise ValueError(
                "FLOW_LOCK_BACKEND=redis 但未配置 REDIS_URL"
                "（例如 redis://localhost:6379/0）。"
                "单机部署请把 FLOW_LOCK_BACKEND 改回 sqlite。"
            )
        backend_impl = RedisLockBackend(redis_url)
        # 启动即探测：连不上就现在炸，不要等第一个写请求才 503
        if not backend_impl.ping():
            backend_impl.close()
            raise RuntimeError(
                f"Redis 锁后端连接失败：{redis_url}。"
                "请确认 Redis 服务已启动（docker compose up redis），"
                "或把 FLOW_LOCK_BACKEND 改回 sqlite。"
            )
        logger.info("分布式锁：Redis（%s，多 worker 互斥已生效）", _mask_url(redis_url))
        return backend_impl

    raise ValueError(f"未知的 FLOW_LOCK_BACKEND：{backend}（可选 sqlite / redis）")


def _mask_url(url: str) -> str:
    """日志里脱敏 Redis URL 的密码部分（redis://:password@host → redis://:***@host）。"""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, hostpart = rest.rpartition("@")
    return f"{scheme}://***@{hostpart}"
