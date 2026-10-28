// ============================================================================
// 客户查进度 —— 用手机壳把"客户看到的那一屏"直接演出来
//
// 这一页同时是给业务员的工具（客户打电话问进度时照着念）和给面试官看的证据：
// 脱敏发生在服务端 —— 前端想多显示一个字段也拿不到。
// ============================================================================
import { useMemo, useState } from "react";
import { api, ApiError, type ProgressSnapshot } from "../api";
import { fmtAgo, fmtTime, stageLabel, stageProgress, useAsync } from "../hooks";
import { Button, Card, CardHead, Field, Notice, Select, useToast } from "../components/ui";
import { Icon } from "../components/icons";

export function ProgressPage() {
  const { notify } = useToast();
  const leads = useAsync(() => api.flow.leads(), []);
  const [leadId, setLeadId] = useState("");
  const [last4, setLast4] = useState("");
  const [snap, setSnap] = useState<ProgressSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const selected = useMemo(
    () => (leads.data?.leads ?? []).find((l) => l.lead_id === leadId),
    [leads.data, leadId],
  );

  // 演示提示：从已有线索里挑一条有真实手机尾号的，把"怎么试"直接写出来。
  // 空着的页面没人知道该填什么，面试官也一样 —— 所以给个一键填入。
  // 优先挑走到中后段的线索：节点多，手机屏上才看得出"进度"长什么样。
  const demo = useMemo(() => {
    const pool = (leads.data?.leads ?? []).filter((l) => /(\d{4})$/.test(l.phone_masked));
    if (pool.length === 0) return null;
    const rich = ["invited", "arrived", "proposal_ready", "fee_estimated", "nurturing", "wechat_added"];
    const picked =
      pool.find((l) => rich.includes(l.stage)) ??
      pool.find((l) => l.stage !== "lost") ??
      pool[0];
    return {
      lead_id: picked.lead_id,
      customer_name: picked.customer_name,
      tail: picked.phone_masked.match(/(\d{4})$/)![1],
    };
  }, [leads.data]);

  const fillDemo = () => {
    if (!demo) return;
    setLeadId(demo.lead_id);
    setLast4(demo.tail);
    setSnap(null);
    setError("");
    notify(`已填入演示数据：${demo.customer_name}`);
  };

  const query = async () => {
    if (!leadId) {
      setError("先选一条线索");
      return;
    }
    if (last4.length !== 4) {
      setError("手机号后 4 位必须是 4 位数字");
      return;
    }
    setBusy(true);
    setError("");
    setSnap(null);
    try {
      setSnap(await api.flow.progress(leadId, last4));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="g-side" style={{ gap: 20, alignItems: "start" }}>
      <div className="stack gap-16">
        <Card>
          <CardHead
            title="查一位客户的进度"
            hint="客户打电话问「到哪一步了」的时候，用这一页直接答复，不用再翻聊天记录"
          />
          <div className="card-body stack gap-14">
            <Field label="客户线索">
              <Select
                value={leadId}
                onChange={(v) => {
                  setLeadId(v);
                  setSnap(null);
                  setError("");
                }}
                options={[
                  { value: "", label: "选择客户…" },
                  ...(leads.data?.leads ?? []).map((l) => ({
                    value: l.lead_id,
                    label: `${l.customer_name} · ${l.phone_masked} · ${stageLabel(l.stage)}`,
                  })),
                ]}
              />
            </Field>

            <Field
              label="手机号后 4 位"
              hint={
                selected
                  ? `该客户登记手机号 ${selected.phone_masked}，客户需要自己报出后 4 位才能查 —— 这是身份校验，不是密码`
                  : "身份校验：客户必须同时知道线索编号与手机号后 4 位"
              }
            >
              <div className="row gap-8">
                <input
                  className="input"
                  style={{ maxWidth: 140, letterSpacing: "0.3em", textAlign: "center" }}
                  value={last4}
                  maxLength={4}
                  inputMode="numeric"
                  onChange={(e) => setLast4(e.target.value.replace(/\D/g, ""))}
                  placeholder="0000"
                />
                <Button variant="primary" icon="search" onClick={() => void query()} loading={busy}>
                  查询
                </Button>
              </div>
              {demo ? (
                <div className="row gap-8 wrap" style={{ marginTop: 8 }}>
                  <span className="t-xs dim">演示数据：{demo.customer_name} · 尾号 {demo.tail}</span>
                  <Button size="sm" variant="ghost" icon="zap" onClick={fillDemo}>
                    一键填入
                  </Button>
                </div>
              ) : null}
            </Field>

            {error && <Notice tone="danger">{error}</Notice>}

            <Notice tone="info" icon="lock">
              返回的是<b>脱敏视图</b>：只有阶段、下一步、预约信息和节点时间。
              身份证、完整手机号、征信明细、佣金、内部备注都不会下发到客户端。
            </Notice>
          </div>
        </Card>

        <Card>
          <CardHead title="客户看得到 / 看不到什么" hint="客服口径与合规口径保持一致" />
          <div className="card-body g-2" style={{ gap: 20 }}>
            <div>
              <div className="t-xs dim" style={{ marginBottom: 8 }}>能看到</div>
              {["当前阶段与下一步", "到店预约的店名与时间", "每个节点的日期", "姓名脱敏后的称呼"].map((t) => (
                <div className="bullet" key={t}>
                  <i className="bullet-dot" style={{ background: "var(--ok)" }} />
                  <span>{t}</span>
                </div>
              ))}
            </div>
            <div>
              <div className="t-xs dim" style={{ marginBottom: 8 }}>看不到</div>
              {["完整手机号与身份证", "征信查询次数与逾期", "内部催办与预警记录", "佣金与服务费明细"].map((t) => (
                <div className="bullet danger" key={t}>
                  <i className="bullet-dot" />
                  <span>{t}</span>
                </div>
              ))}
            </div>
          </div>
        </Card>
      </div>

      {/* 手机预览 */}
      <div>
        <div className="phone">
          <div className="phone-screen">
            <div className="phone-status">
              <span>9:41</span>
              <span className="row gap-4">
                <span style={{ width: 14, height: 8, border: "1px solid var(--ink-4)", borderRadius: 2 }} />
                <span style={{ width: 18, height: 8, border: "1px solid var(--ink-4)", borderRadius: 2 }} />
              </span>
            </div>

            <div className="phone-hero">
              <div style={{ fontSize: 11, opacity: 0.75, letterSpacing: "0.08em" }}>助贷服务进度</div>
              <div style={{ fontSize: 19, fontWeight: 600, marginTop: 6 }}>
                {snap ? snap.customer_name : "客户查询"}
              </div>
              <div style={{ fontSize: 11.5, opacity: 0.78, marginTop: 3 }}>
                {snap ? `线索 ${snap.lead_id}` : "凭线索编号 + 手机号后 4 位查询"}
              </div>
            </div>

            <div className="phone-body">
              {!snap ? (
                <div style={{ paddingTop: 40, textAlign: "center", color: "var(--ink-3)" }}>
                  <div
                    style={{
                      width: 48,
                      height: 48,
                      borderRadius: 14,
                      background: "var(--surface-3)",
                      display: "grid",
                      placeItems: "center",
                      margin: "0 auto 12px",
                      color: "var(--ink-4)",
                    }}
                  >
                    <Icon name="phone" size={22} />
                  </div>
                  <div className="t-sm" style={{ color: "var(--ink-2)", fontWeight: 500 }}>
                    还没有查询结果
                  </div>
                  <div className="t-xs" style={{ marginTop: 4, lineHeight: 1.7 }}>
                    左侧选择客户并输入手机号后 4 位，
                    <br />
                    这里会显示客户手机上的真实界面。
                  </div>
                </div>
              ) : (
                <div className="stack gap-14">
                  <div>
                    <div className="row" style={{ justifyContent: "space-between" }}>
                      <span className="t-xs dim">当前进度</span>
                      <span className="t-xs" style={{ color: "var(--brand)", fontWeight: 600 }}>
                        {stageProgress(snap.current_stage)}%
                      </span>
                    </div>
                    <div className="bar" style={{ marginTop: 6, height: 7 }}>
                      <i style={{ width: `${stageProgress(snap.current_stage)}%` }} />
                    </div>
                    <div style={{ fontSize: 20, fontWeight: 600, marginTop: 10 }}>{snap.stage_label}</div>
                    {snap.next_step && (
                      <div className="t-sm" style={{ color: "var(--ink-2)", marginTop: 4, lineHeight: 1.6 }}>
                        下一步：{snap.next_step}
                      </div>
                    )}
                  </div>

                  {snap.appointment && (
                    <div
                      style={{
                        padding: 12,
                        borderRadius: 12,
                        background: "var(--brand-soft)",
                        border: "1px solid var(--brand-line)",
                      }}
                    >
                      <div className="row gap-6 t-xs" style={{ color: "var(--brand-ink)" }}>
                        <Icon name="calendar" size={13} />
                        到店预约
                      </div>
                      <div className="t-sm w-500" style={{ marginTop: 4 }}>
                        {snap.appointment.store}
                      </div>
                      <div className="t-xs" style={{ color: "var(--ink-2)", marginTop: 2 }}>
                        {snap.appointment.time_text}
                      </div>
                    </div>
                  )}

                  <div>
                    <div className="t-xs dim" style={{ marginBottom: 8 }}>办理记录</div>
                    {snap.timeline.length === 0 ? (
                      <div className="t-xs dim">暂无节点</div>
                    ) : (
                      <div className="timeline">
                        {snap.timeline.map((t, i) => (
                          <div className={`tl-item ${i === snap.timeline.length - 1 ? "on" : ""}`} key={`${t.stage}-${t.ts}`}>
                            <div className="tl-title">{t.label}</div>
                            <div className="tl-time">{t.time_text}</div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="t-xs" style={{ color: "var(--ink-4)", textAlign: "center" }}>
                    更新于 {fmtAgo(snap.updated_at)}（{fmtTime(snap.updated_at)}）
                  </div>
                </div>
              )}
            </div>

            <div className="phone-foot">
              进度仅供参考，最终以银行审批与合同为准
            </div>
          </div>
        </div>

        <div className="row gap-6" style={{ justifyContent: "center", marginTop: 14 }}>
          <Button
            size="sm"
            variant="ghost"
            icon="link"
            onClick={() => {
              void navigator.clipboard?.writeText(`${window.location.origin}/ui/#/progress`);
              notify("已复制查询入口");
            }}
          >
            复制查询入口
          </Button>
        </div>
      </div>
    </div>
  );
}
