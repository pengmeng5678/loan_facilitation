// ============================================================================
// 通用工具：数据拉取、动作提交、阶段元数据、格式化
//
// 阶段元数据是前端与后端业务语义的"翻译表"：
// 后端只回 stage=first_called，业务员看到的是「已首呼 · 下一步：添加微信」。
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./api";

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string;
  reload: () => void;
}

/** 拉取一次数据；挂载时执行，reload() 手动刷新。刷新时保留旧数据避免页面闪白。 */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loader()
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setError("");
        }
      })
      .catch((exc: unknown) => {
        if (cancelled) return;
        setError(exc instanceof ApiError ? exc.message : String(exc));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, loading, error, reload };
}

/** 一次性动作（按钮触发）：执行中禁用按钮，错误与成功消息交给调用方。 */
export function useAction() {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const run = useCallback(
    async <T,>(fn: () => Promise<T>, onSuccess?: (result: T) => void) => {
      setRunning(true);
      setError("");
      setMessage("");
      try {
        const result = await fn();
        onSuccess?.(result);
        return result;
      } catch (exc: unknown) {
        setError(exc instanceof ApiError ? exc.message : String(exc));
        return undefined;
      } finally {
        setRunning(false);
      }
    },
    [],
  );

  return { run, running, error, message, setMessage, setError };
}

/** 极简 hash 路由：#/customers/ld_xxx 这样刷新也能回到原页面。 */
export function useHashRoute(): [string, (path: string) => void] {
  const read = () => window.location.hash.replace(/^#\/?/, "") || "today";
  const [route, setRoute] = useState(read);

  useEffect(() => {
    const onChange = () => setRoute(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const go = useCallback((path: string) => {
    const next = `#/${path.replace(/^#?\/?/, "")}`;
    if (window.location.hash === next) return;
    window.location.hash = next;
  }, []);

  return [route, go];
}

// ---------------------------------------------------------------------------
// 阶段元数据
// ---------------------------------------------------------------------------
export interface StageMeta {
  key: string;
  label: string;
  short: string;
  /** 当前阶段应做的动作（与后端 STAGE_ACTION_HINT 一致） */
  action: string;
  /** 推进到的下一阶段；终态为 "" */
  next: string;
  tone: "gray" | "blue" | "cyan" | "amber" | "green" | "red";
}

export const STAGES: StageMeta[] = [
  { key: "new", label: "新线索", short: "新线索", action: "分配业务员", next: "assigned", tone: "gray" },
  { key: "assigned", label: "待首呼", short: "待首呼", action: "完成首呼", next: "first_called", tone: "blue" },
  { key: "first_called", label: "已首呼", short: "已首呼", action: "添加微信", next: "wechat_added", tone: "blue" },
  { key: "wechat_added", label: "已加微", short: "已加微", action: "开始跟进", next: "nurturing", tone: "blue" },
  { key: "nurturing", label: "跟进中", short: "跟进", action: "发出邀约", next: "invited", tone: "cyan" },
  { key: "invited", label: "已邀约", short: "已邀约", action: "确认到店", next: "arrived", tone: "cyan" },
  { key: "arrived", label: "已到店", short: "到店", action: "生成到店准备", next: "proposal_ready", tone: "amber" },
  { key: "proposal_ready", label: "方案就绪", short: "方案", action: "费用试算", next: "fee_estimated", tone: "amber" },
  { key: "fee_estimated", label: "费用就绪", short: "费用", action: "确认费用并签约", next: "signed", tone: "amber" },
  { key: "signed", label: "已成单", short: "成单", action: "—", next: "", tone: "green" },
  { key: "lost", label: "已流失", short: "流失", action: "—", next: "", tone: "red" },
];

export const STAGE_MAP: Record<string, StageMeta> = Object.fromEntries(
  STAGES.map((s) => [s.key, s]),
);

/** 业务员视角的阶段名（比后端枚举更短、更口语） */
export function stageLabel(stage: string): string {
  return STAGE_MAP[stage]?.label ?? stage;
}

export function stageAction(stage: string): string {
  return STAGE_MAP[stage]?.action ?? "";
}

export function nextStage(stage: string): string {
  return STAGE_MAP[stage]?.next ?? "";
}

export function stageTone(stage: string): string {
  return STAGE_MAP[stage]?.tone ?? "gray";
}

/** 进度：已完成 stage 数 / 10（不含流失） */
export function stageProgress(stage: string): number {
  const idx = STAGES.findIndex((s) => s.key === stage);
  if (idx < 0) return 0;
  return Math.round(((idx + 1) / 10) * 100);
}

/** 看板列：把 11 个阶段收成 6 个业务动作列 */
export interface PhaseDef {
  key: string;
  label: string;
  stages: string[];
  hint: string;
}

export const PHASES: PhaseDef[] = [
  { key: "intake", label: "待分配", stages: ["new"], hint: "线索刚进来" },
  { key: "first_touch", label: "待首触", stages: ["assigned", "first_called"], hint: "打电话 + 加微信" },
  { key: "nurture", label: "跟进中", stages: ["wechat_added", "nurturing"], hint: "持续沟通、发方案" },
  { key: "invite", label: "已邀约", stages: ["invited"], hint: "等客户到店" },
  { key: "onsite", label: "到店推进", stages: ["arrived", "proposal_ready", "fee_estimated"], hint: "接待、方案、费用" },
  { key: "done", label: "已成交", stages: ["signed"], hint: "放款收佣" },
];

export function phaseOf(stage: string): string {
  return PHASES.find((p) => p.stages.includes(stage))?.key ?? "intake";
}

/** 漏斗用的 6 段（与看板列一致，便于点漏斗直接筛看板） */
export const FUNNEL = PHASES;

// ---------------------------------------------------------------------------
// 格式化
// ---------------------------------------------------------------------------
export function fmtTime(seconds: number | undefined): string {
  if (!seconds) return "-";
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

export function fmtDate(seconds: number | undefined): string {
  if (!seconds) return "-";
  const d = new Date(seconds * 1000);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

/**
 * 审计/流转里用的绝对时间：2026-10-19 09:30。
 *
 * 不要带秒 —— "2026/10/19 09:00:00" 是数据库口径，业务员看的是"哪一天几点"。
 */
export function fmtStamp(seconds: number | undefined): string {
  if (!seconds) return "-";
  const d = new Date(seconds * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
    d.getMinutes(),
  )}`;
}

/** 相对时间：业务员只关心"等了多久" */
export function fmtAgo(seconds: number | undefined, now = Date.now() / 1000): string {
  if (!seconds) return "-";
  const diff = Math.max(0, Math.floor(now - seconds));
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  const days = Math.floor(diff / 86400);
  if (days < 30) return `${days} 天前`;
  return fmtDate(seconds);
}

/** 已等待时长（用于"这条线索晾了多久"） */
export function fmtWaited(seconds: number | undefined, now = Date.now() / 1000): string {
  if (!seconds) return "-";
  const diff = Math.max(0, Math.floor(now - seconds));
  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))} 分钟`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时`;
  return `${Math.floor(diff / 86400)} 天`;
}

export function fmtMoney(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return "-";
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(value % 10000 === 0 ? 0 : 1)} 万`;
  return value.toLocaleString("zh-CN");
}

/**
 * 金额 + 单位一起给。
 *
 * fmtMoney 只管数字（"30 万"），外面再补" 元"就会变成"30 万 元"这种双单位，
 * 所以凡是展示给客户的金额都走这个函数，只在这里加 ¥。
 */
export function fmtYuan(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return "-";
  return `¥${fmtMoney(value)}`;
}

export function fmtPct(value: number | undefined | null, digits = 0): string {
  if (value == null) return "-";
  return `${(value * 100).toFixed(digits)}%`;
}

/**
 * 通过率分档的展示映射。
 *
 * 后端只给 high/medium/low（不对外给百分比）：0.55 + 0.40*avg 只是准入余量
 * 的启发式映射，没有历史审批数据校准，拿它当"通过率"对客户承诺就是纠纷源。
 * 这里的宽度只是视觉占位，不是概率值。
 */
export const PASS_BAND_META: Record<string, { width: number; color: string }> = {
  high: { width: 88, color: "var(--ok)" },
  medium: { width: 62, color: "var(--warn)" },
  low: { width: 30, color: "var(--danger)" },
};

export function passBandMeta(band: string | undefined | null) {
  return PASS_BAND_META[band || "medium"] || PASS_BAND_META.medium;
}

/** 姓名首字（脱敏名 "李*士" → 李） */
export function initial(name: string): string {
  return (name || "?").trim().charAt(0);
}

const AVATAR_TONES = [
  "background:#eef2ff;color:#1b3aa3",
  "background:#e8f7f0;color:#0b8a5b",
  "background:#fdf3e3;color:#b25e09",
  "background:#e7f4f8;color:#0e6f8c",
  "background:#f3eefe;color:#5b3bb5",
];

export function avatarStyle(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) hash = (hash * 31 + seed.charCodeAt(i)) % 997;
  return AVATAR_TONES[hash % AVATAR_TONES.length];
}

// ---------------------------------------------------------------------------
// 渠道 / 人工节点标签
// ---------------------------------------------------------------------------
export const SOURCE_LABELS: Record<string, string> = {
  douyin: "抖音信息流",
  infoflow: "信息流广告",
  wechat: "微信",
  referral: "老客转介绍",
  offline: "线下门店",
  search: "搜索",
  phone: "电话咨询",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

/** 看板卡片只有一百多像素宽，渠道要用短名，长名会把手机号挤掉 */
export const SOURCE_SHORT_LABELS: Record<string, string> = {
  douyin: "抖音",
  infoflow: "信息流",
  wechat: "微信",
  referral: "转介绍",
  offline: "门店",
  search: "搜索",
  phone: "电话",
};

export function sourceShort(source: string): string {
  return SOURCE_SHORT_LABELS[source] ?? source;
}

export const HUMAN_STAGE_LABELS: Record<string, string> = {
  reception: "接待",
  negotiation: "谈判",
  fee_confirmation: "费用确认",
};

/**
 * 账号 → 人名。
 *
 * 业务员不需要知道 rep_01 是谁，界面上所有地方都应该显示人名。
 * 这是演示用的虚拟顾问，接真实账号体系时换成组织架构里的姓名即可。
 */
export const OWNER_LABELS: Record<string, string> = {
  rep_01: "张晓东",
  rep_02: "李文静",
  rep_03: "王海峰",
  mgr_01: "陈立主管",
};

export function ownerLabel(owner: string): string {
  if (!owner) return "未分配";
  return OWNER_LABELS[owner] ?? owner;
}

/**
 * SOP 动作键 → 业务员看得懂的动作名。
 *
 * 后端的 action 是给规则表用的机器键（first_call / add_wechat ...），
 * 界面必须翻译，否则业务员看到的是「该做：first_call」。
 */
export const ACTION_LABELS: Record<string, string> = {
  first_call: "打首呼",
  add_wechat: "加微信",
  follow_up: "跟进一次",
  confirm_arrival: "确认到店时间",
  readiness: "准备到店材料",
  fee_estimate: "做费用试算",
};

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

/**
 * 审计里的操作者：seed/agent/system 这类机器身份也要说人话。
 *
 * seed 是演示数据载入，业务员看到"初始化"会以为是系统在启动，
 * 所以统一报成"系统"——它确实是系统代记的账。
 */
export const ACTOR_LABELS: Record<string, string> = {
  seed: "系统",
  agent: "AI 助手",
  system: "系统",
  scheduler: "定时任务",
  mgr_01: "陈立主管",
};

export function actorLabel(actor: string): string {
  if (!actor) return "系统";
  return ACTOR_LABELS[actor] ?? OWNER_LABELS[actor] ?? actor;
}

/**
 * 审计详情里会出现 `first_call → lost：原因` 这种机器写法。
 * 业务员看不懂阶段键，这里把两端的阶段名换成中文再展示。
 */
export function auditDetail(detail: string): string {
  const matched = detail.match(/^([a-z_]+)\s*→\s*([a-z_]+)\s*[：:]?\s*(.*)$/);
  const rest = matched
    ? `${stageLabel(matched[1])} → ${stageLabel(matched[2])}${matched[3] ? `：${matched[3]}` : ""}`
    : detail;
  // 详情里还可能夹着 SOP 动作键（"SOP 逾期：first_call 超时…"），一并翻译
  return rest
    .replace(
      /\b(first_call|add_wechat|follow_up|confirm_arrival|readiness|fee_estimate)\b/g,
      (key) => ACTION_LABELS[key] ?? key,
    )
    // 后端格式化出来的是"（ SLA 30 ）"，补上单位和空格
    .replace(/（\s*SLA\s*(\d+)\s*）/g, "（SLA $1 分钟）");
}

/**
 * 审计流水 → 一句话人话。
 *
 * 后端 detail 是审计格式（`key=value`、阶段键、内部 id），直接铺到界面上
 * 业务员只能看到"准备包 pk_2bad6222… 费用=20000.0 耗时 0ms"。
 * 这里按 action 分派翻译，比堆一条大正则好维护，也不会误伤别的字段。
 */
export function auditText(action: string, detail: string): string {
  if (action === "readiness.build") {
    const plan = detail.match(/方案=(\S+)/)?.[1];
    const fee = detail.match(/费用=(\S+)/)?.[1];
    const parts = ["生成到店准备包"];
    if (plan && plan !== "无") parts.push(`主推 ${plan}`);
    if (fee && fee !== "无" && !Number.isNaN(Number(fee))) parts.push(`服务费 ${fmtYuan(Number(fee))}`);
    return parts.join(" · ");
  }

  if (action === "lead.intake") {
    if (detail.includes("重复")) return "重复录入，被幂等键拦下";
    const source = detail.match(/渠道=(\S+)/)?.[1];
    const owner = detail.match(/分配=(\S+)/)?.[1];
    return [
      "录入新线索",
      source ? `来源 ${sourceLabel(source)}` : "",
      owner ? `分给 ${ownerLabel(owner)}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
  }

  if (action === "fee.estimate") {
    // "服务费 6000.0 元" → "服务费 ¥6,000"（"服务费率" 前面没空格，不会被误伤）
    return auditDetail(detail).replace(
      /服务费\s+([\d.]+)\s*元/g,
      (_, value: string) => `服务费 ¥${Number(value).toLocaleString("zh-CN")}`,
    );
  }

  if (action === "lead.invite") {
    if (detail.includes("重复")) return "重复邀约，被幂等键拦下";
    const store = detail.match(/门店=(\S+)/)?.[1];
    const when = detail.match(/时间=([\d\-: ]+)/)?.[1];
    return ["已约客户到店", store ?? "", when ?? ""].filter(Boolean).join(" · ");
  }

  return auditDetail(detail);
}

/** 审批的超时剩余时间：后端只给分钟数，1432 分钟得说成"24 小时内" */
export function fmtLeft(minutes: number | undefined): string {
  if (minutes == null || minutes <= 0) return "已过期";
  if (minutes < 60) return `剩余 ${Math.round(minutes)} 分钟`;
  if (minutes < 1440) return `剩余 ${Math.floor(minutes / 60)} 小时`;
  return `剩余 ${Math.floor(minutes / 1440)} 天`;
}

export const APPROVAL_ACTION_LABELS: Record<string, string> = {
  send_contract: "发送合同",
  confirm_fee: "确认费用",
};

/** 审计动作名 → 中文。后端是 `lead.advance` 这种点分命名，界面不能直接铺 */
export const AUDIT_ACTION_LABELS: Record<string, string> = {
  "lead.intake": "录入线索",
  "lead.advance": "推进阶段",
  "lead.invite": "邀约到店",
  "lead.confirm_arrival": "确认到店",
  "lead.handoff": "转人工",
  "readiness.build": "生成准备包",
  "fee.estimate": "费用试算",
  "sop.alert": "SOP 预警",
  "sop.ack": "预警认领",
  "approval.request.send_contract": "申请发合同",
  "approval.request.confirm_fee": "申请确认费用",
  "approval.execute.send_contract": "执行发合同",
  "approval.execute.confirm_fee": "执行确认费用",
};

export function auditActionLabel(action: string): string {
  return AUDIT_ACTION_LABELS[action] ?? action;
}

/** 审计四态（ok / blocked / failed / idempotent_hit）的人话版本 */
export const AUDIT_RESULT_LABELS: Record<string, string> = {
  ok: "成功",
  blocked: "被拦截",
  failed: "失败",
  idempotent_hit: "重复点击已拦截",
};

export function auditResultLabel(result: string): string {
  return AUDIT_RESULT_LABELS[result] ?? result;
}

export const APPROVAL_STATUS_LABELS: Record<string, string> = {
  pending: "待审批",
  approved: "已批准",
  rejected: "已拒绝",
  expired: "已过期",
};
