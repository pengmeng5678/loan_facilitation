"""幂等键与分布式锁（项目八"可靠性"的两块基石）。

**幂等解决什么**：客户在网络不好的时候连点三次"预约到店"、
消息中间件重投、前端超时重试 —— 这些在真实环境里天天发生。
如果写操作没有幂等保护，客户会收到三条确认短信、系统里出现三条
一模一样的线索。**重复操作零事故**不是性能指标，是正确性指标。

**实现要点**：
    ① 幂等键由调用方生成（业务语义：lead_id + action + 关键参数），
       而不是随机生成 —— 随机键防不住重试；
    ② 命中缓存直接返回首次结果，并且**在审计里记 idempotent_hit**，
       让"拦了多少次重复"成为一个可观测的数字；
    ③ 锁的 owner 校验 + TTL：A 不能释放 B 的锁；持锁者崩了，
       TTL 到期后锁自动可被抢占，不会死锁。

**替换路径**：锁的后端可插拔 —— SQLite（本文件默认，单机/测试）
或 Redis（多 worker 生产，见 flow_ops/locking.py：SET NX PX + Lua 释放）。
LockGuard 对两种后端暴露完全相同的接口（hold / renew）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from agent_platform.logging_config import get_logger
from flow_ops.leads import FlowError

logger = get_logger(__name__)


def make_idempotency_key(*parts: Any) -> str:
    """把业务参数组装成稳定的幂等键。

    同样的业务语义 → 同样的键。参数顺序不影响结果（先排序再哈希），
    避免调用方换个传参顺序就绕过幂等保护。

    **碰撞分析（面试必问，这里就是答案）**：

    键 = SHA-256 截断前 20 个十六进制字符 = 80 bit。为什么这个长度够：

    ① 输入空间远小于 2^80：幂等键的入参不是任意数据，而是**受控的
       业务语义字段组合**（lead_id、action 枚举、少量数字参数）。
       系统里可能出现的"不同语义"由业务模型决定 —— 线索量 × 动作数 ×
       参数组合，现实量级在 10^9 以内，离 2^80（≈1.2×10^24）差 15 个数量级；
    ② 按生日界悲观估计，也得先攒约 2^40 ≈ 1.1 万亿个**互不相同**的键
       才有 50% 碰撞概率。按日均 10 万次写入 × 30 年也只产生 ~10^9 个键，
       碰撞期望在 10^-7 量级 —— 远低于同机房单盘年故障率；
    ③ 真碰撞的后果可控：命中缓存返回的是"第一次那个操作"的结果，
       不是资金错账；下游还有状态机合法性校验与 owner 权限兜底。

    **兜底方案（若未来字段不可控，比如混入用户自由输入）**：
    让调用方额外传入客户端生成的请求 ID（UUID）作为参数参与哈希 ——
    同一次重复提交会带同一个 UUID（语义去重仍然生效），而两个语义
    巧合相同的操作因 UUID 不同而不再撞键；或把截断从 20 位升到完整
    64 位十六进制。两条路都只改这一个函数，调用方无感。
    """
    canonical = json.dumps(
        [str(p) for p in parts], sort_keys=True, ensure_ascii=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"idem_{digest}"


class IdempotencyGuard:
    """幂等执行器。

    用法：
        result, hit = guard.run(key, action=lambda: {...})
    第一次执行并缓存；之后同样的 key 直接返回缓存（hit=True）。
    """

    def __init__(self, store, *, ttl_hours: int = 24, metrics: Any = None) -> None:
        self._store = store
        self._ttl_hours = ttl_hours
        self._metrics = metrics

    def _incr(self, name: str) -> None:
        if self._metrics is not None:
            self._metrics.incr(name)

    def run(
        self,
        key: str,
        action: Callable[[], dict[str, Any]],
        *,
        action_name: str = "",
        now: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """执行带幂等保护的动作，返回 (结果, 是否命中缓存)。

        跨进程语义：先在库里原子"抢占"这个键（processing + 租约），
        抢到才执行；别的 worker 抢不到直接返回 busy，**不做任何修改**。
        执行失败则释放占位，允许安全重试 —— 失败不能把键永久占死。
        """
        from flow_ops.store import now_ts

        ts = now if now is not None else now_ts()
        owner = f"idem-{uuid.uuid4().hex[:8]}"
        state, cached = self._store.claim_idempotent(
            key,
            action=action_name,
            owner=owner,
            now=ts,
            lease_seconds=30,
            ttl_hours=self._ttl_hours,
        )
        if state == "done":
            logger.info("幂等命中：%s（%s）", key, action_name)
            return cached or {}, True
        if state == "busy":
            self._incr("idem_in_progress_total")
            raise FlowError(
                "idem_in_progress",
                "相同操作正在处理中，请稍后重试（本次未做任何修改）",
            )

        try:
            result = action()
        except BaseException:
            self._store.release_idempotent(key, owner=owner)
            raise
        self._store.complete_idempotent(
            key, owner=owner, result=result, now=ts, ttl_hours=self._ttl_hours
        )
        return result, False


class LockGuard:
    """分布式锁门面（后端可插拔：SQLite / Redis，接口完全一致）。

    用法：
        with lock.hold("invite:ld_x", owner="worker-1"):
            ...  # 临界区

    两种构造方式：
        LockGuard(store)                  —— 传 FlowStore，内部包成 SQLite 后端
                                              （单机/测试默认，零外部依赖）
        LockGuard(redis_backend)          —— 传 LockBackend（多 worker 生产）
    之所以保留"传 store"的老签名：现有调用方与测试不用改，
    而生产装配走 build_lock_backend() 显式选后端。
    """

    def __init__(self, store_or_backend, *, ttl_seconds: int = 30) -> None:
        from flow_ops.locking import LockBackend, SqliteLockBackend

        if isinstance(store_or_backend, LockBackend):
            self._backend = store_or_backend
        else:
            self._backend = SqliteLockBackend(store_or_backend)
        self._ttl_seconds = ttl_seconds

    @property
    def backend_name(self) -> str:
        """后端名（日志与 /ready 展示用：一眼看出锁是不是 Redis）。"""
        return type(self._backend).__name__

    def hold(
        self,
        key: str,
        *,
        owner: str = "",
        timeout: float = 0.0,
        watchdog: bool = False,
        ttl_seconds: int | None = None,
    ):
        """获取锁的上下文管理器。拿不到锁抛 FlowError(lock_busy)。

        为什么不阻塞等待：流程系统的写操作都是短临界区（几十毫秒），
        "快速失败让调用方重试"比"抱着锁等"更可控 —— 等待会拖垮
        HTTP 线程池，而重试可以带上同一个幂等键，行为完全一致。

        watchdog=True 时启动看门狗线程自动续约（长临界区防租约过期）。
        默认关：短临界区开看门狗徒增后台线程与失效模式，见
        locking.WatchdogRenewer 的文档。开启后若续约失败（锁已丢），
        上下文对象的 lock_lost 属性置 True，业务侧可在临界区内随时检查。

        ttl_seconds 允许单次覆盖租约时长（如调度器用"间隔一半"的短租约，
        复用同一把 LockGuard 而不必另建实例）。
        """
        owner = owner or f"lock-{uuid.uuid4().hex[:8]}"
        return _LockContext(
            self._backend,
            key,
            owner=owner,
            ttl_seconds=ttl_seconds or self._ttl_seconds,
            watchdog=watchdog,
        )

    def try_hold(self, key: str, *, owner: str = ""):
        return self.hold(key, owner=owner)

    def renew(self, key: str, *, owner: str, ttl_seconds: int | None = None) -> bool:
        """续约（长临界区防租约过期用）。返回 False 表示锁已丢，应放弃执行。"""
        return self._backend.renew(key, owner=owner, ttl_seconds=ttl_seconds or self._ttl_seconds)


class _LockContext:
    def __init__(
        self,
        backend,
        key: str,
        *,
        owner: str,
        ttl_seconds: int,
        watchdog: bool = False,
    ) -> None:
        self._backend = backend
        self._key = key
        self._owner = owner
        self._ttl = ttl_seconds
        self._watchdog_enabled = watchdog
        # 延迟创建：进入临界区（拿锁成功）才有资格续约（类型 WatchdogRenewer）
        self._watchdog = None
        # 看门狗检测到"锁已丢"（续约失败）时置 True：临界区内的业务侧可读它
        # 决定是否放弃后半段操作。bool 赋值在 CPython 下原子，无需再加锁
        self.lock_lost = False

    def __enter__(self) -> str:
        from flow_ops.locking import WatchdogRenewer

        ok = self._backend.acquire(self._key, owner=self._owner, ttl_seconds=self._ttl)
        if not ok:
            raise FlowError(
                "lock_busy",
                f"锁被占用：{self._key}（另一进程正在执行相同操作，请稍后重试）",
            )
        if self._watchdog_enabled:
            self._watchdog = WatchdogRenewer(
                self._backend,
                self._key,
                owner=self._owner,
                ttl_seconds=self._ttl,
                on_lost=self._mark_lost,
            )
            self._watchdog.start()
        return self._owner

    def _mark_lost(self) -> None:
        self.lock_lost = True

    def __exit__(self, *_exc) -> None:
        # 先停看门狗再释放：关死"释放与续约赛跑"的窗口（见 WatchdogRenewer.stop）。
        # 就算没停干净也无害 —— 锁释放后 key 已不存在，续约的 owner 校验必然失败
        if self._watchdog is not None:
            self._watchdog.stop()
            self._watchdog = None
        # 释放失败只记日志：租约到期后锁会自动让出，不影响正确性
        try:
            self._backend.release(self._key, owner=self._owner)
        except Exception:  # noqa: BLE001 - 释放失败不能覆盖临界区里的业务异常
            logger.warning("锁释放失败：%s（owner=%s，将等待租约过期）", self._key, self._owner)


@contextmanager
def noop_lock(key: str) -> Iterator[None]:
    """测试里偶尔需要显式跳过锁（验证临界区逻辑本身）。"""
    yield None
