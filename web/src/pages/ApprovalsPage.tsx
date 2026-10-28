// ============================================================================
// 审批中心 —— 高风险动作的唯一出口
//
// 页面要教会使用者的只有一件事：
//   批准 ≠ 已发送。批准只是"允许"，真正执行还得人再点一次。
// 所以按钮是两段式的：批准/拒绝 → 执行。
// ============================================================================
import { useMemo, useState } from "react";
import { api, ApiError, type Approval } from "../api";
import {
  APPROVAL_ACTION_LABELS,
  APPROVAL_STATUS_LABELS,
  ownerLabel,
  fmtAgo,
  fmtLeft,
  fmtTime,
  useAsync,
} from "../hooks";
import {
  Badge,
  Button,
  Card,
  Empty,
  Field,
  Input,
  Modal,
  Notice,
  Segmented,
  SkeletonRows,
  useToast,
} from "../components/ui";
import { Icon } from "../components/icons";

type Filter = "pending" | "approved" | "all";

export function ApprovalsPage({ approver, onChanged }: { approver: string; onChanged: () => void }) {
  const { notify } = useToast();
  const [filter, setFilter] = useState<Filter>("pending");
  const status = filter === "all" ? "" : filter;
  const approvals = useAsync(() => api.flow.approvals(status), [status]);
  const leads = useAsync(() => api.flow.leads(), []);

  const [target, setTarget] = useState<{ item: Approval; approve: boolean } | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState("");

  const nameOf = useMemo(() => {
    const map: Record<string, string> = {};
    (leads.data?.leads ?? []).forEach((l) => {
      map[l.lead_id] = `${l.customer_name}（${l.phone_masked}）`;
    });
    return map;
  }, [leads.data]);

  const decide = async () => {
    if (!target) return;
    setBusy("decide");
    try {
      await api.flow.decide(target.item.approval_id, approver, target.approve, comment);
      notify(target.approve ? "已批准，还需点击「执行」才会真正生效" : "已拒绝");
      setTarget(null);
      setComment("");
      approvals.reload();
      onChanged();
    } catch (exc) {
      notify(exc instanceof ApiError ? exc.message : "操作失败", "error");
    } finally {
      setBusy("");
    }
  };

  const execute = async (item: Approval) => {
    setBusy(item.approval_id);
    try {
      await api.flow.execute(item.approval_id);
      notify("已执行并写入审计");
      approvals.reload();
      onChanged();
    } catch (exc) {
      notify(exc instanceof ApiError ? exc.message : "执行失败", "error");
    } finally {
      setBusy("");
    }
  };

  const all = approvals.data?.approvals ?? [];
  // 「已批准待执行」只算**还没执行**的：批准只是允许，发出去才算办完。
  // 已执行的单子留在这里会让业务员以为还欠一个动作，反而更乱。
  const items = filter === "approved" ? all.filter((a) => !a.executed_at) : all;

  return (
    <div className="stack gap-16">
      <Notice tone="info" icon="lock">
        <b>这些动作必须人工批：</b>发送合同、确认费用、对外发素材、告警主管、数据入库。
        审批超时只会过期，<b>绝不会自动批准</b>；批准之后还要人点「执行」—— 系统不替人做承诺。
      </Notice>

      <div className="row gap-10 wrap">
        <Segmented<Filter>
          value={filter}
          onChange={setFilter}
          options={[
            { value: "pending", label: "待我审批" },
            { value: "approved", label: "已批准待执行" },
            { value: "all", label: "全部" },
          ]}
        />
        <div className="grow" />
        <span className="t-xs dim">审批人：{ownerLabel(approver)}</span>
        <Button size="sm" icon="refresh" onClick={approvals.reload}>
          刷新
        </Button>
      </div>

      {approvals.error && <Notice tone="danger">{approvals.error}</Notice>}

      {approvals.loading && !approvals.data ? (
        <Card pad>
          <SkeletonRows rows={3} height={84} />
        </Card>
      ) : items.length === 0 ? (
        <Card>
          <Empty
            icon="shield"
            title={filter === "pending" ? "没有待审批的申请" : "没有符合条件的审批"}
            desc="合同与费用类动作会在这里排队。没有申请是好事：说明没人乱发东西。"
          />
        </Card>
      ) : (
        <div className="stack gap-10">
          {items.map((a) => {
            const remain = a.expires_at - Date.now() / 1000;
            const expired = a.status === "expired" || (a.status === "pending" && remain <= 0);
            return (
              <Card key={a.approval_id} pad hover>
                <div className="row gap-14 wrap" style={{ alignItems: "flex-start" }}>
                  <div
                    style={{
                      width: 38,
                      height: 38,
                      borderRadius: 11,
                      background: a.status === "approved" ? "var(--ok-soft)" : a.status === "rejected" ? "var(--surface-3)" : "var(--warn-soft)",
                      color: a.status === "approved" ? "var(--ok)" : a.status === "rejected" ? "var(--ink-3)" : "var(--warn)",
                      display: "grid",
                      placeItems: "center",
                      flex: "none",
                    }}
                  >
                    <Icon name={a.action === "send_contract" ? "file" : "wallet"} size={18} />
                  </div>

                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="row gap-8 wrap">
                      <span style={{ fontSize: 15, fontWeight: 600 }}>
                        {APPROVAL_ACTION_LABELS[a.action] ?? a.action}
                      </span>
                      <Badge
                        tone={
                          a.status === "approved"
                            ? "green"
                            : a.status === "rejected"
                              ? "gray"
                              : expired
                                ? "gray"
                                : "amber"
                        }
                      >
                        {expired && a.status === "pending" ? "已过期" : APPROVAL_STATUS_LABELS[a.status] ?? a.status}
                      </Badge>
                      {a.status === "approved" &&
                        (a.executed_at ? (
                          <Badge tone="gray">已执行</Badge>
                        ) : (
                          <Badge tone="blue">待执行</Badge>
                        ))}
                    </div>

                    <div className="row gap-10 wrap" style={{ marginTop: 5, color: "var(--ink-2)", fontSize: 12.5 }}>
                      <span className="row gap-4">
                        <Icon name="user" size={13} />
                        {nameOf[a.lead_id] ?? a.lead_id}
                      </span>
                      <span>申请人 {ownerLabel(a.requester)}</span>
                      <span>{fmtAgo(a.created_at)}</span>
                      {a.status === "pending" && !expired && (
                        <span style={{ color: remain < 1800 ? "var(--danger)" : "var(--ink-3)" }}>
                          超时自动作废 · {fmtLeft(remain / 60)}
                        </span>
                      )}
                    </div>

                    <div
                      className="t-sm"
                      style={{
                        marginTop: 9,
                        padding: "9px 12px",
                        background: "var(--surface-2)",
                        borderRadius: 8,
                        color: "var(--ink-2)",
                      }}
                    >
                      {a.reason || "（未填写理由）"}
                    </div>

                    {a.status !== "pending" && a.decided_by && (
                      <div className="t-xs dim" style={{ marginTop: 8 }}>
                        {ownerLabel(a.decided_by)} 于 {fmtTime(a.decided_at)}{" "}
                        {a.status === "approved" ? "批准" : "拒绝"}
                        {a.decision_comment ? ` · ${a.decision_comment}` : ""}
                        {a.executed_at
                          ? ` · 已于 ${fmtTime(a.executed_at)} 执行（动作真正发出去了）`
                          : a.status === "approved"
                            ? " · 尚未执行（批准只是允许，还没发出去）"
                            : ""}
                      </div>
                    )}
                  </div>

                  <div className="row gap-8" style={{ flex: "none" }}>
                    {a.status === "pending" && !expired && (
                      <>
                        <Button
                          size="sm"
                          variant="ok"
                          icon="check"
                          onClick={() => {
                            setTarget({ item: a, approve: true });
                            setComment("");
                          }}
                        >
                          批准
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => {
                            setTarget({ item: a, approve: false });
                            setComment("");
                          }}
                        >
                          拒绝
                        </Button>
                      </>
                    )}
                    {a.status === "approved" && !a.executed_at && (
                      <Button
                        size="sm"
                        variant="primary"
                        icon="send"
                        loading={busy === a.approval_id}
                        onClick={() => void execute(a)}
                      >
                        执行
                      </Button>
                    )}
                    {(a.status === "rejected" || expired) && <span className="t-xs dim">不可再操作</span>}
                  </div>
                </div>

                {a.status === "approved" && !a.executed_at && (
                  <div className="t-xs" style={{ marginTop: 12, paddingLeft: 52, color: "var(--ok)" }}>
                    已获批准但尚未执行 —— 点「执行」才会真正发送/确认，并写入审计。
                  </div>
                )}
                {a.executed_at ? (
                  <div className="t-xs" style={{ marginTop: 12, paddingLeft: 52, color: "var(--ink-3)" }}>
                    动作已执行完毕（{fmtTime(a.executed_at)}），审计流水里有完整记录，无需再操作。
                  </div>
                ) : null}
              </Card>
            );
          })}
        </div>
      )}

      <Modal
        open={target !== null}
        onClose={() => setTarget(null)}
        title={target?.approve ? "批准这项申请" : "拒绝这项申请"}
        hint={target ? `${APPROVAL_ACTION_LABELS[target.item.action] ?? target.item.action} · ${nameOf[target.item.lead_id] ?? target.item.lead_id}` : ""}
        footer={
          <>
            <Button onClick={() => setTarget(null)}>取消</Button>
            <Button variant={target?.approve ? "primary" : "danger"} onClick={decide} loading={busy === "decide"}>
              {target?.approve ? "确认批准" : "确认拒绝"}
            </Button>
          </>
        }
      >
        <div className="stack gap-12">
          <div className="t-sm muted">{target?.item.reason || "（申请人未填写理由）"}</div>
          <Field label="审批意见" hint="会写进审计，拒绝时建议写清原因">
            <Input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="同意 / 额度再核 / 资料不全" />
          </Field>
          {target?.approve && (
            <Notice tone="warn" icon="lock">
              批准只代表允许。发送合同/确认费用的动作仍需在列表里点一次「执行」。
            </Notice>
          )}
        </div>
      </Modal>
    </div>
  );
}
