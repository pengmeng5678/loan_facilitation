// ============================================================================
// 方案中心 —— 顾问身边的产品专家
//
// 左边选客户，右边出方案；方案里每个推荐都要能回答"为什么是它"，
// 每个没推荐的产品也要能回答"为什么不是它"。政策问答必须带引用。
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type MatchResult, type Proposal, type QaAnswer } from "../api";
import { fmtPct, fmtYuan, passBandMeta, useAsync } from "../hooks";
import {
  Avatar,
  Badge,
  Button,
  Card,
  CardHead,
  Empty,
  Notice,
  Segmented,
  SkeletonRows,
  useToast,
} from "../components/ui";
import { Icon } from "../components/icons";

type Tab = "match" | "qa";

const PRESET_QUESTIONS = [
  "公积金信用贷需要什么条件？",
  "征信查询次数多还能做吗？",
  "有按揭房还能做二次抵押吗？",
  "企业主做经营贷需要什么资料？",
];

export function SolutionPage() {
  const { notify } = useToast();
  const customers = useAsync(() => api.product.customers(), []);
  const [tab, setTab] = useState<Tab>("match");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string>("");

  const [match, setMatch] = useState<MatchResult | null>(null);
  const [matchLoading, setMatchLoading] = useState(false);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [proposalLoading, setProposalLoading] = useState(false);
  const [error, setError] = useState("");

  const list = useMemo(() => {
    const items = customers.data?.customers ?? [];
    if (!query.trim()) return items;
    return items.filter(
      (c) => c.name_masked.includes(query.trim()) || c.customer_id.toLowerCase().includes(query.toLowerCase()),
    );
  }, [customers.data, query]);

  const current = useMemo(
    () => (customers.data?.customers ?? []).find((c) => c.customer_id === selected) ?? null,
    [customers.data, selected],
  );

  // 默认选中第一位客户：双栏页面右边空着，业务员会以为坏了。
  useEffect(() => {
    if (selected) return;
    const first = customers.data?.customers?.[0];
    if (first) setSelected(first.customer_id);
  }, [customers.data, selected]);

  // 选中客户自动跑一次匹配 —— 顾问不需要"再点一下"
  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setMatch(null);
    setProposal(null);
    setError("");
    setMatchLoading(true);
    api.product
      .match(selected)
      .then((r) => {
        if (!cancelled) setMatch(r);
      })
      .catch((exc) => {
        if (!cancelled) setError(exc instanceof ApiError ? exc.message : String(exc));
      })
      .finally(() => {
        if (!cancelled) setMatchLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const genProposal = async () => {
    if (!selected) return;
    setProposalLoading(true);
    try {
      setProposal(await api.product.proposal(selected));
      notify("方案书已生成");
    } catch (exc) {
      notify(exc instanceof ApiError ? exc.message : "生成失败", "error");
    } finally {
      setProposalLoading(false);
    }
  };

  return (
    <div className="g-side-nav" style={{ gap: 16, alignItems: "start" }}>
      {/* 左：客户 */}
      <Card style={{ position: "sticky", top: 76 }}>
        <div className="card-head">
          <div className="grow">
            <h3>客户档案</h3>
            <div className="hint">选一位客户，自动出方案</div>
          </div>
        </div>
        <div style={{ padding: "12px 12px 6px" }}>
          <div className="search">
            <Icon name="search" size={15} />
            <input
              className="input"
              placeholder="搜索客户"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        </div>
        <div style={{ maxHeight: "calc(100vh - 250px)", overflowY: "auto", padding: "0 8px 10px" }}>
          {customers.loading && !customers.data ? (
            <div style={{ padding: 8 }}>
              <SkeletonRows rows={4} height={54} />
            </div>
          ) : list.length === 0 ? (
            <Empty title="没有匹配的客户" />
          ) : (
            list.map((c) => {
              const active = c.customer_id === selected;
              return (
                <button
                  key={c.customer_id}
                  onClick={() => setSelected(c.customer_id)}
                  style={{
                    width: "100%",
                    display: "flex",
                    gap: 9,
                    alignItems: "center",
                    padding: "9px 10px",
                    marginBottom: 2,
                    borderRadius: 9,
                    border: "1px solid transparent",
                    background: active ? "var(--brand-soft)" : "transparent",
                    borderColor: active ? "var(--brand-line)" : "transparent",
                    cursor: "pointer",
                    textAlign: "left",
                  }}
                >
                  <Avatar name={c.name_masked} size="sm" />
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="t-sm w-500" style={{ color: active ? "var(--brand-ink)" : "var(--ink)" }}>
                      {c.name_masked}
                    </div>
                    <div className="t-xs dim truncate">
                      {c.city} · {c.age} 岁 · {c.needs.join(" / ") || "未填需求"}
                    </div>
                  </div>
                  <Icon name="chevronRight" size={14} />
                </button>
              );
            })
          )}
        </div>
      </Card>

      {/* 右：内容 */}
      <div className="stack gap-16">
        <div className="row gap-12 wrap">
          <Segmented<Tab>
            value={tab}
            onChange={setTab}
            options={[
              { value: "match", label: "方案匹配" },
              { value: "qa", label: "政策问答" },
            ]}
          />
          <div className="grow" />
          {current && <span className="t-xs dim">当前客户：{current.name_masked}（{current.customer_id}）</span>}
        </div>

        {tab === "qa" ? (
          <QaPanel />
        ) : !current ? (
          <Card>
            <Empty
              icon="sparkles"
              title="先从左边选一位客户"
              desc="方案匹配不是聊天，是准入硬筛：资质、征信、需求三条线先判能不能做，再排优先级。"
            />
          </Card>
        ) : (
          <>
            <Card pad>
              <div className="row gap-12 wrap">
                <Avatar name={current.name_masked} size="lg" />
                <div className="grow">
                  <div className="row gap-8 wrap">
                    <span style={{ fontSize: 17, fontWeight: 600 }}>{current.name_masked}</span>
                    <Badge tone="blue">{current.city}</Badge>
                    <Badge>{current.age} 岁</Badge>
                  </div>
                  <div className="row gap-6 wrap" style={{ marginTop: 7 }}>
                    {current.qualifications.map((q) => (
                      <span className="chip chip-strong" key={q}>
                        <Icon name="check" size={12} />
                        {q}
                      </span>
                    ))}
                    {current.needs.map((n) => (
                      <span className="chip" key={n}>
                        {n}
                      </span>
                    ))}
                  </div>
                </div>
                <div style={{ textAlign: "right", minWidth: 110 }}>
                  <div className="t-xs dim">负债比</div>
                  <div
                    className="num"
                    style={{
                      fontSize: 20,
                      fontWeight: 600,
                      color: current.debt_ratio > 0.6 ? "var(--danger)" : current.debt_ratio > 0.4 ? "var(--warn)" : "var(--ok)",
                    }}
                  >
                    {fmtPct(current.debt_ratio, 0)}
                  </div>
                </div>
              </div>
            </Card>

            {matchLoading && (
              <Card pad>
                <SkeletonRows rows={4} height={60} />
              </Card>
            )}

            {error && <Notice tone="danger">{error}</Notice>}

            {match && !matchLoading && (
              <>
                {!match.matched && (
                  <Notice tone="warn">
                    没有匹配到可用产品：{match.handoff_reason || "资质与准入条件不匹配"}。
                    <b> 不要硬推</b>，建议转人工专家或先养征信。
                  </Notice>
                )}

                {match.primary && (
                  <Card>
                    <div className="card-head">
                      <div className="grow">
                        <div className="row gap-6">
                          <Badge tone="green">主力方案</Badge>
                          <span className="t-xs dim">{match.primary.product_code}</span>
                        </div>
                        <h3 style={{ marginTop: 6, fontSize: 16 }}>
                          {match.primary.product_name}
                          <span className="dim" style={{ fontSize: 13, fontWeight: 400 }}>
                            {" "}
                            · {match.primary.bank}
                          </span>
                        </h3>
                      </div>
                      <div style={{ textAlign: "right" }}>
                        <div className="t-xs dim">通过率分档</div>
                        <div className="num" style={{ fontSize: 24, fontWeight: 600 }}>
                          {match.primary.pass_band_label}
                        </div>
                      </div>
                    </div>
                    <div className="card-body stack gap-16">
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
                        <Kpi label="预估额度" value={fmtYuan(match.primary.estimated_amount)} />
                        <Kpi label="利率区间" value={match.primary.rate_display || "-"} />
                        <Kpi label="综合得分" value={match.primary.score.toFixed(3)} />
                      </div>

                      <div className="g-2" style={{ gap: 18 }}>
                        <div>
                          <div className="t-xs dim" style={{ marginBottom: 7 }}>命中理由</div>
                          {match.primary.reasons.length === 0 ? (
                            <div className="t-sm dim">-</div>
                          ) : (
                            match.primary.reasons.map((r, i) => (
                              <div className="bullet" key={i}>
                                <i className="bullet-dot" />
                                <span>{r}</span>
                              </div>
                            ))
                          )}
                        </div>
                        <div>
                          <div className="t-xs dim" style={{ marginBottom: 7 }}>风险提示</div>
                          {match.primary.risks.length === 0 ? (
                            <div className="t-sm dim">暂无明显风险项</div>
                          ) : (
                            match.primary.risks.map((r, i) => (
                              <div className="bullet warn" key={i}>
                                <i className="bullet-dot" />
                                <span>{r}</span>
                              </div>
                            ))
                          )}
                        </div>
                      </div>

                      <div className="row gap-10 wrap" style={{ paddingTop: 14, borderTop: "1px solid var(--line)" }}>
                        <Button variant="primary" icon="file" onClick={genProposal} loading={proposalLoading}>
                          生成方案书
                        </Button>
                        <span className="t-xs dim">方案书会带上额度区间、资料清单、办理流程与风险提示</span>
                      </div>
                    </div>
                  </Card>
                )}

                {match.alternates.length > 0 && (
                  <Card>
                    <CardHead title="备选方案" hint="主方案被拒时立刻有退路" />
                    <div className="card-body stack gap-12">
                      {match.alternates.map((a) => (
                        <div key={a.product_code} className="row gap-10" style={{ alignItems: "flex-start" }}>
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
                  </Card>
                )}

                {match.rejected_summary.length > 0 && (
                  <Card>
                    <details>
                      <summary className="collapse-head" style={{ padding: "14px 20px" }}>
                        <span>被淘汰的 {match.rejected_summary.length} 个产品及原因</span>
                        <Icon name="chevronDown" size={16} />
                      </summary>
                      <div className="card-body stack gap-8" style={{ paddingTop: 0 }}>
                        {match.rejected_summary.map((r, i) => (
                          <div className="row gap-8 t-sm" key={i}>
                            <Badge tone="red">不满足</Badge>
                            <b style={{ fontWeight: 500 }}>{String(r.product_name ?? r.product_code ?? "产品")}</b>
                            <span className="dim">{String(r.reason ?? r.reject_reason ?? "准入条件不满足")}</span>
                          </div>
                        ))}
                      </div>
                    </details>
                  </Card>
                )}

                {proposal && (
                  <Card>
                    <CardHead
                      title="方案书"
                      hint={`${proposal.proposal_id} · 生成于 ${proposal.generated_at}`}
                      right={
                        <Button
                          size="sm"
                          icon="copy"
                          onClick={() => {
                            void navigator.clipboard?.writeText(proposal.markdown);
                            notify("方案书已复制");
                          }}
                        >
                          复制
                        </Button>
                      }
                    />
                    <div className="card-body">
                      <pre className="doc" style={{ maxHeight: 520, overflow: "auto", border: 0, padding: 0 }}>
                        {proposal.markdown}
                      </pre>
                    </div>
                  </Card>
                )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ padding: "10px 12px", background: "var(--surface-2)", borderRadius: 10 }}>
      <div className="t-xs dim">{label}</div>
      <div className="t-md num" style={{ fontWeight: 600, marginTop: 2 }}>
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function QaPanel() {
  const [question, setQuestion] = useState(PRESET_QUESTIONS[0]);
  const [answer, setAnswer] = useState<QaAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const ask = async (q: string) => {
    if (!q.trim()) return;
    setBusy(true);
    setError("");
    setAnswer(null);
    try {
      setAnswer(await api.product.qa(q.trim()));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack gap-16">
      <Card pad>
        <div className="row gap-10 wrap" style={{ alignItems: "flex-end" }}>
          <div className="field grow">
            <label>问产品、问政策、问准入</label>
            <input
              className="input"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void ask(question)}
              placeholder="例如：公积金缴存 3 年，能贷多少？"
            />
          </div>
          <Button variant="primary" icon="search" onClick={() => void ask(question)} loading={busy}>
            提问
          </Button>
        </div>
        <div className="row gap-6 wrap" style={{ marginTop: 12 }}>
          {PRESET_QUESTIONS.map((q) => (
            <button
              key={q}
              className="chip"
              style={{ cursor: "pointer" }}
              onClick={() => {
                setQuestion(q);
                void ask(q);
              }}
            >
              {q}
            </button>
          ))}
        </div>
      </Card>

      {error && <Notice tone="danger">{error}</Notice>}

      {answer && (
        <Card>
          <CardHead
            title="回答"
            right={
              answer.refused ? (
                <Badge tone="amber">已拒答</Badge>
              ) : (
                <Badge tone="green">有政策依据</Badge>
              )
            }
          />
          <div className="card-body stack gap-14">
            {answer.refused ? (
              <Notice tone="warn">
                <b>这个问题没有政策依据，系统选择不回答。</b>
                <div style={{ marginTop: 4 }}>{answer.reason || "换个问法，或直接问产品准入条件。"}</div>
              </Notice>
            ) : (
              <div className="t-md" style={{ lineHeight: 1.75, whiteSpace: "pre-wrap" }}>
                {answer.answer}
              </div>
            )}

            {answer.citations.length > 0 && (
              <div style={{ paddingTop: 12, borderTop: "1px solid var(--line)" }}>
                <div className="t-xs dim" style={{ marginBottom: 7 }}>
                  政策依据（{answer.citations.length} 条，可对原文核对）
                </div>
                {answer.citations.map((c, i) => (
                  <div key={i} className="row gap-8 t-sm" style={{ marginBottom: 5, alignItems: "flex-start" }}>
                    <Icon name="link" size={13} />
                    <span className="muted">{c}</span>
                  </div>
                ))}
              </div>
            )}

            {answer.related_products.length > 0 && (
              <div className="row gap-6 wrap">
                <span className="t-xs dim">关联产品</span>
                {answer.related_products.map((p) => (
                  <span className="chip" key={p}>
                    {p}
                  </span>
                ))}
              </div>
            )}
          </div>
        </Card>
      )}

      {!answer && !busy && (
        <Card>
          <Empty
            icon="sparkles"
            title="问一句试试"
            desc="答案必须带政策条款来源；检索相关性不达标时系统会拒答 —— 宁可说不知道，也不给客户编条件。"
          />
        </Card>
      )}
    </div>
  );
}
