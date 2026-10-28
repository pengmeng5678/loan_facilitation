// ============================================================================
// 今日工作台 —— 打开系统第一眼要回答："我今天该干什么？"
// 顺序：异常 → 待办 → 漏斗 → 动态。业务员不看技术指标，只看"该打哪个电话"。
// ============================================================================
import { useMemo, useState } from "react";
import { api } from "../api";
import {
  APPROVAL_ACTION_LABELS,
  FUNNEL,
  actionLabel,
  actorLabel,
  auditText,
  fmtAgo,
  ownerLabel,
  sourceLabel,
  stageAction,
  stageLabel,
  useAsync,
} from "../hooks";
import { Avatar, Badge, Button, Card, CardHead, Empty, Metric, Segmented, SkeletonRows, useToast } from "../components/ui";
import { Icon } from "../components/icons";
import { LeadDrawer } from "../components/LeadDrawer";

type TodoTab = "alerts" | "approvals" | "firstcall";

export function TodayPage({
  owner,
  onNavigate,
  reloadKey,
}: {
  owner: string;
  onNavigate: (path: string) => void;
  reloadKey: number;
}) {
  const { notify } = useToast();
  const [tab, setTab] = useState<TodoTab>("alerts");
  const [openLead, setOpenLead] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const stats = useAsync(() => api.flow.stats(), [reloadKey, tick]);
  const alerts = useAsync(() => api.flow.alerts(false), [reloadKey, tick]);
  const approvals = useAsync(() => api.flow.approvals("pending"), [reloadKey, tick]);
  const leads = useAsync(() => api.flow.leads(), [reloadKey, tick]);
  const audit = useAsync(() => api.flow.audit("", 14), [reloadKey, tick]);

  const myLeads = useMemo(
    () => (leads.data?.leads ?? []).filter((l) => (owner ? l.owner === owner : true)),
    [leads.data, owner],
  );
  const myAlerts = useMemo(
    () => (alerts.data?.alerts ?? []).filter((a) => (owner ? a.owner === owner : true)),
    [alerts.data, owner],
  );
  const myApprovals = useMemo(
    () => (approvals.data?.approvals ?? []).filter((a) => (owner ? a.requester === owner || a.requester === "agent" : true)),
    [approvals.data, owner],
  );
  const firstCall = useMemo(() => myLeads.filter((l) => l.stage === "assigned" || l.stage === "new"), [myLeads]);

  const funnelCounts = useMemo(() => {
    const out: Record<string, number> = {};
    FUNNEL.forEach((p) => {
      out[p.key] = myLeads.filter((l) => p.stages.includes(l.stage)).length;
    });
    return out;
  }, [myLeads]);

  const maxFunnel = Math.max(1, ...Object.values(funnelCounts));
  const lostCount = myLeads.filter((l) => l.stage === "lost").length;
  const active = stats.data?.metrics.leads.active ?? 0;
  const overdueTotal = myAlerts.length;
  const hotLead = myAlerts[0];

  const refresh = () => {
    setTick((t) => t + 1);
  };

  const sweep = async () => {
    try {
      await api.flow.sweep();
      notify("已扫描全部线索");
      refresh();
    } catch {
      notify("扫描失败，请稍后再试", "error");
    }
  };

  return (
    <div className="stack gap-16">
      {/* 异常优先 */}
      {overdueTotal > 0 && (
        <div
          className="card"
          style={{ borderColor: "var(--danger-line)", background: "var(--danger-soft)", padding: "14px 18px" }}
        >
          <div className="row gap-12 wrap">
            <div
              style={{
                width: 34,
                height: 34,
                borderRadius: 10,
                background: "#fff",
                color: "var(--danger)",
                display: "grid",
                placeItems: "center",
                flex: "none",
              }}
            >
              <Icon name="alert" size={18} />
            </div>
            <div className="grow">
              <div className="t-md w-600" style={{ color: "#8f1d14" }}>
                有 {overdueTotal} 条线索已经超过跟进时限
              </div>
              <div className="t-sm" style={{ color: "#a8483d", marginTop: 2 }}>
                {hotLead ? `最久的一条是 ${hotLead.customer_name}，超时 ${hotLead.overdue_minutes} 分钟。` : ""}
                系统会自动盯着，但电话还是得人来打。
              </div>
            </div>
            <Button
              variant="danger"
              onClick={() => {
                setTab("alerts");
                if (hotLead) setOpenLead(hotLead.lead_id);
              }}
            >
              立即处理
            </Button>
          </div>
        </div>
      )}

      {/* 四个数字 */}
      <div className="g-kpi" style={{ gap: 12 }}>
        <Metric
          label="超时未跟进"
          value={overdueTotal}
          unit="条"
          icon="clock"
          tone={overdueTotal > 0 ? "danger" : undefined}
          foot={overdueTotal > 0 ? "超 SLA 的线索" : "全部在时限内"}
          onClick={() => setTab("alerts")}
        />
        <Metric
          label="待首呼"
          value={firstCall.length}
          unit="条"
          icon="phoneCall"
          tone={firstCall.length > 0 ? "warn" : undefined}
          foot="黄金 30 分钟，越早越好"
          onClick={() => setTab("firstcall")}
        />
        <Metric
          label="待审批"
          value={myApprovals.length}
          unit="单"
          icon="shield"
          tone={myApprovals.length > 0 ? "warn" : undefined}
          foot="合同 / 费用，必须人批"
          onClick={() => onNavigate("approvals")}
        />
        <Metric
          label="在跟客户"
          value={active}
          unit="位"
          icon="users"
          foot={`本月累计 ${stats.data?.metrics.leads.total ?? 0} 条线索`}
        />
      </div>

      <div className="g-split" style={{ gap: 16 }}>
        {/* 待办 */}
        <Card>
          <CardHead
            title="今天的待办"
            hint="按紧急程度排，处理完会自动消失"
            right={
              <div className="row gap-8">
                <Segmented<TodoTab>
                  value={tab}
                  onChange={setTab}
                  options={[
                    { value: "alerts", label: `超时 ${overdueTotal}` },
                    { value: "approvals", label: `审批 ${myApprovals.length}` },
                    { value: "firstcall", label: `首呼 ${firstCall.length}` },
                  ]}
                />
                <button className="icon-btn" onClick={refresh} title="刷新">
                  <Icon name="refresh" size={16} />
                </button>
              </div>
            }
          />

          <div style={{ padding: "6px 8px 10px" }}>
            {stats.loading && !stats.data ? (
              <div style={{ padding: 12 }}>
                <SkeletonRows rows={3} />
              </div>
            ) : tab === "alerts" ? (
              myAlerts.length === 0 ? (
                <Empty
                  icon="check"
                  title="没有超时线索"
                  desc="所有在跟客户都在 SOP 时限内。系统仍会每 5 分钟扫一次，漏掉会立刻预警。"
                />
              ) : (
                myAlerts.map((a) => (
                  <div
                    key={a.alert_id}
                    className="row gap-12"
                    style={{
                      padding: "12px 12px",
                      borderRadius: 10,
                      alignItems: "center",
                      borderBottom: "1px solid var(--line)",
                    }}
                  >
                    <Avatar name={a.customer_name} size="sm" />
                    <div className="grow" style={{ minWidth: 0 }}>
                      <div className="row gap-8">
                        <span className="t-sm w-500">{a.customer_name}</span>
                        <Badge tone="red">超时 {a.overdue_minutes} 分钟</Badge>
                      </div>
                      <div className="t-xs dim" style={{ marginTop: 2 }}>
                        {stageLabel(a.stage)} · 该做：{actionLabel(a.action)} · 负责人 {ownerLabel(a.owner)}
                      </div>
                    </div>
                    <Button size="sm" variant="soft" onClick={() => setOpenLead(a.lead_id)}>
                      去处理
                    </Button>
                  </div>
                ))
              )
            ) : tab === "approvals" ? (
              myApprovals.length === 0 ? (
                <Empty icon="shield" title="没有待审批" desc="合同发送、费用确认这类高风险动作会出现在这里。" />
              ) : (
                myApprovals.map((a) => (
                  <div
                    key={a.approval_id}
                    className="row gap-12"
                    style={{ padding: "12px", borderBottom: "1px solid var(--line)" }}
                  >
                    <div
                      style={{
                        width: 30,
                        height: 30,
                        borderRadius: 9,
                        background: "var(--warn-soft)",
                        color: "var(--warn)",
                        display: "grid",
                        placeItems: "center",
                        flex: "none",
                      }}
                    >
                      <Icon name="lock" size={15} />
                    </div>
                    <div className="grow" style={{ minWidth: 0 }}>
                      <div className="t-sm w-500">{APPROVAL_ACTION_LABELS[a.action] ?? a.action}</div>
                      <div className="t-xs dim" style={{ marginTop: 2 }}>
                        {a.reason || "高风险动作"} · 申请人 {a.requester} · {fmtAgo(a.created_at)}
                      </div>
                    </div>
                    <Button size="sm" variant="soft" onClick={() => onNavigate("approvals")}>
                      去审批
                    </Button>
                  </div>
                ))
              )
            ) : firstCall.length === 0 ? (
              <Empty icon="phoneCall" title="首呼都打完了" desc="新线索进来会立刻出现在这里，别让它凉掉。" />
            ) : (
              firstCall.map((l) => (
                <div
                  key={l.lead_id}
                  className="row gap-12"
                  style={{ padding: "12px", borderBottom: "1px solid var(--line)" }}
                >
                  <Avatar name={l.customer_name} size="sm" />
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="row gap-8">
                      <span className="t-sm w-500">{l.customer_name}</span>
                      <Badge tone="blue">{sourceLabel(l.source)}</Badge>
                    </div>
                    <div className="t-xs dim" style={{ marginTop: 2 }}>
                      {l.phone_masked} · 等待 {fmtAgo(l.stage_entered_at)} · 下一步：{stageAction(l.stage)}
                    </div>
                  </div>
                  <Button size="sm" variant="primary" onClick={() => setOpenLead(l.lead_id)}>
                    记录首呼
                  </Button>
                </div>
              ))
            )}
          </div>

          <div
            className="row gap-8"
            style={{ padding: "12px 20px", borderTop: "1px solid var(--line)", justifyContent: "flex-end" }}
          >
            <Button size="sm" variant="ghost" icon="refresh" onClick={sweep}>
              手动扫一遍 SOP
            </Button>
            <Button size="sm" variant="ghost" icon="arrowRight" onClick={() => onNavigate("customers")}>
              全部客户
            </Button>
          </div>
        </Card>

        {/* 右侧：只放阶段分布。动态挪到下面整行 —— 挤在同一列里会让
            左栏留出半屏空白，第一眼像加载失败 */}
        <div className="stack gap-16">
          <Card>
            <CardHead
              title="阶段分布"
              hint="点任意一段，直接筛出这批客户"
              right={<span className="t-xs dim">共 {myLeads.length} 位</span>}
            />
            <div className="card-body">
              <div className="funnel" style={{ height: 150 }}>
                {FUNNEL.map((p) => {
                  const count = funnelCounts[p.key] ?? 0;
                  const h = 34 + Math.round((count / maxFunnel) * 88);
                  return (
                    <button
                      key={p.key}
                      className="funnel-col"
                      onClick={() => onNavigate(`customers?phase=${p.key}`)}
                      title={`${p.label}：${count} 位（${p.hint}）`}
                    >
                      <div
                        className="funnel-bar"
                        style={{
                          height: h,
                          background: count > 0 ? "var(--brand-soft)" : "var(--surface-3)",
                          borderColor: count > 0 ? "var(--brand-line)" : "transparent",
                          color: count > 0 ? "var(--brand-ink)" : "var(--ink-4)",
                        }}
                      >
                        {count}
                      </div>
                      <div className="funnel-label">{p.label}</div>
                    </button>
                  );
                })}
              </div>
              <div className="hint-text" style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--line)" }}>
                线索从投流进来，到首触、跟进、邀约、到店、成交 —— 每一段的数量差就是流失。
                {lostCount > 0 && `当前还有 ${lostCount} 位已判定流失，不在这六段里。`}
              </div>
            </div>
          </Card>
        </div>
      </div>

      {/* 最近动态：整行铺开，两列时间线。审计流水是"谁动了哪条线索"的证据链 */}
      <Card>
        <CardHead
          title="最近动态"
          hint="谁在什么时候动了哪条线索 —— 每一步都留痕，不是口头交接"
          right={
            <span className="t-xs dim">
              最近 {Math.min(8, audit.data?.entries?.length ?? 0)} 条
            </span>
          }
        />
        <div className="card-body" style={{ paddingTop: 6 }}>
          {audit.data?.entries?.length ? (
            <div className="g-2" style={{ columnGap: 32 }}>
              {(audit.data.entries ?? []).slice(0, 8).map((e) => (
                <div
                  key={e.entry_id}
                  className="row gap-10"
                  style={{ padding: "10px 0", borderBottom: "1px solid var(--line)", alignItems: "flex-start" }}
                >
                  <span className="t-xs dim" style={{ width: 62, flex: "none", paddingTop: 2 }}>
                    {fmtAgo(e.ts)}
                  </span>
                  <span className="t-sm grow" style={{ lineHeight: 1.55 }}>
                    <b style={{ fontWeight: 500 }}>{actorLabel(e.actor)}</b>{" "}
                    <span className="dim">{auditText(e.action, e.detail || e.action)}</span>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <Empty title="还没有动态" desc="录入第一条线索后，这里会记录每一次推进。" />
          )}
        </div>
      </Card>

      <LeadDrawer
        leadId={openLead}
        onClose={() => setOpenLead(null)}
        onChanged={refresh}
      />
    </div>
  );
}
