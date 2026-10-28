"""SOP 定时调度器（让"漏跟进"在超时的那一刻被发现，而不是月底复盘）。

**为什么服务里必须有一个常驻调度**：SOP 扫描是**时间驱动**的 ——
首呼 30 分钟没打、加微 2 小时没通过、到店前 1 天没确认，这些事不会
因为没人打开后台而发生或消失。没有常驻任务，预警就只能靠主管手动点
"扫描"按钮，而那正是漏跟进率 15% 的成因之一：不是没人管，是没人知道。

**四条设计约束**：

1. **不阻塞事件循环**。sweep 是同步的（SQLite 查询 + 规则判定），
   用 `asyncio.to_thread` 丢到线程池跑 —— API 请求不该被它拖慢；
2. **单轮失败不自毙**。扫描抛异常只记日志、返回空结果，下一轮照常。
   定时任务最怕"炸一次就静默死掉"，那比没有定时任务更危险；
3. **多副本防重**。sweep 本身幂等（同一预警不重复建）且重复预警有
   2×SLA 宽限期，但"两个副本同时全量扫描"仍会双倍打库、双倍发通知。
   所以每轮扫描前先抢一把 `flow:sop:sweep` 短租约：抢不到说明另一
   副本在扫，本轮直接跳过（debug 日志）—— 扫描是幂等读 + 预警去重，
   漏一轮的代价只是预警晚半间隔出现，取舍明确。锁服务本身挂了
   （如 Redis 不可达）同样只记日志跳过本轮，不拖垮调度循环；
4. **默认关闭**。`flow_scheduler_enabled=False` 时 `build_scheduler`
   返回 None，本地开发 / 测试 / demo 不起任何后台任务；生产/compose
   显式打开（见 .env.example）。

**启停边界**：调度器在 `build_app_context`（同步装配）里创建，
在 FastAPI lifespan（异步上下文）里 start/stop —— `create_task`
需要运行中的事件循环，装配阶段还没有。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from flow_ops.leads import FlowError

logger = get_logger(__name__)

#: 扫描间隔下限（秒）。配置层已有 ge=10 校验，这里再兜一层：
#: 防止有人用代码直接构造时把间隔设成 0.001，把数据库打满
MIN_INTERVAL_SECONDS = 10

#: 多副本防重的扫描锁 key（redis 后端会自动加 flow:lock: 前缀做命名空间）
SWEEP_LOCK_KEY = "flow:sop:sweep"

#: 扫描锁 TTL 上限（秒）。扫描是秒级操作，60s 已远超一轮所需；
#: 这个上限还决定了"副本崩溃后另一副本接管扫描"的最坏等待时间
SWEEP_LOCK_MAX_TTL_SECONDS = 60


class SopScheduler:
    """常驻 SOP 扫描调度器（asyncio 任务，随 FastAPI 生命周期启停）。"""

    def __init__(
        self,
        service: Any,
        *,
        interval_seconds: int = 300,
        lock_guard: Any = None,
        approval_sweeper: Any = None,
    ) -> None:
        """
        service          提供 check_sop() 的服务（FlowOpsService；
                         这里用 Any 而不是具体类型：调度器只依赖这一个
                         方法，测试可以传任何替身）
        interval_seconds 扫描间隔（秒）
        lock_guard       LockGuard（多副本防重的扫描互斥锁）。
                         None = 单副本/测试替身，不加锁，行为与旧版一致
        approval_sweeper 审批超时扫描器（ApprovalManager.expire_overdue）。
                         审批超时若不主动扫描，待办会一直"pending"到有人点开，
                         超时策略（abort/escalate）就等于没生效。
        """
        self._service = service
        self._locks = lock_guard
        self._approval_sweeper = approval_sweeper
        self.interval_seconds = max(MIN_INTERVAL_SECONDS, int(interval_seconds))
        # 扫描锁租约只需覆盖"一轮扫描"的时长：取间隔的一半 —— 一个副本
        # 崩在扫描中时，另一个最多等半间隔就能接管；上限 60s 兜底，
        # 防止把大间隔（如 30 分钟）的租约也拖成 15 分钟白等
        self._sweep_lock_ttl = min(self.interval_seconds // 2, SWEEP_LOCK_MAX_TTL_SECONDS)
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> asyncio.Task:
        """启动调度循环（必须在有事件循环的上下文里调用，如 lifespan）。"""
        if self.running:
            return self._task  # type: ignore[return-value]
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="flow-sop-scheduler")
        logger.info(
            "SOP 调度器启动：每 %d 秒扫描一轮（首轮立即执行）", self.interval_seconds
        )
        return self._task

    async def stop(self) -> None:
        """停止调度循环并等待当前轮结束。

        cancel 之后 to_thread 里的同步 sweep 可能还在跑（SQLite 读，
        毫秒级）—— lifespan 随后不关库，所以这个残余窗口无害；
        真要"绝对干净"，得把 sweep 也改成可中断的，为一个读操作
        引入取消机制不划算。
        """
        self._stopping = True
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("SOP 调度器已停止")

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    async def sweep_once(self) -> dict[str, Any]:
        """跑一轮扫描。任何失败都吞掉并返回 {} —— 见文件头约束 2。

        多副本防重（约束 3）：扫描前先抢 `flow:sop:sweep` 短租约，
        with 进入即抢锁、退出即释放（finally 语义）—— 即使 check_sop
        抛异常，锁也会被干净释放，不会把下一轮堵死。
        """
        if self._stopping:
            return {}
        try:
            # with 体内不会抛 lock_busy（sweep_sop 不走业务锁），
            # 所以这里的 lock_busy 只可能来自抢锁本身 = 另一副本在扫
            with self._sweep_lock():
                # to_thread：同步 sweep 不占事件循环；actor=scheduler 让审计里
                # 能区分"定时扫的"和"人手动点的"
                result = await asyncio.to_thread(self._service.check_sop, actor="scheduler")
        except FlowError as exc:
            if exc.code == "lock_busy":
                logger.debug("SOP 扫描：另一副本持有扫描锁（%s），本轮跳过", SWEEP_LOCK_KEY)
                return {}
            # 锁后端故障（如 Redis 不可达的 lock_unavailable）：与扫描失败
            # 同一待遇 —— 记日志、跳过本轮，交给"下一轮自愈"
            logger.exception("SOP 扫描锁不可用（跳过本轮，下轮重试）")
            return {}
        except Exception:  # noqa: BLE001
            logger.exception("SOP 扫描失败（下轮重试）")
            return {}
        if result.get("count"):
            logger.info(
                "SOP 扫描：新预警 %d 条（未处理合计 %s）",
                result.get("count"), result.get("open_total"),
            )
        self._sweep_approvals()
        return result

    def _sweep_approvals(self) -> None:
        """审批超时扫描：与 SOP 扫描同频，失败不影响本轮结果。"""
        if self._approval_sweeper is None:
            return
        try:
            expired = self._approval_sweeper.expire_overdue()
        except Exception:  # noqa: BLE001 - 审批扫描失败不能拖垮调度循环
            logger.exception("审批超时扫描失败（下轮重试）")
            return
        if expired:
            logger.info("审批超时扫描：处理 %d 条", len(expired))

    def _sweep_lock(self) -> Any:
        """构造本轮扫描的互斥上下文（进入即抢锁，退出即释放）。

        为什么没配锁时退化为 nullcontext：lock_guard=None 只出现在
        单副本部署与测试替身里，此时"永远抢得到"就是正确语义，
        不该为不存在的竞争付任何代价。
        """
        if self._locks is None:
            return contextlib.nullcontext()
        return self._locks.hold(
            SWEEP_LOCK_KEY,
            owner=f"sched-{uuid.uuid4().hex[:8]}",
            ttl_seconds=self._sweep_lock_ttl,
        )

    async def _run(self) -> None:
        """调度主循环：先扫一轮（启动时补上停机期间积累的逾期），再定时扫。"""
        try:
            while not self._stopping:
                await self.sweep_once()
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            logger.info("SOP 调度器收到取消信号")
            raise


def build_scheduler(
    settings: Settings, service: Any, approval_sweeper: Any = None
) -> SopScheduler | None:
    """按配置构造调度器：没打开开关就不建（本地/测试零后台任务）。

    扫描互斥锁取自引擎的 LockGuard（sqlite/redis 双后端随 FLOW_LOCK_BACKEND
    走）—— 不新建后端实例：锁后端（尤其 Redis 连接池）应全进程共享，
    多建只会多耗连接。测试替身没有 engine 属性时退化为 None（单副本语义）。
    """
    if not settings.flow_scheduler_enabled:
        logger.info("SOP 调度器：未启用（FLOW_SCHEDULER_ENABLED=false），预警靠手动扫描")
        return None
    engine = getattr(service, "engine", None)
    return SopScheduler(
        service,
        interval_seconds=settings.flow_scheduler_interval_seconds,
        lock_guard=getattr(engine, "locks", None),
        approval_sweeper=approval_sweeper,
    )
