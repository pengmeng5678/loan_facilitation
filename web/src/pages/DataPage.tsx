// ============================================================================
// 数据资产 —— 所有 AI 能力的底座
//
// 这一页的关键不是"能导出多少条"，而是那条审核闸门：
// 只有人工点过「批准」的对话，才能进训练集和知识库。
// ============================================================================
import { useState } from "react";
import { api, ApiError, type KnowledgeHit, type StagedItem } from "../api";
import { useAsync } from "../hooks";
import {
  Badge,
  Button,
  Card,
  CardHead,
  Empty,
  Metric,
  Notice,
  Segmented,
  SkeletonRows,
  useToast,
} from "../components/ui";
import { Icon } from "../components/icons";

const CATEGORY_LABELS: Record<string, string> = {
  系统消息: "系统消息",
  媒体消息: "媒体消息",
  敏感对话: "敏感对话",
  垃圾广告: "垃圾广告",
  过短: "过短",
  闲聊: "闲聊",
  重复内容: "重复内容",
  普通有效: "普通有效",
  高价值: "高价值",
};

type Tab = "queue" | "export" | "knowledge" | "rules";

export function DataPage() {
  const { notify } = useToast();
  const pipeline = useAsync(() => api.data.pipeline(), []);
  const staging = useAsync(() => api.data.staging("pending", 20), []);
  const exports = useAsync(() => api.data.exports(), []);
  const rules = useAsync(() => api.data.rules(), []);
  const [tab, setTab] = useState<Tab>("queue");
  const [busy, setBusy] = useState("");
  const [query, setQuery] = useState("公积金 置换");
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null);

  const p = pipeline.data;

  const refreshAll = () => {
    pipeline.reload();
    staging.reload();
    exports.reload();
  };

  const run = async (key: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(key);
    try {
      await fn();
      notify(done);
      refreshAll();
    } catch (exc) {
      notify(exc instanceof ApiError ? exc.message : "操作失败", "error");
    } finally {
      setBusy("");
    }
  };

  const reviewItem = (item: StagedItem, action: "approve" | "reject") =>
    void run(
      item.staging_id,
      () =>
        api.data.review(
          item.staging_id,
          action,
          action === "approve" ? "审核通过（工作台）" : "内容不合格（工作台驳回）",
        ),
      action === "approve" ? "已批准，这条对话可以进语料了" : "已驳回",
    );

  const exportAs = (format: string) =>
    void run(
      `export-${format}`,
      async () => {
        const m = await api.data.exportDataset(format);
        notify(`${m.format}：${m.rows} 行${m.reused ? "（复用已有产物）" : ""}`);
      },
      "导出完成",
    );

  return (
    <div className="stack gap-16">
      <Notice tone="info" icon="database">
        <b>一份对话的四条去向：</b>原始层只增不改 → 9 级分类 + 三层脱敏 → 人工审核闸门 →
        训练语料（ShareGPT / Alpaca / OpenAI JSONL）、RAG 切片、知识文章、脱敏案例素材。
        价格与身份证是硬红线，三层都跑。
      </Notice>

      <div className="g-kpi g-kpi-sm" style={{ gap: 12 }}>
        <Metric label="原始消息" value={p?.raw_messages ?? "-"} unit="条" icon="database" foot={`${p?.sessions ?? 0} 个会话`} />
        <Metric label="对话块" value={p?.blocks ?? "-"} unit="块" icon="layers" foot={`噪声丢弃 ${(p ? p.drop_rate * 100 : 0).toFixed(0)}%`} />
        <Metric label="待审核" value={p?.pending ?? "-"} unit="条" icon="clock" tone={(p?.pending ?? 0) > 0 ? "warn" : undefined} foot="批准后才可信" />
        <Metric label="可训练样本" value={p?.approved_eligible ?? "-"} unit="条" icon="check" foot="已批准且非敏感" />
        <Metric label="导出批次" value={p?.exports ?? "-"} unit="批" icon="download" foot="同输入同输出" />
        <Metric label="知识切片" value={p?.chunks ?? "-"} unit="条" icon="sparkles" foot={`${p?.articles ?? 0} 篇知识文章`} />
      </div>

      <Card pad>
        <div className="row gap-10 wrap pipeline-row">
          <div className="grow">
            <div className="t-sm w-500">数据管线</div>
            <div className="row gap-6 wrap" style={{ marginTop: 8 }}>
              {p &&
                Object.entries(p.by_category).map(([name, count]) => (
                  <span className="chip" key={name}>
                    {CATEGORY_LABELS[name] ?? name} {count}
                  </span>
                ))}
            </div>
          </div>
          <Button size="sm" icon="refresh" onClick={() => void run("dry", () => api.data.ingest(true), "试跑完成，未落库")} loading={busy === "dry"}>
            试跑（不落库）
          </Button>
          <Button size="sm" icon="zap" onClick={() => void run("ingest", () => api.data.ingest(false), "抽取完成")} loading={busy === "ingest"}>
            正式抽取
          </Button>
          <Button size="sm" icon="sparkles" onClick={() => void run("knowledge", () => api.data.buildKnowledge(), "知识产物已构建")} loading={busy === "knowledge"}>
            构建知识库
          </Button>
        </div>
      </Card>

      <div className="row gap-10 wrap">
        <Segmented<Tab>
          value={tab}
          onChange={setTab}
          options={[
            { value: "queue", label: `审核队列 ${staging.data?.count ?? 0}` },
            { value: "export", label: "导出与语料" },
            { value: "knowledge", label: "知识检索" },
            { value: "rules", label: "脱敏规则" },
          ]}
        />
        <div className="grow" />
        <Button size="sm" variant="ghost" icon="refresh" onClick={refreshAll}>
          刷新
        </Button>
      </div>

      {tab === "queue" && (
        <div className="stack gap-10">
          {staging.loading && !staging.data ? (
            <Card pad>
              <SkeletonRows rows={3} height={110} />
            </Card>
          ) : (staging.data?.items.length ?? 0) === 0 ? (
            <Card>
              <Empty
                icon="check"
                title="没有待审对话"
                desc="抽取完成后，高质量对话会进这里等人工批准。敏感对话也会出现（主管要看到投诉苗头），但它永远不会进训练集。"
              />
            </Card>
          ) : (
            staging.data?.items.map((item) => (
              <Card key={item.staging_id} pad>
                <div className="row gap-8 wrap" style={{ marginBottom: 12 }}>
                  <Badge tone="blue">{CATEGORY_LABELS[item.category] ?? item.category}</Badge>
                  <Badge tone={item.quality_score >= 0.75 ? "green" : "gray"}>
                    质量 {item.quality_score.toFixed(2)}
                  </Badge>
                  <Badge tone={item.dataset_eligible ? "green" : "amber"}>
                    {item.dataset_eligible ? "可进训练集" : "敏感 · 仅可审计"}
                  </Badge>
                  {item.price_hits > 0 && <Badge tone="red">价格命中 {item.price_hits}</Badge>}
                  <span className="t-xs dim mono">{item.staging_id}</span>
                  <div className="grow" />
                  <Button size="sm" variant="ghost" onClick={() => void reviewItem(item, "reject")} loading={busy === item.staging_id}>
                    驳回
                  </Button>
                  <Button size="sm" variant="primary" icon="check" onClick={() => void reviewItem(item, "approve")} loading={busy === item.staging_id}>
                    批准入库
                  </Button>
                </div>

                <div
                  className="stack gap-8"
                  style={{ background: "var(--surface-2)", borderRadius: 10, padding: 12, maxHeight: 220, overflow: "auto" }}
                >
                  {item.messages.slice(0, 6).map((m, i) => (
                    <div
                      key={i}
                      className="row gap-8"
                      style={{ alignItems: "flex-start", justifyContent: m.role === "customer" ? "flex-start" : "flex-end" }}
                    >
                      <div
                        className="t-sm"
                        style={{
                          maxWidth: "78%",
                          padding: "7px 10px",
                          borderRadius: 10,
                          background: m.role === "customer" ? "var(--surface)" : "var(--brand-soft)",
                          border: "1px solid",
                          borderColor: m.role === "customer" ? "var(--line)" : "var(--brand-line)",
                          color: m.role === "customer" ? "var(--ink-2)" : "var(--brand-ink)",
                        }}
                      >
                        {m.content}
                      </div>
                    </div>
                  ))}
                  {item.messages.length > 6 && (
                    <div className="t-xs dim" style={{ textAlign: "center" }}>
                      还有 {item.messages.length - 6} 条
                    </div>
                  )}
                </div>
              </Card>
            ))
          )}
        </div>
      )}

      {tab === "export" && (
        <div className="stack gap-12">
          <Card pad>
            <div className="t-sm w-500" style={{ marginBottom: 4 }}>
              导出格式
            </div>
            <div className="t-xs dim" style={{ marginBottom: 14 }}>
              导出幂等：key = sha256(格式 | 数据水位 | 内容哈希)，同样的输入不会产出两份"看起来不同"的文件。
            </div>
            <div className="row gap-8 wrap">
              {[
                { key: "sharegpt", label: "ShareGPT（多轮）" },
                { key: "alpaca", label: "Alpaca（指令）" },
                { key: "openai", label: "OpenAI JSONL（微调）" },
                { key: "rag_csv", label: "RAG CSV（检索）" },
              ].map((f) => (
                <Button
                  key={f.key}
                  size="sm"
                  icon="download"
                  onClick={() => exportAs(f.key)}
                  loading={busy === `export-${f.key}`}
                >
                  {f.label}
                </Button>
              ))}
            </div>
          </Card>

          <Card>
            <CardHead title="导出记录" hint="每批都带内容哈希与数据水位，可追溯" />
            {(exports.data?.items.length ?? 0) === 0 ? (
              <Empty title="还没有导出记录" desc="先批准几条对话，再导出试试。" />
            ) : (
              <div className="table-wrap">
          <table className="table">
                <thead>
                  <tr>
                    <th>格式</th>
                    <th>行数</th>
                    <th>大小</th>
                    <th>内容哈希</th>
                    <th>数据水位</th>
                  </tr>
                </thead>
                <tbody>
                  {exports.data?.items.map((e) => (
                    <tr key={e.export_id}>
                      <td>
                        <Badge tone="blue">{e.format}</Badge>
                      </td>
                      <td className="num">{e.rows}</td>
                      <td className="num dim">{(e.bytes / 1024).toFixed(1)} KB</td>
                      <td className="mono dim">{e.content_hash.slice(0, 10)}</td>
                      <td className="mono dim">{e.watermark}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
          </div>
            )}
          </Card>
        </div>
      )}

      {tab === "knowledge" && (
        <Card>
          <CardHead title="知识检索" hint="只检索已批准对话构建的切片；向量失败自动降级关键词" />
          <div className="card-body stack gap-12">
            <div className="row gap-8">
              <input className="input" value={query} onChange={(e) => setQuery(e.target.value)} />
              <Button
                variant="primary"
                icon="search"
                loading={busy === "search"}
                onClick={() =>
                  void run(
                    "search",
                    async () => {
                      const r = await api.data.searchKnowledge(query, 5);
                      setHits(r.hits);
                    },
                    "检索完成",
                  )
                }
              >
                检索
              </Button>
            </div>
            {hits === null ? (
              <Empty title="输入关键词试试" desc="例如：公积金 置换 / 征信查询 多" />
            ) : hits.length === 0 ? (
              <Empty title="没有命中" desc="先点上面的「构建知识库」，或换个关键词。" />
            ) : (
              hits.map((h) => (
                <div key={h.chunk_id} style={{ padding: 12, background: "var(--surface-2)", borderRadius: 10 }}>
                  <div className="row gap-6" style={{ marginBottom: 6 }}>
                    <Badge tone="blue">{h.scene || "未分类"}</Badge>
                    <span className="t-xs dim mono">score {h.score.toFixed(3)}</span>
                  </div>
                  <div className="t-sm" style={{ lineHeight: 1.7, color: "var(--ink-2)" }}>
                    {h.text}
                  </div>
                </div>
              ))
            )}
          </div>
        </Card>
      )}

      {tab === "rules" && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
          <Card>
            <CardHead title="分类口径" hint={`会话按 ${rules.data?.window_minutes ?? 5} 分钟窗口切块`} />
            <div className="card-body stack gap-8">
              {rules.data?.categories.map((c) => (
                <div key={c.key} className="row gap-8">
                  <Badge tone={c.keep ? "green" : "gray"}>{c.name}</Badge>
                  <span className="t-xs dim">
                    {c.keep ? "保留" : "丢弃"} · {c.dataset_eligible ? "可进训练集" : "不可训练"}
                  </span>
                </div>
              )) ?? <SkeletonRows rows={4} />}
            </div>
          </Card>

          <Card>
            <CardHead title="脱敏规则" hint="按 priority 抢占，避免身份证与银行卡互相串味" />
            <div className="card-body stack gap-8">
              {rules.data?.masking.map((m) => (
                <div key={m.rule} className="row gap-8">
                  <span className="chip">{m.rule}</span>
                  <span className="t-xs dim">
                    优先级 {m.priority} · {m.kind}
                  </span>
                </div>
              )) ?? <SkeletonRows rows={4} />}
              <div className="hint-text" style={{ marginTop: 6 }}>
                三层都跑：{rules.data?.layers.join(" → ") ?? "清洗 → 写入 → 导出"}
              </div>
            </div>
          </Card>
        </div>
      )}

      <div className="row gap-6 t-xs dim">
        <Icon name="lock" size={13} />
        源库以只读方式挂载（mode=ro），抽取与导出都不会改原始聊天记录。
      </div>
    </div>
  );
}
