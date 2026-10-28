"""Prometheus /metrics 端点（对应简历"可观测"的最后一环：被采集）。

**为什么自己渲染 text format 而不引 prometheus_client**：
    本项目的指标是进程内轻量收集（observability.Metrics + 各服务 stats），
    exposition format 本身是个很薄的文本协议；自己渲染 = 零新依赖、
    离线可测、格式完全可控。生产若要换官方客户端，只需替换本文件的
    `render_prometheus`，采集契约（指标名/标签）不变。

**指标口径**（三块来源，全部进程内快照，无外部依赖）：
    ① 平台运行时   agent_runs_total / tool_calls_total / tool_latency_ms ...
                   —— 来自 observability.Metrics 的结构化出口
    ② 业务服务     gateway_*（成本/缓存/路由）、voice_*、flow_*（SOP/HITL）
                   —— 来自各服务 stats()，挑"运维真正要告警的"字段
    ③ 进程自身     app_info / flow_scheduler_running —— 部署形态可观测

**采集方式**：`curl http://localhost:8000/metrics`。
该路径在 API 鉴权白名单里（见 api/auth.py）—— Prometheus 拉取带
API Key 很别扭；生产应在反代层（Nginx/APISIX）按来源 IP 收紧。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from agent_platform.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["ops"])


# =========================================================================== #
# 一、Prometheus exposition format 渲染
# =========================================================================== #
@dataclass
class MetricFamily:
    """一个指标族（同名 + 同类型 + 一组带标签的样本）。"""

    name: str
    type: str  # counter | gauge
    help_text: str
    #: [(labels, value)]；labels 为空 dict 表示无标签样本
    samples: list[tuple[dict[str, str], float]] = field(default_factory=list)


def _escape_label_value(value: str) -> str:
    """Prometheus 标签值转义：反斜杠、双引号、换行。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_value(value: float) -> str:
    """float → Prometheus 接受的十进制。

    用 repr 的往返精度（0.1 不会变成 0.100000）；NaN/Inf 按规范写成
    NaN / +Inf（Prometheus 解析得了，乱码会污染整条 scrape）。
    """
    import math

    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def render_prometheus(families: list[MetricFamily]) -> str:
    """渲染成 Prometheus text exposition format（version 0.0.4）。

    顺序固定：HELP → TYPE → 样本行。空族也输出 HELP/TYPE（Prometheus
    依赖 TYPE 行判断类型，缺了它同名指标会被当 untyped 处理）。
    """
    lines: list[str] = []
    for fam in families:
        lines.append(f"# HELP {fam.name} {fam.help_text}")
        lines.append(f"# TYPE {fam.name} {fam.type}")
        for labels, value in fam.samples:
            if labels:
                label_str = ",".join(
                    f'{k}="{_escape_label_value(str(v))}"' for k, v in sorted(labels.items())
                )
                lines.append(f"{fam.name}{{{label_str}}} {_fmt_value(value)}")
            else:
                lines.append(f"{fam.name} {_fmt_value(value)}")
    # 末尾空行：一个 scrape 的礼貌分隔（promtool 也这么期待）
    return "\n".join(lines) + "\n"


# =========================================================================== #
# 二、采集：AppContext → 指标族
# =========================================================================== #
def collect_metric_families(ctx: Any) -> list[MetricFamily]:
    """把应用上下文翻成指标族。任何一块采集失败不影响其它块。

    为什么整体 try：/metrics 是排障入口，它自己不能成为故障源 ——
    某个服务 stats() 抛异常时，其余的指标照常产出。
    """
    families: list[MetricFamily] = []

    families.append(_app_info_family(ctx))

    try:
        families.extend(_runtime_families(ctx))
    except Exception:  # noqa: BLE001
        logger.exception("采集运行时指标失败")

    try:
        families.extend(_gateway_families(ctx))
    except Exception:  # noqa: BLE001
        logger.exception("采集网关指标失败")

    try:
        families.extend(_flow_families(ctx))
    except Exception:  # noqa: BLE001
        logger.exception("采集流程指标失败")

    try:
        families.extend(_scheduler_families(ctx))
    except Exception:  # noqa: BLE001
        logger.exception("采集调度器指标失败")

    try:
        families.extend(_data_families(ctx))
    except Exception:  # noqa: BLE001
        logger.exception("采集数据中台指标失败")

    return [f for f in families if f is not None]


def _app_info_family(ctx: Any) -> MetricFamily:
    settings = getattr(ctx, "settings", None)
    env = getattr(settings, "app_env", "unknown") if settings else "unknown"
    return MetricFamily(
        name="app_info",
        type="gauge",
        help_text="服务信息（恒为 1，用 label 携带版本与环境）",
        samples=[({"service": "zhudai-ai-suite", "version": "1.0.0", "env": str(env)}, 1.0)],
    )


def _runtime_families(ctx: Any) -> list[MetricFamily]:
    """平台运行时的 counters / histograms → 指标族。"""
    metrics = ctx.runtime.metrics
    counters = metrics.structured_counters()
    histograms = metrics.structured_histograms()

    families: list[MetricFamily] = []
    # 计数器：按 name 分族（Prometheus 要求同族同类型；incr 的语义就是 counter）
    by_name: dict[str, list[tuple[dict[str, str], float]]] = {}
    for name, labels, value in counters:
        by_name.setdefault(name, []).append((labels, value))
    for name, samples in by_name.items():
        families.append(
            MetricFamily(
                name=name if name.endswith("_total") else f"{name}_total",
                type="counter",
                help_text=f"平台计数器 {name}",
                samples=samples,
            )
        )
    # 直方图：本项目的直方图只存了样本统计，按 Prometheus 惯例拆出
    # _count / _sum / _avg / _p50 / _p95 / _max 六个出口（够告警用）
    for name, labels, samples in histograms:
        if not samples:
            continue
        ordered = sorted(samples)
        n = len(ordered)
        avg = sum(ordered) / n
        fam = MetricFamily(
            name=f"{name}_count",
            type="counter",
            help_text=f"{name} 样本数",
            samples=[(labels, float(n))],
        )
        families.append(fam)
        for suffix, value in (
            ("_sum", sum(ordered)),
            ("_avg", avg),
            ("_p50", ordered[int(n * 0.5)]),
            ("_p95", ordered[min(n - 1, int(n * 0.95))]),
            ("_max", ordered[-1]),
        ):
            families.append(
                MetricFamily(
                    name=f"{name}{suffix}",
                    type="gauge",
                    help_text=f"{name} {suffix.lstrip('_')}",
                    samples=[(labels, float(value))],
                )
            )
    return families


def _gateway_families(ctx: Any) -> list[MetricFamily]:
    """推理网关：成本与缓存是"省钱是否生效"的直接证据。"""
    stats = ctx.gateway_service.stats()
    families = [
        MetricFamily(
            name="gateway_calls_total",
            type="counter",
            help_text="网关处理的模型调用总数",
            samples=[({}, float(stats.get("calls", 0)))],
        ),
        MetricFamily(
            name="gateway_cache_hits_total",
            type="counter",
            help_text="缓存命中次数（前缀缓存 + 语义缓存）",
            samples=[({}, float(stats.get("cache_hits", 0)))],
        ),
        MetricFamily(
            name="gateway_cache_hit_rate",
            type="gauge",
            help_text="缓存命中率（0-1）",
            samples=[({}, float(stats.get("cache_hit_rate", 0.0)))],
        ),
        MetricFamily(
            name="gateway_cost_yuan",
            type="gauge",
            help_text="累计模型成本（元）",
            samples=[({}, float(stats.get("total_cost", 0.0)))],
        ),
        MetricFamily(
            name="gateway_cost_saved_yuan",
            type="gauge",
            help_text="成本治理累计节省（元，对照基线）",
            samples=[({}, float(stats.get("saved", 0.0)))],
        ),
        MetricFamily(
            name="gateway_fallbacks_total",
            type="counter",
            help_text="模型降级次数（主模型不可用走备选）",
            samples=[({}, float(stats.get("fallbacks", 0)))],
        ),
    ]
    # 路由分布：复杂度路由有没有把轻任务送去小模型，一眼可验
    routing = stats.get("routing") or {}
    if routing:
        families.append(
            MetricFamily(
                name="gateway_route_total",
                type="counter",
                help_text="按复杂度档位的路由次数",
                samples=[({"tier": str(tier)}, float(count)) for tier, count in routing.items()],
            )
        )
    return families


def _flow_families(ctx: Any) -> list[MetricFamily]:
    """流程数字员工：SOP 漏跟进率与 HITL 积压是业务健康度核心指标。"""
    snap = ctx.flow_ops.metrics.snapshot()
    leads = snap.get("leads", {})
    hitl = snap.get("hitl", {})
    prepare = snap.get("prepare", {})

    families = [
        MetricFamily(
            name="flow_leads",
            type="gauge",
            help_text="线索数（by_stage 按阶段拆分）",
            samples=(
                [({"stage": stage}, float(count)) for stage, count in (leads.get("by_stage") or {}).items()]
                + [({"stage": "__total__"}, float(leads.get("total", 0)))]
            ),
        ),
        MetricFamily(
            name="flow_sop_alerts_open",
            type="gauge",
            help_text="未处理的 SOP 漏跟进预警数（主管眼前的待办）",
            samples=[({}, float(snap.get("sop_alerts_open", 0)))],
        ),
        MetricFamily(
            name="flow_missed_followup_rate",
            type="gauge",
            help_text="漏跟进率（逾期线索 / 活跃线索）",
            samples=[({}, float(snap.get("missed_followup_rate", 0.0)))],
        ),
        MetricFamily(
            name="flow_idempotent_hits_total",
            type="counter",
            help_text="幂等拦截的重复提交次数（可靠性证据）",
            samples=[({}, float(snap.get("idempotent_hits", 0)))],
        ),
        MetricFamily(
            name="flow_hitl_pending",
            type="gauge",
            help_text="待人工审批的高风险动作数（积压 = 有人在等）",
            samples=[({}, float(hitl.get("pending", 0)))],
        ),
        MetricFamily(
            name="flow_prepare_p95_ms",
            type="gauge",
            help_text="到店前准备包生成耗时 P95（毫秒）",
            samples=[({}, float(prepare.get("p95_ms", 0.0)))],
        ),
    ]
    return families


def _scheduler_families(ctx: Any) -> list[MetricFamily]:
    """SOP 调度器：running=0 在生产里意味着"预警不会再自己跑"。"""
    scheduler = getattr(ctx, "flow_scheduler", None)
    running = bool(getattr(scheduler, "running", False)) if scheduler is not None else False
    interval = float(getattr(scheduler, "interval_seconds", 0) or 0) if scheduler is not None else 0.0
    return [
        MetricFamily(
            name="flow_scheduler_running",
            type="gauge",
            help_text="SOP 调度器是否在跑（1=在跑；生产必须为 1）",
            samples=[({}, 1.0 if running else 0.0)],
        ),
        MetricFamily(
            name="flow_scheduler_interval_seconds",
            type="gauge",
            help_text="SOP 扫描间隔（秒）",
            samples=[({}, interval)],
        ),
    ]


def _data_families(ctx: Any) -> list[MetricFamily]:
    """数据中台：管线漏斗是"数据在不在流动"的唯一证据。

    这几个指标回答的是运维最常问的三个问题：
        ① 抽取跑了吗、洗出了多少（raw / blocks）
        ② 审核有没有积压（pending 一直涨 = 主管没在看）
        ③ 下游能不能取数（approved / chunks / articles）
    """
    snap = ctx.data_platform.pipeline()
    by_category = snap.get("by_category") or {}
    staging = ctx.data_platform.stats().get("staging") or {}

    families = [
        MetricFamily(
            name="data_raw_messages",
            type="gauge",
            help_text="原始消息数（不可变层，只增不改）",
            samples=[({}, float(snap.get("raw_messages", 0)))],
        ),
        MetricFamily(
            name="data_conversation_blocks",
            type="gauge",
            help_text="清洗后的对话块数（按 9 级分类拆分）",
            samples=(
                [({"category": str(k)}, float(v)) for k, v in by_category.items()]
                + [({"category": "__total__"}, float(snap.get("blocks", 0)))]
            ),
        ),
        MetricFamily(
            name="data_quality_drop_rate",
            type="gauge",
            help_text="噪声对话块丢弃率（丢弃块 / 总块数）",
            samples=[({}, float(snap.get("drop_rate", 0.0)))],
        ),
        MetricFamily(
            name="data_staging",
            type="gauge",
            help_text="暂存区条目数（按状态拆分；pending 持续上涨说明审核积压）",
            samples=[
                ({"status": status}, float(staging.get(status, 0)))
                for status in ("pending", "approved", "rejected")
            ],
        ),
        MetricFamily(
            name="data_exports_total",
            type="gauge",
            help_text="导出批次总数",
            samples=[({}, float(snap.get("exports", 0)))],
        ),
        MetricFamily(
            name="data_knowledge_chunks",
            type="gauge",
            help_text="知识切片数（向量化后可用于检索）",
            samples=[({}, float(snap.get("chunks", 0)))],
        ),
        MetricFamily(
            name="data_knowledge_articles",
            type="gauge",
            help_text="结构化知识文章数",
            samples=[({}, float(snap.get("articles", 0)))],
        ),
    ]
    return families


# =========================================================================== #
# 三、端点
# =========================================================================== #
@router.get("/metrics", summary="Prometheus 指标（拉取式监控）")
def metrics() -> PlainTextResponse:
    """暴露进程内指标快照（Prometheus text format）。

    采集失败不返回 500：排障时最不想遇到的就是"监控接口自己挂了"，
    任何一块采不到就少一块指标，接口本身保持 200。
    """
    from app_context import get_app_context

    try:
        ctx = get_app_context()
        body = render_prometheus(collect_metric_families(ctx))
    except Exception:  # noqa: BLE001 - 同上：指标接口不能成为故障源
        logger.exception("/metrics 采集失败")
        body = "# 采集失败，详见服务端日志\n"
    return PlainTextResponse(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
