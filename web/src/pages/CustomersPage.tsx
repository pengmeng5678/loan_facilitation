// ============================================================================
// 我的客户 —— 看板 + 列表双视图
//
// 看板列不是数据库字段，是业务动作：待分配 / 待首触 / 跟进中 / 已邀约 / 到店推进 / 已成交。
// 业务员拖不动也没关系 —— 卡片上的「下一步」按钮就是唯一正确操作。
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import { api, type CustomerItem, type Lead } from "../api";
import {
  fmtWaited,
  nextStage,
  ownerLabel,
  PHASES,
  sourceLabel,
  sourceShort,
  stageAction,
  stageLabel,
  useAsync,
} from "../hooks";
import {
  Avatar,
  Badge,
  Button,
  Card,
  Empty,
  Field,
  Input,
  Modal,
  Segmented,
  Select,
  SkeletonRows,
  StageBadge,
  useToast,
} from "../components/ui";
import { Icon } from "../components/icons";
import { LeadDrawer } from "../components/LeadDrawer";

const LOST_COL = { key: "lost", label: "已流失", stages: ["lost"] };

export function CustomersPage({
  owner,
  initialPhase,
  onChanged,
}: {
  owner: string;
  initialPhase: string;
  onChanged: () => void;
}) {
  const { notify } = useToast();
  const leads = useAsync(() => api.flow.leads(), []);
  const alerts = useAsync(() => api.flow.alerts(false), []);
  const customers = useAsync(() => api.product.customers(), []);
  // 手机默认给列表：一列一个阶段的看板在 390px 屏上要滑 6 屏才看完，
  // 业务员找一个人不该先滑三下。桌面仍然默认看板（一屏看清全流程）。
  const [view, setView] = useState<"board" | "list">(() =>
    typeof window !== "undefined" && window.innerWidth < 760 ? "list" : "board",
  );
  const [phase, setPhase] = useState(initialPhase || "all");
  const [query, setQuery] = useState("");
  const [openLead, setOpenLead] = useState<string | null>(null);
  const [intakeOpen, setIntakeOpen] = useState(false);

  // 从漏斗点进来会带 ?phase=xxx，切页时同步筛选
  useEffect(() => {
    if (initialPhase) setPhase(initialPhase);
  }, [initialPhase]);

  const overdueIds = useMemo(
    () => new Set((alerts.data?.alerts ?? []).map((a) => a.lead_id)),
    [alerts.data],
  );

  const filtered = useMemo(() => {
    const list = (leads.data?.leads ?? []).filter((l) => (owner ? l.owner === owner : true));
    const byQuery = query.trim()
      ? list.filter(
          (l) =>
            l.customer_name.includes(query.trim()) ||
            l.phone_masked.includes(query.trim()) ||
            l.customer_id.toLowerCase().includes(query.trim().toLowerCase()),
        )
      : list;
    return byQuery;
  }, [leads.data, owner, query]);

  // 看板只铺 6 个业务列。已流失是终态，不是"正在推进"，单独收在下面一条里 ——
  // 7 列并排在 1440 屏上必然把最后一列切一半，看起来像页面坏了。
  const columns = useMemo(() => [...PHASES, LOST_COL], []);
  const visibleColumns =
    phase === "all" ? PHASES : columns.filter((c) => c.key === phase);
  const lostLeads = useMemo(() => filtered.filter((l) => l.stage === "lost"), [filtered]);

  const reload = () => {
    leads.reload();
    alerts.reload();
    onChanged();
  };

  return (
    <div className="stack gap-16">
      {/* 工具条 */}
      <div className="row gap-10 wrap">
        <div className="search" style={{ width: 260 }}>
          <Icon name="search" size={15} />
          <input
            className="input"
            placeholder="搜索姓名 / 手机号 / 客户编号"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        <Select
          value={phase}
          onChange={setPhase}
          options={[
            { value: "all", label: "全部阶段" },
            ...columns.map((c) => ({ value: c.key, label: c.label })),
          ]}
          className="w-auto"
        />

        <div className="grow" />

        <Segmented<"board" | "list">
          value={view}
          onChange={setView}
          options={[
            { value: "board", label: "看板" },
            { value: "list", label: "列表" },
          ]}
        />
        <Button variant="primary" icon="plus" onClick={() => setIntakeOpen(true)}>
          录入线索
        </Button>
      </div>

      {leads.error && <Card pad><div className="t-sm" style={{ color: "var(--danger)" }}>{leads.error}</div></Card>}

      {leads.loading && !leads.data ? (
        <div className="g-kpi" style={{ gap: 12 }}>
          {Array.from({ length: 5 }).map((_, i) => (
            <Card key={i} pad>
              <SkeletonRows rows={3} height={72} />
            </Card>
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <Card>
          <Empty
            icon="users"
            title="这里还没有客户"
            desc="线索来自广告落地页、客服转接或手工录入。录一条试试，系统会自动开始 SOP 计时。"
            action={
              <Button variant="primary" icon="plus" onClick={() => setIntakeOpen(true)}>
                录入第一条线索
              </Button>
            }
          />
        </Card>
      ) : view === "board" ? (
        <div className="stack gap-8">
          {phase === "all" && (
            <div className="row gap-6 t-xs dim" style={{ paddingLeft: 2 }}>
              <Icon name="arrowRight" size={12} />
              从左到右就是一条线索的完整路径：进来 → 首触 → 跟进 → 邀约 → 到店 → 成交
            </div>
          )}
          <div className="kanban">
            {visibleColumns.map((col) => {
            const items = filtered.filter((l) => col.stages.includes(l.stage));
            return (
              <div className="kanban-col" key={col.key}>
                <div className="kanban-col-head">
                  <span>{col.label}</span>
                  <span className="kanban-count">{items.length}</span>
                  {items.some((l) => overdueIds.has(l.lead_id)) && (
                    <Badge tone="red">有超时</Badge>
                  )}
                </div>
                {items.length === 0 ? (
                  <div className="t-xs" style={{ color: "var(--ink-4)", padding: "14px 6px", textAlign: "center" }}>
                    暂无
                  </div>
                ) : (
                  items
                    .slice()
                    .sort((a, b) => a.stage_entered_at - b.stage_entered_at)
                    .map((l) => (
                      <LeadCard
                        key={l.lead_id}
                        lead={l}
                        overdue={overdueIds.has(l.lead_id)}
                        onOpen={() => setOpenLead(l.lead_id)}
                      />
                    ))
                )}
              </div>
            );
          })}
          </div>

          {/* 流失不占一列，但也不能藏起来：翻旧账时要一眼看到判死在哪一步 */}
          {phase === "all" && lostLeads.length > 0 && (
            <div
              className="card"
              style={{ padding: "12px 16px", background: "var(--surface-2)" }}
            >
              <div className="row gap-10 wrap">
                <Icon name="alert" size={14} />
                <span className="t-sm w-500">已流失 {lostLeads.length} 位</span>
                <span className="t-xs dim grow truncate">
                  {lostLeads.map((l) => l.customer_name).join("、")}
                </span>
                <Button size="sm" variant="ghost" iconRight="chevronRight" onClick={() => setPhase("lost")}>
                  查看
                </Button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <Card>
          <div className="table-wrap">
          <table className="table table-leads">
            <thead>
              <tr>
                <th>客户</th>
                <th>阶段</th>
                <th>渠道</th>
                <th>负责人</th>
                <th>停留</th>
                <th>下一步</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filtered.map((l) => (
                <tr key={l.lead_id} style={{ cursor: "pointer" }} onClick={() => setOpenLead(l.lead_id)}>
                  <td>
                    <div className="row gap-8">
                      <Avatar name={l.customer_name} size="sm" />
                      <div>
                        <div className="t-sm w-500">{l.customer_name}</div>
                        <div className="t-xs dim mono">{l.phone_masked}</div>
                      </div>
                    </div>
                  </td>
                  <td>
                    <StageBadge stage={l.stage} label={stageLabel(l.stage)} />
                  </td>
                  <td className="t-sm">{sourceLabel(l.source)}</td>
                  <td className="t-sm dim">{ownerLabel(l.owner)}</td>
                  <td className="t-sm">
                    <span style={{ color: overdueIds.has(l.lead_id) ? "var(--danger)" : "var(--ink-2)" }}>
                      {fmtWaited(l.stage_entered_at)}
                    </span>
                  </td>
                  <td className="t-sm">{stageAction(l.stage)}</td>
                  <td style={{ textAlign: "right" }}>
                    <Button size="sm" variant="ghost" iconRight="chevronRight" onClick={() => setOpenLead(l.lead_id)}>
                      打开
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </Card>
      )}

      <LeadDrawer leadId={openLead} onClose={() => setOpenLead(null)} onChanged={reload} />

      <IntakeModal
        open={intakeOpen}
        onClose={() => setIntakeOpen(false)}
        customers={customers.data?.customers ?? []}
        defaultOwner={owner || "rep_01"}
        onDone={() => {
          notify("线索已录入，已开始跟进计时");
          setIntakeOpen(false);
          reload();
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
function LeadCard({ lead, overdue, onOpen }: { lead: Lead; overdue: boolean; onOpen: () => void }) {
  const next = nextStage(lead.stage);
  return (
    <div className={`lead-card ${overdue ? "overdue" : ""}`} onClick={onOpen}>
      <div className="row gap-8">
        <Avatar name={lead.customer_name} size="sm" />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row gap-6">
            <span className="t-sm w-500 truncate">{lead.customer_name}</span>
            {overdue && <span className="dot" style={{ background: "var(--danger)" }} />}
          </div>
          <div className="lead-meta">
            <span>{sourceShort(lead.source)}</span>
            <span>·</span>
            <span className="mono">{lead.phone_masked}</span>
          </div>
        </div>
      </div>

      {lead.note && (
        <div
          className="t-xs"
          style={{
            color: "var(--ink-3)",
            marginTop: 7,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {lead.note}
        </div>
      )}

      <div className="lead-next">
        <span className="row gap-4" style={{ color: overdue ? "var(--danger)" : "var(--ink-3)" }}>
          <Icon name="clock" size={12} />
          {fmtWaited(lead.stage_entered_at)}
        </span>
        {next ? (
          <span className="row gap-4" style={{ color: "var(--brand-ink)", fontWeight: 500 }}>
            {stageAction(lead.stage)}
            <Icon name="arrowRight" size={12} />
          </span>
        ) : (
          <span className="dim">{stageLabel(lead.stage)}</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function IntakeModal({
  open,
  onClose,
  customers,
  defaultOwner,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  customers: CustomerItem[];
  defaultOwner: string;
  onDone: () => void;
}) {
  const [form, setForm] = useState({
    customer_id: "",
    customer_name: "",
    phone_masked: "",
    source: "douyin",
    owner: defaultOwner,
    note: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const pickCustomer = (id: string) => {
    const c = customers.find((x) => x.customer_id === id);
    setForm((f) => ({
      ...f,
      customer_id: id,
      customer_name: c ? c.name_masked.replace(/\*/g, "") : f.customer_name,
    }));
  };

  const submit = async () => {
    if (!form.customer_id || !form.customer_name || !form.phone_masked) {
      setError("客户编号、姓名、手机号都要填");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.flow.intake(form);
      onDone();
    } catch (exc) {
      setError(String(exc instanceof Error ? exc.message : exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="录入线索"
      hint="重复提交会被幂等键拦下，不会产生两条一样的线索"
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" onClick={submit} loading={busy}>
            录入并开始跟进
          </Button>
        </>
      }
    >
      <div className="stack gap-12">
        {customers.length > 0 && (
          <Field label="从已有档案选择（可选）" hint="选档案会自动带出客户编号与姓名，方案匹配才有依据">
            <Select
              value={form.customer_id}
              onChange={pickCustomer}
              options={[
                { value: "", label: "手动填写新客户" },
                ...customers.map((c) => ({
                  value: c.customer_id,
                  label: `${c.customer_id} · ${c.name_masked} · ${c.city}`,
                })),
              ]}
            />
          </Field>
        )}
        <div className="g-2" style={{ gap: 12 }}>
          <Field label="客户编号">
            <Input
              value={form.customer_id}
              onChange={(e) => setForm({ ...form, customer_id: e.target.value })}
              placeholder="C1006"
            />
          </Field>
          <Field label="姓名">
            <Input
              value={form.customer_name}
              onChange={(e) => setForm({ ...form, customer_name: e.target.value })}
              placeholder="赵先生"
            />
          </Field>
          <Field label="手机号（脱敏）">
            <Input
              value={form.phone_masked}
              onChange={(e) => setForm({ ...form, phone_masked: e.target.value })}
              placeholder="138****5678"
            />
          </Field>
          <Field label="渠道">
            <Select
              value={form.source}
              onChange={(v) => setForm({ ...form, source: v })}
              options={[
                { value: "douyin", label: "抖音信息流" },
                { value: "infoflow", label: "信息流广告" },
                { value: "wechat", label: "微信" },
                { value: "referral", label: "老客转介绍" },
                { value: "offline", label: "线下门店" },
              ]}
            />
          </Field>
          <Field label="负责人">
            <Input value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} />
          </Field>
          <Field label="备注">
            <Input
              value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })}
              placeholder="客户关注点 / 资金用途"
            />
          </Field>
        </div>
        {error && <div className="t-sm" style={{ color: "var(--danger)" }}>{error}</div>}
      </div>
    </Modal>
  );
}
