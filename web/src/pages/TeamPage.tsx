// ============================================================================
// 团队看板 —— 给主管看的
//
// 上半部分是业务：线索从哪一段漏掉；
// 下半部分是工程健康度，折叠起来 —— 业务员不需要知道锁后端是什么，
// 但主管/技术要能一眼确认"多副本部署的三件套是不是真的生效了"。
// ============================================================================
import { useMemo, useState } from "react";
import { api } from "../api";
import { FUNNEL, fmtPct, useAsync } from "../hooks";
import { Badge, Button, Card, CardHead, Metric, Notice, Segmented, SkeletonRows } from "../components/ui";

export function TeamPage({ owner }: { owner: string }) {
  const stats = useAsync(() => api.flow.stats(), []);
  const leads = useAsync(() => api.flow.leads(), []);
  const ready = useAsync(() => api.ready(), []);
  const alerts = useAsync(() => api.flow.alerts(false), []);
  const [scope, setScope] = useState<"all" | "mine">("all");

  const list = useMemo(() => {
    const all = leads.data?.leads ?? [];
    return scope === "mine" && owner ? all.filter((l) => l.owner === owner) : all;
  }, [leads.data, scope, owner]);

  // 漏斗必须按"到达该环节及之后"累计，不能数"当前停在这一列"的人数：
  // 后者每列都是 1 个人时，算出来的"转化率 300%"只是统计口径错了。
  const funnel = useMemo(() => {
    const live = list.filter((l) => l.stage !== "lost"); // 流失不进漏斗，单独统计
    const indexOf = (stage: string) => FUNNEL.findIndex((p) => p.stages.includes(stage));
    return FUNNEL.map((phase, i) => ({
      ...phase,
      count: live.filter((l) => indexOf(l.stage) >= i).length,
      total: live.length,
    }));
  }, [list]);

  const signed = list.filter((l) => l.stage === "signed").length;
  const lost = list.filter((l) => l.stage === "lost").length;
  const max = Math.max(1, ...funnel.map((f) => f.count));

  const metrics = stats.data?.metrics;
  const scheduler = (ready.data?.flow_scheduler ?? null) as
    | { enabled: boolean; running: boolean; interval_seconds: number }
    | null;
  const agents = (ready.data?.agents ?? []) as string[];
  const tools = Number(ready.data?.tools ?? 0);

  return (
    <div className="stack gap-16">
      <div className="row gap-10 wrap">
        <Segmented<"all" | "mine">
          value={scope}
          onChange={setScope}
          options={[
            { value: "all", label: "全团队" },
            { value: "mine", label: "只看我的" },
          ]}
        />
        <div className="grow" />
        <Button size="sm" icon="refresh" onClick={() => {
          stats.reload();
          leads.reload();
          ready.reload();
          alerts.reload();
        }}>
          刷新
        </Button>
      </div>

      <div className="g-kpi" style={{ gap: 12 }}>
        <Metric label="在跟客户" value={metrics?.leads.active ?? 0} unit="位" icon="users" foot={`累计 ${metrics?.leads.total ?? 0} 条线索`} />
        <Metric
          label="漏跟进率"
          value={metrics ? fmtPct(metrics.missed_followup_rate, 1) : "-"}
          icon="clock"
          tone={(metrics?.missed_followup_rate ?? 0) > 0.2 ? "danger" : undefined}
          foot="超时线索 / 活跃线索"
        />
        <Metric label="已成单" value={signed} unit="单" icon="check" foot={`流失 ${lost} 单`} />
        <Metric
          label="到店准备包"
          // 这一格原来放的是"一页纸生成耗时 P95"。本地跑是 0.4ms，
          // 显示出来是"0 ms"——业务员看不懂，技术同事还会以为指标算错了。
          // 生成耗时属于工程指标，去 /metrics 看；这一格放"生成了几份"。
          value={metrics?.prepare.count ?? 0}
          unit="份"
          icon="zap"
          foot="自动拼档案/方案/费用/话术，人工原来 40 分钟"
        />
      </div>

      <Card>
        <CardHead
          title="线索转化漏斗"
          hint="按「走到这一段及以后」累计；每段比上一段少的人就是卡在这一段"
          right={
            <div className="row gap-6">
              <Badge tone="blue">共 {list.length} 位</Badge>
              {lost > 0 && <Badge tone="red">流失 {lost}</Badge>}
            </div>
          }
        />
        <div className="card-body">
          {leads.loading && !leads.data ? (
            <SkeletonRows rows={2} height={120} />
          ) : (
            <div className="funnel" style={{ height: 190, alignItems: "flex-end" }}>
              {funnel.map((f, i) => {
                const h = 52 + Math.round((f.count / max) * 100);
                const prev = i === 0 ? f.count : funnel[i - 1].count;
                const dropped = Math.max(0, prev - f.count);
                const share = f.total > 0 ? f.count / f.total : 0;
                return (
                  <div key={f.key} className="funnel-col" style={{ cursor: "default" }}>
                    <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 4 }} className="num">
                      {f.count}
                    </div>
                    <div
                      className="funnel-bar"
                      style={{
                        height: h,
                        background: f.count > 0 ? "var(--brand-soft)" : "var(--surface-3)",
                        borderColor: f.count > 0 ? "var(--brand-line)" : "transparent",
                      }}
                    />
                    <div className="funnel-label" style={{ marginTop: 8, fontWeight: 500, color: "var(--ink-2)" }}>
                      {f.label}
                    </div>
                    <div className="funnel-label" style={{ marginTop: 2, color: "var(--ink-4)" }}>
                      {i === 0 ? `共 ${f.total} 条有效` : `占比 ${fmtPct(share, 0)}`}
                      {i > 0 && dropped > 0 && (
                        <span style={{ color: "var(--warn)" }}> · 少 {dropped}</span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {lost > 0 && (
            <div className="hint-text" style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--line)" }}>
              已流失 {lost} 位单独统计，不参与漏斗（它们在任何一个环节都可能被判死）。
            </div>
          )}
        </div>
      </Card>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
        <Card>
          <CardHead title="流程健康度" hint="流程数字员工替你盯着的三件事" />
          <div className="card-body stack gap-12">
            <HealthRow
              label="未处理预警"
              value={`${metrics?.sop_alerts_open ?? 0} 条`}
              ok={(metrics?.sop_alerts_open ?? 0) === 0}
              note="超时未跟进，每 5 分钟自动扫描"
            />
            <HealthRow
              label="待审批（HITL）"
              value={`${metrics?.hitl.pending ?? 0} 单`}
              ok={(metrics?.hitl.pending ?? 0) === 0}
              note={
                (metrics?.hitl.avg_decision_minutes ?? 0) > 0
                  ? `平均处理 ${(metrics?.hitl.avg_decision_minutes ?? 0).toFixed(0)} 分钟`
                  : "还没有处理过的审批，没有样本"
              }
            />
            <HealthRow
              label="重复提交拦截"
              value={`${metrics?.idempotent_hits ?? 0} 次`}
              ok
              note="幂等键生效，没有产生重复线索/重复审批"
            />
            <HealthRow
              label="预警覆盖"
              value={`${alerts.data?.count ?? 0} 条`}
              ok
              note="系统兜底，不依赖「有人记得」"
            />
          </div>
        </Card>

        <Card>
          <CardHead title="部署形态" hint="多副本部署的验收点，业务侧可忽略" />
          <div className="card-body stack gap-12">
            <HealthRow
              label="锁后端"
              value={
                stats.data?.lock_backend === "RedisLockBackend"
                  ? "Redis（可多副本）"
                  : stats.data?.lock_backend
                    ? "SQLite（仅单机）"
                    : "-"
              }
              ok={stats.data?.lock_backend === "RedisLockBackend"}
              note="单机 SQLite / 多 worker 必须 Redis，且不可用要 fail-closed"
            />
            <HealthRow
              label="SOP 调度器"
              value={scheduler ? (scheduler.running ? "运行中" : "未启动") : "未启用"}
              ok={Boolean(scheduler?.running)}
              note={scheduler ? `每 ${scheduler.interval_seconds} 秒扫描` : "FLOW_SCHEDULER_ENABLED=false"}
            />
            <HealthRow
              label="已装配 Agent"
              value={`${agents.length} 个`}
              ok={agents.length > 0}
              note={`${tools} 个工具 · 按意图裁剪作用域`}
            />
            <div className="hint-text" style={{ paddingTop: 6 }}>
              当前：{agents.join(" / ") || "未就绪"}
            </div>
          </div>
        </Card>
      </div>

      <Notice tone="info" icon="layers">
        这一页的指标口径全部来自流程库与审计流水，不看模型自评。
        AI 效果另有评测集（Golden Set + 人工双盲抽检）单独衡量，两条线不混着讲。
      </Notice>
    </div>
  );
}

function HealthRow({
  label,
  value,
  ok,
  note,
}: {
  label: string;
  value: string;
  ok: boolean;
  note: string;
}) {
  return (
    <div className="row gap-10" style={{ alignItems: "flex-start" }}>
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: 4,
          marginTop: 7,
          flex: "none",
          background: ok ? "var(--ok)" : "var(--warn)",
        }}
      />
      <div className="grow">
        <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
          <span className="t-sm w-500">{label}</span>
          <span className="t-sm num" style={{ color: ok ? "var(--ink)" : "var(--warn)", fontWeight: 600 }}>
            {value}
          </span>
        </div>
        <div className="t-xs dim" style={{ marginTop: 2, lineHeight: 1.6 }}>
          {note}
        </div>
      </div>
    </div>
  );
}
