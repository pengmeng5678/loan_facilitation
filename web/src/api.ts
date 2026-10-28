// ============================================================================
// API 客户端 —— 前端与后端的唯一接触面
//
// 三件事：
//   ① API Key 从 localStorage 取，随每个请求带 X-API-Key 头；
//   ② 401 统一抛 ApiError(401) → 界面提示"配置 Key"而不是一堆看不懂的报错；
//   ③ 写操作自动带 Idempotency-Key 头，键按**业务语义**生成（不是随机 uuid）
//      —— 随机键防不住重试，这一点与后端 flow_ops/idempotency.py 同源。
// ============================================================================

const API_KEY_STORAGE = "zhudai_api_key";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) ?? "";
}

export function setApiKey(key: string): void {
  localStorage.setItem(API_KEY_STORAGE, key);
}

export function clearApiKey(): void {
  localStorage.removeItem(API_KEY_STORAGE);
}

type RequestOptions = {
  method?: "GET" | "POST";
  body?: unknown;
  idempotencyKey?: string;
  query?: Record<string, string | number | boolean | undefined>;
};

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, idempotencyKey, query } = options;
  const url = new URL(path, window.location.origin);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
    }
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  const resp = await fetch(url.toString(), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (resp.status === 401) {
    throw new ApiError(401, "未授权：请先配置 API Key");
  }
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const payload = await resp.json();
      if (payload && typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // 响应体不是 JSON（比如反代返回 HTML 错误页）就用状态行
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

// ---------------------------------------------------------------------------
// 类型（与后端 Pydantic 模型一一对应；字段名保持后端原样，避免两层命名体系）
// ---------------------------------------------------------------------------
export interface Lead {
  lead_id: string;
  customer_id: string;
  customer_name: string;
  phone_masked: string;
  source: string;
  owner: string;
  stage: string;
  human_stage: string | null;
  lost_reason: string;
  note: string;
  stage_entered_at: number;
  created_at: number;
  updated_at: number;
}

export interface LeadTransition {
  from_stage?: string;
  to_stage?: string;
  reason?: string;
  ts?: number;
  actor?: string;
}

export interface Appointment {
  appointment_id?: string;
  store?: string;
  scheduled_at?: number;
  status?: string;
  note?: string;
}

export interface LeadDetail extends Lead {
  transitions: LeadTransition[];
  appointments: Appointment[];
}

export interface SopAlert {
  alert_id: string;
  lead_id: string;
  customer_name: string;
  owner: string;
  stage: string;
  action: string;
  overdue_minutes: number;
  created_at: number;
  acknowledged_by?: string;
}

export interface Approval {
  approval_id: string;
  action: string;
  lead_id: string;
  payload?: Record<string, unknown>;
  requester: string;
  reason: string;
  status: string;
  created_at: number;
  decided_at?: number;
  decided_by?: string;
  decision_comment?: string;
  expires_at: number;
  /** 实际执行时间；0 表示还没执行（批准 ≠ 已执行） */
  executed_at?: number;
  is_executed?: boolean;
}

export interface FlowMetrics {
  leads: { total: number; active: number; by_stage: Record<string, number> };
  missed_followup_rate: number;
  sop_alerts_open: number;
  idempotent_hits: number;
  hitl: { pending: number; avg_decision_minutes: number };
  prepare: { count: number; avg_ms: number; p95_ms: number };
}

export interface FlowStats {
  enabled: boolean;
  lock_backend: string;
  store: Record<string, number | string>;
  metrics: FlowMetrics;
  high_risk_actions: string[];
}

export interface AuditEntry {
  entry_id: string;
  ts: number;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  detail: string;
  approval_id: string;
  result: string;
}

/** 客户档案摘要（项目二只回脱敏后的画像字段） */
export interface CustomerItem {
  customer_id: string;
  name_masked: string;
  age: number;
  city: string;
  qualifications: string[];
  needs: string[];
  debt_ratio: number;
}

/** 客户档案详情（/product/customers/{id}，只读）——用于预填额度等业务参数 */
export interface CustomerDetail {
  customer_id: string;
  name_masked: string;
  age: number;
  city: string;
  occupation_type: string;
  monthly_income: number;
  qualifications: string[];
  credit_query_6m: number;
  overdue_count_12m: number;
  overdue_days_max: number;
  monthly_debt: number;
  loans_count: number;
  needs: string[];
  desired_amount: number;
  note: string;
  debt_ratio: number;
}

export interface MatchCandidate {
  product_code: string;
  product_name: string;
  bank: string;
  score: number;
  /** 通过率分档（对外口径，不含百分比：最终以银行审批为准） */
  pass_band: "high" | "medium" | "low";
  pass_band_label: string;
  pass_band_reason: string;
  estimated_amount: number;
  rate_display: string;
  reasons: string[];
  risks: string[];
  margin_detail: Record<string, unknown>;
}

export interface MatchResult {
  matched: boolean;
  customer_id: string;
  primary: MatchCandidate | null;
  alternates: MatchCandidate[];
  rejected_summary: Array<Record<string, unknown>>;
  handoff_reason: string;
  weights: Record<string, number>;
}

export interface Proposal {
  proposal_id: string;
  customer_id: string;
  generated_at: string;
  products: Array<Record<string, unknown>>;
  amount_range: string;
  rate_range: string;
  materials: string[];
  process: string[];
  risk_notes: string[];
  citations: string[];
  disclaimer: string;
  markdown: string;
}

export interface QaAnswer {
  question: string;
  answer: string;
  citations: string[];
  refused: boolean;
  reason: string;
  related_products: string[];
  hits: Array<Record<string, unknown>>;
}

export interface FeeEstimate {
  lead_id: string;
  loan_amount: number;
  months: number;
  annual_rate: number;
  monthly_payment: number;
  total_interest: number;
  total_repayment: number;
  service_fee_rate: number;
  service_fee: number;
  net_amount: number;
  basis: string;
  disclaimer: string;
}

export interface ProfileSummary {
  customer_id: string;
  name: string;
  city: string;
  monthly_income: number;
  social_insurance_months: number;
  housing_fund_months: number;
  has_property: boolean;
  has_vehicle: boolean;
  has_policy: boolean;
  has_business_license: boolean;
  credit_overdue_times: number;
  credit_query_times_90d: number;
  debt_total: number;
  microloan_count: number;
  need_amount: number;
  need_purpose: string;
}

export interface ReadinessPack {
  pack_id: string;
  lead_id: string;
  customer_id: string;
  profile: ProfileSummary;
  highlights: string[];
  risks: string[];
  plan_draft: {
    customer_id: string;
    product_code: string;
    product_name: string;
    summary: string;
    citations: string[];
    rejected: Array<Record<string, unknown>>;
    handoff_reason: string;
  } | null;
  fee_estimate: FeeEstimate | null;
  talking_points: string[];
  warnings: string[];
  prepared_in_ms: number;
  created_at: number;
}

export interface ProgressSnapshot {
  lead_id: string;
  customer_name: string;
  current_stage: string;
  stage_label: string;
  next_step: string;
  appointment: { store: string; scheduled_at: number; time_text: string; status: string } | null;
  timeline: Array<{ stage: string; label: string; ts: number; time_text: string }>;
  updated_at: number;
}

// ---------------------------------------------------------------------------
// 项目四：数据中台
// ---------------------------------------------------------------------------
export interface DataPipeline {
  raw_messages: number;
  sessions: number;
  blocks: number;
  dropped: number;
  drop_rate: number;
  pending: number;
  approved: number;
  rejected: number;
  approved_eligible: number;
  exports: number;
  chunks: number;
  articles: number;
  by_category: Record<string, number>;
}

export interface DataRules {
  masking: Array<{ rule: string; priority: number; kind: string; note: string }>;
  categories: Array<{ key: string; name: string; keep: boolean; dataset_eligible: boolean }>;
  layers: string[];
  window_minutes: number;
}

export interface StagedItem {
  staging_id: string;
  block_id: string;
  status: string;
  revision: number;
  category: string;
  quality_score: number;
  dataset_eligible: boolean;
  session_id: string;
  customer_id: string;
  messages: Array<{ role: string; content: string; ts?: number }>;
  findings_count: number;
  price_hits: number;
}

export interface ExportRecord {
  export_id: string;
  format: string;
  path: string;
  rows: number;
  bytes: number;
  content_hash: string;
  watermark: string;
  reused: boolean;
}

export interface KnowledgeHit {
  chunk_id: string;
  staging_id: string;
  scene: string;
  text: string;
  score: number;
}

// ---------------------------------------------------------------------------
// 端点
// ---------------------------------------------------------------------------
export const api = {
  health: () => request<{ status: string }>("/health"),
  ready: () => request<Record<string, unknown>>("/ready"),

  data: {
    pipeline: () => request<DataPipeline>("/data/pipeline"),
    rules: () => request<DataRules>("/data/rules"),
    ingest: (dryRun: boolean) =>
      request<Record<string, unknown>>("/data/ingest", {
        method: "POST",
        body: { dry_run: dryRun },
      }),
    staging: (status = "pending", limit = 20) =>
      request<{ count: number; items: StagedItem[]; counts: Record<string, number> }>(
        "/data/staging",
        { query: { status, limit } },
      ),
    review: (stagingId: string, action: string, comment = "") =>
      request<{ item: StagedItem }>(`/data/staging/${stagingId}/review`, {
        method: "POST",
        body: { action, reviewer: "web_operator", comment },
      }),
    exportDataset: (format: string) =>
      request<ExportRecord>("/data/export", { method: "POST", body: { format } }),
    exports: () => request<{ count: number; items: ExportRecord[] }>("/data/exports"),
    buildKnowledge: () =>
      request<Record<string, unknown>>("/data/knowledge/build", { method: "POST", body: {} }),
    searchKnowledge: (q: string, topK = 3) =>
      request<{ count: number; hits: KnowledgeHit[] }>("/data/knowledge/search", {
        query: { q, top_k: topK },
      }),
    feed: () => request<{ count: number; items: Array<Record<string, unknown>> }>("/data/feed"),
  },

  flow: {
    leads: (stage = "", owner = "") =>
      request<{ count: number; leads: Lead[] }>("/flow/leads", { query: { stage, owner } }),
    leadDetail: (leadId: string) => request<LeadDetail>(`/flow/leads/${leadId}`),
    intake: (body: {
      customer_id: string;
      customer_name: string;
      phone_masked: string;
      source: string;
      owner: string;
      note?: string;
    }) =>
      request<Lead>("/flow/leads", {
        method: "POST",
        body,
        idempotencyKey: `intake:${body.customer_id}:${body.source}:${body.owner}`,
      }),
    advance: (leadId: string, toStage: string, reason = "") =>
      request<Lead>(`/flow/leads/${leadId}/advance`, {
        method: "POST",
        body: { to_stage: toStage, reason },
      }),
    invite: (leadId: string, store: string, scheduledAt: number, note = "") =>
      request<Record<string, unknown>>(`/flow/leads/${leadId}/invite`, {
        method: "POST",
        body: { store, scheduled_at: scheduledAt, note },
        idempotencyKey: `invite:${leadId}:${store}:${Math.floor(scheduledAt)}`,
      }),
    arrived: (leadId: string) =>
      request<Lead>(`/flow/leads/${leadId}/arrived`, {
        method: "POST",
        idempotencyKey: `arrive:${leadId}`,
      }),
    readiness: (leadId: string) =>
      request<ReadinessPack>(`/flow/leads/${leadId}/readiness`, { method: "POST" }),
    fee: (leadId: string, loanAmount: number) =>
      request<FeeEstimate>(`/flow/leads/${leadId}/fee`, {
        method: "POST",
        body: { loan_amount: loanAmount },
      }),
    handoff: (leadId: string, humanStage: string, note = "") =>
      request<Lead>(`/flow/leads/${leadId}/handoff`, {
        method: "POST",
        body: { human_stage: humanStage, note },
      }),
    alerts: (includeAcknowledged = false) =>
      request<{ count: number; alerts: SopAlert[] }>("/flow/sop/alerts", {
        query: { include_acknowledged: includeAcknowledged },
      }),
    sweep: () => request<Record<string, unknown>>("/flow/sop/sweep", { method: "POST" }),
    ackAlert: (alertId: string, by = "mgr_01") =>
      request<Record<string, unknown>>(`/flow/sop/alerts/${alertId}/ack`, {
        method: "POST",
        query: { by },
      }),
    approvals: (status = "") =>
      request<{ count: number; approvals: Approval[] }>("/flow/approvals", { query: { status } }),
    requestContract: (leadId: string, reason: string) =>
      request<Approval>(`/flow/leads/${leadId}/request-contract`, {
        method: "POST",
        body: { reason },
      }),
    requestFeeConfirm: (leadId: string, reason: string) =>
      request<Approval>(`/flow/leads/${leadId}/confirm-fee`, {
        method: "POST",
        body: { reason },
      }),
    decide: (approvalId: string, approver: string, approved: boolean, comment = "") =>
      request<Approval>(`/flow/approvals/${approvalId}/decide`, {
        method: "POST",
        body: { approver, approved, comment },
      }),
    execute: (approvalId: string) =>
      request<Record<string, unknown>>(`/flow/approvals/${approvalId}/execute`, { method: "POST" }),
    progress: (leadId: string, phoneLast4: string) =>
      request<ProgressSnapshot>("/flow/progress", {
        method: "POST",
        body: { lead_id: leadId, phone_last4: phoneLast4 },
      }),
    audit: (leadId = "", limit = 30) =>
      request<{ count: number; entries: AuditEntry[] }>("/flow/audit", {
        query: { lead_id: leadId, limit },
      }),
    stats: () => request<FlowStats>("/flow/stats"),
    metrics: () => request<FlowMetrics>("/flow/metrics"),
  },

  product: {
    qa: (question: string, productCode = "") =>
      request<QaAnswer>("/product/qa", {
        method: "POST",
        body: { question, product_code: productCode },
      }),
    customers: () => request<{ count: number; customers: CustomerItem[] }>("/product/customers"),
    customer: (customerId: string) => request<CustomerDetail>(`/product/customers/${customerId}`),
    match: (customerId: string) =>
      request<MatchResult>("/product/match", { method: "POST", body: { customer_id: customerId } }),
    proposal: (customerId: string) =>
      request<Proposal>("/product/proposal", { method: "POST", body: { customer_id: customerId } }),
    products: () => request<{ count: number; products: Array<Record<string, unknown>> }>("/product/products"),
  },
};
