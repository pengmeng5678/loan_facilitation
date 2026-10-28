// ============================================================================
// 客户详情抽屉 —— 业务员的所有动作都在这里发生
//
// 设计原则：一个阶段只突出一个主按钮。
// 业务员不需要理解状态机，只需要看到「下一步：完成首呼」然后点它；
// 高风险动作（发合同、确认费用）单独走确认弹层，并明确告诉人"批准 ≠ 已发送"。
// ============================================================================
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  api,
  ApiError,
  type AuditEntry,
  type CustomerItem,
  type FeeEstimate,
  type LeadDetail,
  type MatchResult,
  type Proposal,
  type ReadinessPack,
} from "../api";
import {
  actorLabel,
  auditActionLabel,
  auditResultLabel,
  auditText,
  fmtAgo,
  fmtDate,
  fmtMoney,
  fmtPct,
  fmtStamp,
  fmtWaited,
  fmtYuan,
  HUMAN_STAGE_LABELS,
  nextStage,
  ownerLabel,
  passBandMeta,
  sourceLabel,
  stageAction,
  stageLabel,
} from "../hooks";
import {
  Avatar,
  Badge,
  Button,
  Drawer,
  DrawerHead,
  Empty,
  Field,
  Input,
  Modal,
  Notice,
  Segmented,
  SkeletonRows,
  StageBadge,
  Textarea,
  useToast,
} from "./ui";
import { StageRail } from "./StageRail";
import { Icon } from "./icons";

type Tab = "overview" | "plan" | "prep" | "history";

export function LeadDrawer({
  leadId,
  onClose,
  onChanged,
}: {
  leadId: string | null;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const { notify } = useToast();
  const [detail, setDetail] = useState<LeadDetail | null>(null);
  const [customers, setCustomers] = useState<CustomerItem[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState("");

  const [readiness, setReadiness] = useState<ReadinessPack | null>(null);
  const [fee, setFee] = useState<FeeEstimate | null>(null);
  const [amount, setAmount] = useState("300000");
  const [match, setMatch] = useState<MatchResult | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);

  const [inviteOpen, setInviteOpen] = useState(false);
  const [store, setStore] = useState("南京新街口门店");
  const [visitAt, setVisitAt] = useState(() => {
    const d = new Date(Date.now() + 24 * 3600 * 1000);
    d.setMinutes(0, 0, 0);
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  });
  const [approveOpen, setApproveOpen] = useState<null | "send_contract" | "confirm_fee">(null);
  const [reason, setReason] = useState("");

  const load = useCallback(async () => {
    if (!leadId) return;
    setLoading(true);
    try {
      setDetail(await api.flow.leadDetail(leadId));
      setError("");
      const trail = await api.flow.audit(leadId, 30);
      setAudit(trail.entries);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [leadId]);

  useEffect(() => {
    setReadiness(null);
    setFee(null);
    setMatch(null);
    setProposal(null);
    setTab("overview");
    void load();
  }, [leadId, load]);

  useEffect(() => {
    api.product
      .customers()
      .then((r) => setCustomers(r.customers))
      .catch(() => undefined);
  }, []);

  // 进入方案 tab 就自动匹配：已经到了「方案就绪」的线索，点开却显示
  // "还没有做方案匹配"是自相矛盾的。匹配是只读的确定性打分，不写库。
  useEffect(() => {
    if (tab !== "plan" || match || !detail) return;
    let cancelled = false;
    api.product
      .match(detail.customer_id)
      .then((r) => {
        if (!cancelled) setMatch(r);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [tab, match, detail]);

  // 费用试算的金额默认取客户自己报的额度，而不是写死 30 万 ——
  // 否则业务员点"试算"算出来的是别人的数字，很容易报错价。
  useEffect(() => {
    if (!detail) return;
    let cancelled = false;
    api.product
      .customer(detail.customer_id)
      .then((c) => {
        if (!cancelled && c.desired_amount > 0) setAmount(String(Math.round(c.desired_amount)));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [detail]);

  const customer = useMemo(
    () => customers.find((c) => c.customer_id === detail?.customer_id),
    [customers, detail?.customer_id],
  );

  const refresh = useCallback(async () => {
    await load();
    onChanged?.();
  }, [load, onChanged]);

  const act = async (key: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(key);
    try {
      await fn();
      notify(done);
      await refresh();
    } catch (exc) {
      notify(exc instanceof ApiError ? exc.message : String(exc), "error");
    } finally {
      setBusy("");
    }
  };

  if (!leadId) return null;

  const stage = detail?.stage ?? "new";
  const next = nextStage(stage);
  const terminal = !next;

  const primary = () => {
    if (!detail || terminal) return;
    if (stage === "nurturing") {
      setInviteOpen(true);
      return;
    }
    if (stage === "invited") {
      void act("arrived", () => api.flow.arrived(detail.lead_id), "已确认到店");
      return;
    }
    if (stage === "arrived") {
      // 到店后的"下一步"就是出准备包（档案+方案初稿+费用预估），
      // 不是一个空推进 —— 业务员按了要真的拿到一张能带去接待桌的纸
      setTab("prep");
      void act("readiness", async () => {
        const pack = await api.flow.readiness(detail.lead_id);
        setReadiness(pack);
        if (pack.profile?.need_amount) setAmount(String(Math.round(pack.profile.need_amount)));
      }, "到店准备已生成");
      return;
    }
    if (stage === "proposal_ready") {
      // 费用必须由确定性公式算出来，不能靠"推进阶段"假装算过
      setTab("prep");
      void act("fee", async () => {
        setFee(await api.flow.fee(detail.lead_id, Number(amount) || 300000));
      }, "费用已试算");
      return;
    }
    if (stage === "fee_estimated") {
      // 签约前必须走审批：这是"对外承诺收费"的动作，agent 和业务员都不能自己拍
      setApproveOpen("confirm_fee");
      return;
    }
    void act(
      "advance",
      () => api.flow.advance(detail.lead_id, next, `${stageAction(stage)}（业务员操作）`),
      `已推进到「${stageLabel(next)}」`,
    );
  };

  return (
    <Drawer open={Boolean(leadId)} onClose={onClose} width={760}>
      <DrawerHead onClose={onClose}>
        {loading && !detail ? (
          <SkeletonRows rows={2} height={40} />
        ) : detail ? (
          <>
            <div className="row gap-12" style={{ alignItems: "flex-start" }}>
              <Avatar name={detail.customer_name} size="lg" />
              <div className="grow" style={{ minWidth: 0 }}>
                <div className="row gap-8 wrap">
                  <span style={{ fontSize: 19, fontWeight: 600, letterSpacing: "-0.01em" }}>
                    {detail.customer_name}
                  </span>
                  <StageBadge stage={detail.stage} label={stageLabel(detail.stage)} />
                  {detail.human_stage && (
                    <Badge tone="amber">
                      人工节点 · {HUMAN_STAGE_LABELS[detail.human_stage] ?? detail.human_stage}
                    </Badge>
                  )}
                </div>
                <div className="row gap-10 wrap" style={{ marginTop: 5, color: "var(--ink-3)", fontSize: 12.5 }}>
                  <span className="row gap-4">
                    <Icon name="phone" size={13} />
                    {detail.phone_masked}
                  </span>
                  <span>{sourceLabel(detail.source)}</span>
                  <span>负责人 {ownerLabel(detail.owner)}</span>
                  <span>已等待 {fmtWaited(detail.stage_entered_at)}</span>
                </div>
              </div>
            </div>

            <div style={{ marginTop: 16 }}>
              <StageRail stage={detail.stage} />
            </div>
          </>
        ) : (
          <div className="t-sm dim">客户信息加载失败</div>
        )}
      </DrawerHead>

      <div className="drawer-body">
        {error && (
          <div style={{ marginBottom: 14 }}>
            <Notice tone="danger">{error}</Notice>
          </div>
        )}

        {!detail ? (
          <SkeletonRows rows={5} height={64} />
        ) : (
          <>
            <div style={{ marginBottom: 16 }}>
              <Segmented<Tab>
                value={tab}
                onChange={setTab}
                options={[
                  { value: "overview", label: "概览" },
                  { value: "plan", label: "方案" },
                  { value: "prep", label: "到店准备" },
                  { value: "history", label: "记录" },
                ]}
              />
            </div>

            {tab === "overview" && (
              <div className="stack gap-12">
                <div className="card card-pad">
                  <div className="row gap-12" style={{ alignItems: "flex-start" }}>
                    <div
                      style={{
                        width: 38,
                        height: 38,
                        borderRadius: 11,
                        background: terminal ? "var(--surface-3)" : "var(--brand-soft)",
                        color: terminal ? "var(--ink-3)" : "var(--brand)",
                        display: "grid",
                        placeItems: "center",
                        flex: "none",
                      }}
                    >
                      <Icon name={terminal ? "check" : "arrowRight"} size={18} />
                    </div>
                    <div className="grow">
                      <div className="t-xs dim">下一步动作</div>
                      <div style={{ fontSize: 15, fontWeight: 600, marginTop: 2 }}>
                        {terminal
                          ? detail.stage === "signed"
                            ? "已成交，进入放款与收佣"
                            : "已流失，可重新激活"
                          : stageAction(detail.stage)}
                      </div>
                      <div className="t-sm dim" style={{ marginTop: 3 }}>
                        {terminal
                          ? detail.lost_reason || "终态线索不再流转，请在记录页查看原因"
                          : `推进到「${stageLabel(next)}」后，客户侧进度会同步更新`}
                      </div>
                    </div>
                    {!terminal && (
                      <Button variant="primary" onClick={primary} loading={busy !== ""}>
                        {stage === "nurturing" ? "邀约到店" : stage === "invited" ? "确认到店" : stageAction(detail.stage)}
                      </Button>
                    )}
                  </div>

                  {detail.human_stage && (
                    <div style={{ marginTop: 14 }}>
                      <Notice tone="warn" icon="lock">
                        已进入人工节点（{HUMAN_STAGE_LABELS[detail.human_stage] ?? detail.human_stage}）：
                        AI 只做准备与建议，谈判与承诺由顾问本人完成。
                      </Notice>
                    </div>
                  )}

                  {detail.note && (
                    <div className="row gap-8" style={{ marginTop: 12, color: "var(--ink-3)", fontSize: 12.5 }}>
                      <Icon name="file" size={14} />
                      {detail.note}
                    </div>
                  )}
                </div>

                {detail.appointments.length > 0 && (
                  <div className="card">
                    <div className="card-head">
                      <div className="grow">
                        <h3>到店预约</h3>
                        <div className="hint">顾问和前台看到的是同一条记录</div>
                      </div>
                      <Badge
                        tone={
                          detail.appointments[0].status === "arrived"
                            ? "green"
                            : detail.appointments[0].status === "booked"
                              ? "cyan"
                              : "gray"
                        }
                      >
                        {detail.appointments[0].status === "arrived"
                          ? "已到店"
                          : detail.appointments[0].status === "booked"
                            ? "待到店"
                            : "已取消"}
                      </Badge>
                    </div>
                    <div className="card-body stack gap-10">
                      {detail.appointments.slice(0, 2).map((appt, idx) => (
                        <div key={appt.appointment_id ?? idx} className="row gap-10 wrap">
                          <span className="chip chip-strong">
                            <Icon name="mapPin" size={12} />
                            {appt.store || "未填门店"}
                          </span>
                          <span className="chip">
                            <Icon name="clock" size={12} />
                            {fmtDate(appt.scheduled_at)}
                          </span>
                          {appt.note && <span className="t-sm dim">{appt.note}</span>}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div className="card">
                  <div className="card-head">
                    <div className="grow">
                      <h3>客户画像</h3>
                      <div className="hint">资质决定能做什么产品，征信决定能不能过</div>
                    </div>
                    {customer && <Badge tone="blue">{customer.customer_id}</Badge>}
                  </div>
                  <div className="card-body stack gap-16">
                    {customer ? (
                      <>
                        <div>
                          <div className="t-xs dim" style={{ marginBottom: 7 }}>资质</div>
                          <div className="row gap-6 wrap">
                            {customer.qualifications.length === 0 ? (
                              <span className="t-sm" style={{ color: "var(--danger)" }}>
                                暂无有效资质 —— 准入会很受限
                              </span>
                            ) : (
                              customer.qualifications.map((q) => (
                                <span className="chip chip-strong" key={q}>
                                  <Icon name="check" size={12} />
                                  {q}
                                </span>
                              ))
                            )}
                          </div>
                        </div>
                        <div>
                          <div className="t-xs dim" style={{ marginBottom: 7 }}>需求</div>
                          <div className="row gap-6 wrap">
                            {customer.needs.map((n) => (
                              <span className="chip" key={n}>
                                {n}
                              </span>
                            ))}
                            <span className="chip">{customer.city}</span>
                            <span className="chip">{customer.age} 岁</span>
                          </div>
                        </div>
                        <div>
                          <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                            <span className="t-xs dim">负债比</span>
                            <span className="t-xs num" style={{ fontWeight: 600 }}>
                              {fmtPct(customer.debt_ratio, 0)}
                            </span>
                          </div>
                          <div className="bar">
                            <i
                              style={{
                                width: `${Math.min(100, Math.round(customer.debt_ratio * 100))}%`,
                                background:
                                  customer.debt_ratio > 0.6
                                    ? "var(--danger)"
                                    : customer.debt_ratio > 0.4
                                      ? "var(--warn)"
                                      : "var(--ok)",
                              }}
                            />
                          </div>
                          <div className="hint-text" style={{ marginTop: 5 }}>
                            月供 / 月收入，超过 60% 基本要转人工或换方案
                          </div>
                        </div>
                      </>
                    ) : (
                      <Notice tone="info">
                        这个客户还没在方案中心建档，无法做准入匹配。可以先按现场沟通记录资质，再补档。
                      </Notice>
                    )}

                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                        gap: 12,
                        paddingTop: 4,
                        borderTop: "1px solid var(--line)",
                      }}
                    >
                      <Info label="线索编号" value={<span className="mono">{detail.lead_id}</span>} />
                      <Info label="客户编号" value={<span className="mono">{detail.customer_id}</span>} />
                      <Info label="进入本阶段" value={fmtAgo(detail.stage_entered_at)} />
                      <Info label="创建时间" value={fmtDate(detail.created_at)} />
                    </div>
                  </div>
                </div>

                <div className="card">
                  <div className="card-head">
                    <div className="grow">
                      <h3>其他操作</h3>
                      <div className="hint">不常用，但真到这一步时业务员需要它</div>
                    </div>
                  </div>
                  <div className="card-body row gap-8 wrap">
                    <Button
                      icon="user"
                      size="sm"
                      onClick={() =>
                        void act("reception", () => api.flow.handoff(detail.lead_id, "reception", "客户到店，转接待"), "已转接待")
                      }
                      loading={busy === "reception"}
                    >
                      转接待
                    </Button>
                    <Button
                      icon="chat"
                      size="sm"
                      onClick={() =>
                        void act("negotiation", () => api.flow.handoff(detail.lead_id, "negotiation", "进入方案谈判"), "已转谈判")
                      }
                      loading={busy === "negotiation"}
                    >
                      转谈判
                    </Button>
                    <Button icon="file" size="sm" onClick={() => setApproveOpen("send_contract")}>
                      申请发合同
                    </Button>
                    <Button icon="wallet" size="sm" onClick={() => setApproveOpen("confirm_fee")}>
                      申请确认费用
                    </Button>
                    {detail.stage === "fee_estimated" && (
                      <Button
                        icon="check"
                        size="sm"
                        variant="ok"
                        loading={busy === "sign"}
                        onClick={() =>
                          void act(
                            "sign",
                            () =>
                              api.flow.advance(detail.lead_id, "signed", "客户已签约，服务费口径已确认"),
                            "已标记成交，进入放款收佣",
                          )
                        }
                      >
                        签约成交
                      </Button>
                    )}
                  </div>
                </div>
              </div>
            )}

            {tab === "plan" && (
              <PlanTab
                customerId={detail.customer_id}
                match={match}
                proposal={proposal}
                busy={busy}
                onMatch={() =>
                  void act("match", async () => {
                    const r = await api.product.match(detail.customer_id);
                    setMatch(r);
                  }, "方案已生成")
                }
                onProposal={() =>
                  void act("proposal", async () => {
                    const p = await api.product.proposal(detail.customer_id);
                    setProposal(p);
                  }, "方案书已生成")
                }
                onGoSolution={onClose}
              />
            )}

            {tab === "prep" && (
              <PrepTab
                leadId={detail.lead_id}
                stage={detail.stage}
                readiness={readiness}
                fee={fee}
                amount={amount}
                busy={busy}
                onAmount={setAmount}
                onBuild={() =>
                  void act("readiness", async () => {
                    const pack = await api.flow.readiness(detail.lead_id);
                    setReadiness(pack);
                    if (pack.profile?.need_amount) setAmount(String(Math.round(pack.profile.need_amount)));
                  }, "到店准备已生成")
                }
                onFee={() =>
                  void act("fee", async () => {
                    const est = await api.flow.fee(detail.lead_id, Number(amount) || 300000);
                    setFee(est);
                  }, "费用已试算")
                }
              />
            )}

            {tab === "history" && <HistoryTab detail={detail} audit={audit} />}
          </>
        )}
      </div>

      <div className="drawer-foot">
        <div className="grow t-xs dim foot-note">
          所有操作都会写审计（谁、何时、对哪条线索做了什么），重复提交由幂等键拦截
        </div>
        <Button onClick={onClose}>关闭</Button>
        {detail && !terminal && (
          <Button variant="primary" onClick={primary} loading={busy !== ""}>
            {stage === "nurturing" ? "邀约到店" : stage === "invited" ? "确认到店" : stageAction(detail.stage)}
          </Button>
        )}
      </div>

      {/* 邀约弹层 */}
      <Modal
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        title="邀约到店"
        hint="约定时间后，客户侧进度页会显示这个预约"
        footer={
          <>
            <Button onClick={() => setInviteOpen(false)}>取消</Button>
            <Button
              variant="primary"
              loading={busy === "invite"}
              onClick={() => {
                if (!detail) return;
                const ts = Math.floor(new Date(visitAt).getTime() / 1000);
                void act("invite", () => api.flow.invite(detail.lead_id, store, ts), "已发出邀约")
                  .then(() => setInviteOpen(false));
              }}
            >
              确认邀约
            </Button>
          </>
        }
      >
        <div className="stack gap-12">
          <Field label="门店">
            <Input value={store} onChange={(e) => setStore(e.target.value)} />
          </Field>
          <Field label="到店时间">
            <Input type="datetime-local" value={visitAt} onChange={(e) => setVisitAt(e.target.value)} />
          </Field>
          <Notice tone="info" icon="calendar">
            客户到店前一天，系统会自动提醒顾问；到店后点「确认到店」进入方案准备阶段。
          </Notice>
        </div>
      </Modal>

      {/* 高风险动作确认 */}
      <Modal
        open={approveOpen !== null}
        onClose={() => setApproveOpen(null)}
        title={approveOpen === "send_contract" ? "申请发送合同" : "申请确认费用"}
        hint="这是高风险动作，必须由主管审批，批准后还要由人点击执行"
        footer={
          <>
            <Button onClick={() => setApproveOpen(null)}>取消</Button>
            <Button
              variant="primary"
              loading={busy === "approve"}
              onClick={() => {
                if (!detail || !approveOpen) return;
                const fn =
                  approveOpen === "send_contract"
                    ? () => api.flow.requestContract(detail.lead_id, reason || "客户确认方案，申请发送合同")
                    : () => api.flow.requestFeeConfirm(detail.lead_id, reason || "费用明细待客户确认");
                void act("approve", fn, "已提交审批，等待主管处理").then(() => {
                  setApproveOpen(null);
                  setReason("");
                });
              }}
            >
              提交审批
            </Button>
          </>
        }
      >
        <div className="stack gap-12">
          <Field label="申请理由" hint="主管会看到这句话，写清楚客户当前状态">
            <Textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={approveOpen === "send_contract" ? "客户已确认方案，同意签署" : "客户对费用无异议，待书面确认"}
            />
          </Field>
          <Notice tone="warn" icon="lock">
            申请提交后不会自动执行。审批通过只是"允许"，真正发送/确认仍需人工点一次 ——
            防止"没人批却发出去了"。
          </Notice>
        </div>
      </Modal>
    </Drawer>
  );
}

// ---------------------------------------------------------------------------
function Info({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div className="t-xs dim">{label}</div>
      <div className="t-sm" style={{ marginTop: 2, fontWeight: 500 }}>
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function PlanTab({
  customerId,
  match,
  proposal,
  busy,
  onMatch,
  onProposal,
  onGoSolution,
}: {
  customerId: string;
  match: MatchResult | null;
  proposal: Proposal | null;
  busy: string;
  onMatch: () => void;
  onProposal: () => void;
  onGoSolution: () => void;
}) {
  if (!match) {
    return (
      <div className="card card-pad">
        <Empty
          icon="sparkles"
          title="还没有为这个客户做方案匹配"
          desc="按资质、征信、需求三个维度做准入硬筛，再按通过率/利率/额度排序。无匹配产品时不硬配，直接建议转人工。"
          action={
            <Button variant="primary" icon="sparkles" onClick={onMatch} loading={busy === "match"}>
              生成方案
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div className="stack gap-12">
      {!match.matched && (
        <Notice tone="warn">
          没有可推荐的产品：{match.handoff_reason || "准入条件不满足"}。这种情况不要硬推，转人工专家。
        </Notice>
      )}

      {match.primary && (
        <div className="card">
          <div className="card-head">
            <div className="grow">
              <div className="row gap-6">
                <Badge tone="green">主力方案</Badge>
                <span className="t-xs dim">{match.primary.product_code}</span>
              </div>
              <h3 style={{ marginTop: 6, fontSize: 15 }}>
                {match.primary.product_name} · {match.primary.bank}
              </h3>
            </div>
            <div style={{ textAlign: "right" }}>
              <div className="t-xs dim">通过率分档</div>
              <div className="num" style={{ fontSize: 20, fontWeight: 600 }}>
                {match.primary.pass_band_label}
              </div>
            </div>
          </div>
          <div className="card-body stack gap-14">
            <div className="bar">
              <i
                style={{
                  width: `${passBandMeta(match.primary.pass_band).width}%`,
                  background: passBandMeta(match.primary.pass_band).color,
                }}
              />
            </div>
            <div className="t-xs dim">{match.primary.pass_band_reason}</div>
            <div className="g-3" style={{ gap: 12 }}>
              <Info label="预估额度" value={fmtYuan(match.primary.estimated_amount)} />
              <Info label="利率区间" value={match.primary.rate_display || "-"} />
              <Info label="综合得分" value={match.primary.score.toFixed(3)} />
            </div>
            {match.primary.reasons.length > 0 && (
              <div>
                <div className="t-xs dim" style={{ marginBottom: 7 }}>为什么推荐它</div>
                {match.primary.reasons.map((r, i) => (
                  <div className="bullet" key={i}>
                    <i className="bullet-dot" />
                    <span>{r}</span>
                  </div>
                ))}
              </div>
            )}
            {match.primary.risks.length > 0 && (
              <div>
                <div className="t-xs dim" style={{ marginBottom: 7 }}>需要注意</div>
                {match.primary.risks.map((r, i) => (
                  <div className="bullet warn" key={i}>
                    <i className="bullet-dot" />
                    <span>{r}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {match.alternates.length > 0 && (
        <div className="card">
          <div className="card-head">
            <div className="grow">
              <h3>备选方案</h3>
              <div className="hint">主方案被拒时立刻有退路，不用重新问客户</div>
            </div>
          </div>
          <div className="card-body stack gap-10">
            {match.alternates.map((a) => (
              <div className="row gap-10" key={a.product_code} style={{ alignItems: "flex-start" }}>
                <span className="dot" style={{ background: "var(--brand)", marginTop: 6 }} />
                <div className="grow">
                  <div className="t-sm w-500">
                    {a.product_name} · {a.bank}
                  </div>
                  <div className="t-xs dim">
                    {fmtYuan(a.estimated_amount)} · {a.rate_display} · 通过率 {a.pass_band_label}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {match.rejected_summary.length > 0 && (
        <details className="card card-pad">
          <summary className="collapse-head" style={{ padding: 0 }}>
            <span>为什么其他 {match.rejected_summary.length} 个产品没推荐</span>
            <Icon name="chevronDown" size={16} />
          </summary>
          <div className="stack gap-8" style={{ marginTop: 12 }}>
            {match.rejected_summary.map((r, i) => (
              <div className="t-sm" key={i} style={{ color: "var(--ink-2)" }}>
                <b style={{ fontWeight: 500 }}>{String(r.product_name ?? r.product_code ?? "产品")}</b>
                <span className="dim"> —— {String(r.reason ?? r.reject_reason ?? "不满足准入")}</span>
              </div>
            ))}
          </div>
        </details>
      )}

      <div className="card card-pad">
        <div className="row gap-10 wrap">
          <div className="grow">
            <div className="t-sm w-500">方案书</div>
            <div className="t-xs dim" style={{ marginTop: 2 }}>
              模板 + 模型填充，包含额度区间、资料清单、办理流程与风险提示
            </div>
          </div>
          <Button icon="file" onClick={onProposal} loading={busy === "proposal"}>
            生成方案书
          </Button>
          <Button variant="ghost" icon="arrowUpRight" onClick={onGoSolution}>
            去方案中心
          </Button>
        </div>
        {proposal && (
          <div style={{ marginTop: 14 }}>
            <pre className="doc" style={{ maxHeight: 420, overflow: "auto" }}>
              {proposal.markdown}
            </pre>
            <div className="row gap-8" style={{ marginTop: 10 }}>
              <Button
                size="sm"
                icon="copy"
                onClick={() => {
                  void navigator.clipboard?.writeText(proposal.markdown);
                }}
              >
                复制全文
              </Button>
              <span className="t-xs dim">客户 {customerId} · 生成于 {proposal.generated_at}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function PrepTab({
  leadId,
  stage,
  readiness,
  fee,
  amount,
  busy,
  onAmount,
  onBuild,
  onFee,
}: {
  leadId: string;
  stage: string;
  readiness: ReadinessPack | null;
  fee: FeeEstimate | null;
  amount: string;
  busy: string;
  onAmount: (v: string) => void;
  onBuild: () => void;
  onFee: () => void;
}) {
  const ready = ["arrived", "proposal_ready", "fee_estimated", "signed"].includes(stage);

  return (
    <div className="stack gap-12">
      {!ready && (
        <Notice tone="info" icon="calendar">
          客户还没到店。到店前 30 分钟再生成这份一页纸更合适 —— 它会汇总档案、方案初稿和费用试算。
        </Notice>
      )}

      <div className="card card-pad">
        <div className="row gap-10 wrap">
          <div className="grow">
            <div className="t-sm w-500">到店前一页纸</div>
            <div className="t-xs dim" style={{ marginTop: 2 }}>
              档案要点 + 方案初稿 + 费用试算 + 见面先聊哪三句
            </div>
          </div>
          <Button variant="primary" icon="zap" onClick={onBuild} loading={busy === "readiness"}>
            {readiness ? "重新生成" : "生成准备包"}
          </Button>
        </div>
      </div>

      {readiness && (
        <div className="onepager">
          <div className="onepager-head">
            <Icon name="file" size={16} />
            <div className="grow">
              <div className="t-sm w-500">到店准备 · {readiness.profile.name || readiness.customer_id}</div>
              <div className="t-xs dim">
                {/* 不显示"生成耗时 0 ms"：本地跑是一瞬间，业务员看了只会觉得
                    这个数是假的；真实耗时属于工程指标，去 /metrics 看 */}
                {fmtStamp(readiness.created_at)} 生成 · 面谈前请核对档案与风险提示
              </div>
            </div>
            <Badge tone="green">已就绪</Badge>
          </div>

          {readiness.warnings.length > 0 && (
            <div className="onepager-sec" style={{ background: "var(--warn-soft)" }}>
              <h4 style={{ color: "var(--warn)" }}>先看风险</h4>
              {readiness.warnings.map((w, i) => (
                <div className="bullet warn" key={i}>
                  <i className="bullet-dot" />
                  <span>{w}</span>
                </div>
              ))}
            </div>
          )}

          <div className="onepager-sec">
            <h4>客户档案</h4>
            <div className="row gap-6 wrap" style={{ marginBottom: 10 }}>
              <span className="chip">月收入 {fmtMoney(readiness.profile.monthly_income)}</span>
              {/* 月份为 0 表示"档案里没这个数据"，不是"没缴" —— 写 0 个月
                  会让业务员以为客户断缴，属于会误导报价的显示 */}
              <span className="chip">
                社保 {readiness.profile.social_insurance_months > 0
                  ? `${readiness.profile.social_insurance_months} 个月`
                  : "未登记"}
              </span>
              <span className="chip">
                公积金 {readiness.profile.housing_fund_months > 0
                  ? `${readiness.profile.housing_fund_months} 个月`
                  : "未登记"}
              </span>
              {readiness.profile.has_property && <span className="chip chip-strong">有房</span>}
              {readiness.profile.has_vehicle && <span className="chip chip-strong">有车</span>}
              {readiness.profile.has_policy && <span className="chip chip-strong">有保单</span>}
              {readiness.profile.has_business_license && <span className="chip chip-strong">有执照</span>}
            </div>
            <div className="row gap-6 wrap">
              <span className={`chip ${readiness.profile.credit_query_times_90d > 8 ? "chip-danger" : ""}`}>
                近 90 天查询 {readiness.profile.credit_query_times_90d} 次
              </span>
              <span className={`chip ${readiness.profile.credit_overdue_times > 0 ? "chip-danger" : ""}`}>
                逾期 {readiness.profile.credit_overdue_times} 次
              </span>
              <span className="chip">网贷 {readiness.profile.microloan_count} 笔</span>
              <span className="chip">负债 {fmtMoney(readiness.profile.debt_total)}</span>
              <span className="chip">意向 {fmtMoney(readiness.profile.need_amount)}</span>
            </div>
          </div>

          {readiness.plan_draft && (
            <div className="onepager-sec">
              <h4>方案初稿</h4>
              {readiness.plan_draft.product_name ? (
                <>
                  <div className="t-sm w-500" style={{ marginBottom: 4 }}>
                    {readiness.plan_draft.product_name}
                  </div>
                  <div className="t-sm muted">{readiness.plan_draft.summary}</div>
                </>
              ) : (
                <div className="t-sm" style={{ color: "var(--warn)" }}>
                  暂未匹配到产品：{readiness.plan_draft.handoff_reason || "需现场匹配"}
                </div>
              )}
            </div>
          )}

          {readiness.talking_points.length > 0 && (
            <div className="onepager-sec">
              <h4>见面先聊这几句</h4>
              {readiness.talking_points.map((t, i) => (
                <div className="bullet" key={i}>
                  <i className="bullet-dot" />
                  <span>{t}</span>
                </div>
              ))}
            </div>
          )}

          {readiness.highlights.length > 0 && (
            <div className="onepager-sec">
              <h4>可以强调的优势</h4>
              {readiness.highlights.map((t, i) => (
                <div className="bullet" key={i}>
                  <i className="bullet-dot" />
                  <span>{t}</span>
                </div>
              ))}
            </div>
          )}

          {readiness.fee_estimate && <FeeTable fee={readiness.fee_estimate} />}
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <div className="grow">
            <h3>费用试算</h3>
            <div className="hint">预估口径，不是报价 —— 以银行审批结果与签署合同为准</div>
          </div>
        </div>
        <div className="card-body">
          <div className="row gap-8 wrap" style={{ alignItems: "flex-end" }}>
            <Field label="意向放款金额（元）" grow>
              <Input value={amount} onChange={(e) => onAmount(e.target.value.replace(/\D/g, ""))} inputMode="numeric" />
            </Field>
            <Button icon="wallet" onClick={onFee} loading={busy === "fee"}>
              试算
            </Button>
          </div>
          {fee && <FeeTable fee={fee} />}
          {!fee && (
            <div className="hint-text" style={{ marginTop: 10 }}>
              线索 {leadId} · 试算不会写入任何合同数据，只是给顾问一个可解释的数字
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function FeeTable({ fee }: { fee: FeeEstimate }) {
  return (
    <div style={{ marginTop: 14 }}>
      <div className="g-2" style={{ gap: 10 }}>
        <FeeRow label="放款金额" value={`${fee.loan_amount.toLocaleString("zh-CN")} 元`} />
        <FeeRow label="期限" value={`${fee.months} 个月`} />
        <FeeRow label="月供" value={`${fee.monthly_payment.toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 元`} />
        <FeeRow label="总利息" value={`${fee.total_interest.toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 元`} />
        <FeeRow label={`服务费（${(fee.service_fee_rate * 100).toFixed(1)}%）`} value={`${fee.service_fee.toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 元`} />
        <FeeRow label="客户到手" value={`${fee.net_amount.toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 元`} strong />
      </div>
      {fee.basis && <div className="hint-text" style={{ marginTop: 8 }}>口径：{fee.basis}</div>}
      <div className="hint-text" style={{ marginTop: 4, color: "var(--warn)" }}>{fee.disclaimer}</div>
    </div>
  );
}

function FeeRow({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div
      className="row"
      style={{
        justifyContent: "space-between",
        padding: "8px 10px",
        borderRadius: 8,
        background: "var(--surface-2)",
      }}
    >
      <span className="t-xs dim">{label}</span>
      <span className="t-sm num" style={{ fontWeight: strong ? 600 : 500 }}>
        {value}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
function HistoryTab({ detail, audit }: { detail: LeadDetail; audit: AuditEntry[] }) {
  return (
    <div className="stack gap-12">
      <div className="card">
        <div className="card-head">
          <div className="grow">
            <h3>阶段流转</h3>
            <div className="hint">每一次推进都有原因，可追溯到具体的人和时间</div>
          </div>
        </div>
        <div className="card-body">
          {detail.transitions.length === 0 ? (
            <Empty title="还没有流转记录" desc="线索刚录入，完成首呼后这里会出现第一条记录" />
          ) : (
            <div className="timeline">
              {detail.transitions.map((t, i) => (
                <div className="tl-item on" key={i}>
                  <div className="tl-title">
                    {t.from_stage ? `${stageLabel(t.from_stage)} → ` : ""}
                    <b style={{ fontWeight: 600 }}>{stageLabel(String(t.to_stage ?? ""))}</b>
                    {t.reason ? <span className="dim"> · {t.reason}</span> : null}
                  </div>
                  <div className="tl-time">
                    {fmtStamp(Number(t.ts ?? 0))}
                    {t.actor ? ` · ${actorLabel(String(t.actor))}` : ""}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <div className="grow">
            <h3>操作审计</h3>
            <div className="hint">
              每一次写操作都记一笔：成功 / 被拦截 / 失败；重复点击会被幂等键拦下并单独记一条
            </div>
          </div>
        </div>
        <div className="card-body">
          {audit.length === 0 ? (
            <Empty title="暂无审计记录" />
          ) : (
            <div className="timeline">
              {audit.map((e) => (
                <div className="tl-item" key={e.entry_id}>
                  <div className="tl-title">
                    <b style={{ fontWeight: 500 }}>{auditActionLabel(e.action)}</b>{" "}
                    <span className="dim">{auditText(e.action, e.detail || "")}</span>
                  </div>
                  <div className="tl-time">
                    {fmtStamp(e.ts)} · {actorLabel(e.actor)} ·{" "}
                    <span style={{ color: e.result === "ok" ? "var(--ok)" : "var(--warn)" }}>
                      {auditResultLabel(e.result)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
