// ============================================================================
// 应用装配：路由 / 身份 / 鉴权 / 全局刷新
//
// 路由用 hash（#/customers），因为生产由 FastAPI 挂在 /ui 下，
// 没有单独的 SPA fallback 规则；hash 路由刷新不会 404。
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, clearApiKey, getApiKey, setApiKey } from "./api";
import { useHashRoute } from "./hooks";
import { Shell, type RouteKey } from "./components/Shell";
import { Button, Modal, Notice, ToastProvider, useToast } from "./components/ui";
import { Icon } from "./components/icons";
import { TodayPage } from "./pages/TodayPage";
import { CustomersPage } from "./pages/CustomersPage";
import { SolutionPage } from "./pages/SolutionPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { ProgressPage } from "./pages/ProgressPage";
import { AlertsPage } from "./pages/AlertsPage";
import { TeamPage } from "./pages/TeamPage";
import { DataPage } from "./pages/DataPage";

const OWNER_KEY = "zhudai_owner";

const OWNERS = [
  { value: "", label: "主管视角 · 全部线索" },
  { value: "rep_01", label: "张晓东 · 南京" },
  { value: "rep_02", label: "李文静 · 上海" },
  { value: "rep_03", label: "王海峰 · 深圳" },
];

const ROUTES: RouteKey[] = ["today", "customers", "solution", "approvals", "progress", "alerts", "team", "data"];

export default function App() {
  return (
    <ToastProvider>
      <Workbench />
    </ToastProvider>
  );
}

function Workbench() {
  const { notify } = useToast();
  const [route, go] = useHashRoute();
  const [owner, setOwner] = useState(() => localStorage.getItem(OWNER_KEY) ?? "");
  const [reloadKey, setReloadKey] = useState(0);
  const [counts, setCounts] = useState({ pending: 0, alerts: 0 });
  const [flowOpen, setFlowOpen] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [keyInput, setKeyInput] = useState(getApiKey());

  const [path, query] = route.split("?");
  const page = (ROUTES.includes(path as RouteKey) ? path : "today") as RouteKey;
  const phase = useMemo(() => new URLSearchParams(query ?? "").get("phase") ?? "", [query]);

  const bump = useCallback(() => setReloadKey((k) => k + 1), []);

  const loadCounts = useCallback(async () => {
    try {
      const [a, b] = await Promise.all([api.flow.approvals("pending"), api.flow.alerts(false)]);
      setCounts({ pending: a.count, alerts: b.count });
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 401) setAuthOpen(true);
    }
  }, []);

  useEffect(() => {
    void loadCounts();
    const timer = window.setInterval(() => void loadCounts(), 20000);
    return () => window.clearInterval(timer);
  }, [loadCounts, reloadKey]);

  const changeOwner = (next: string) => {
    setOwner(next);
    localStorage.setItem(OWNER_KEY, next);
  };

  const saveKey = () => {
    setApiKey(keyInput.trim());
    setAuthOpen(false);
    notify("API Key 已保存");
    bump();
  };

  return (
    <>
      <Shell
        route={page}
        onNavigate={(key) => go(key)}
        owner={owner}
        owners={OWNERS}
        onOwnerChange={changeOwner}
        pendingCount={counts.pending}
        alertCount={counts.alerts}
        onShowFlow={() => setFlowOpen(true)}
      >
        {page === "today" && <TodayPage owner={owner} onNavigate={go} reloadKey={reloadKey} />}
        {page === "customers" && <CustomersPage owner={owner} initialPhase={phase} onChanged={bump} />}
        {page === "solution" && <SolutionPage />}
        {page === "approvals" && <ApprovalsPage approver={owner || "mgr_01"} onChanged={bump} />}
        {page === "progress" && <ProgressPage />}
        {page === "alerts" && <AlertsPage owner={owner} onChanged={bump} />}
        {page === "team" && <TeamPage owner={owner} />}
        {page === "data" && <DataPage />}
      </Shell>

      {/* 业务链路说明：面试官/新同事 1 分钟看懂系统在干什么 */}
      <Modal
        open={flowOpen}
        onClose={() => setFlowOpen(false)}
        title="这套系统怎么跑"
        hint="一条助贷业务链路，每个环节都有 AI 在做具体的活，但成交永远留给人"
        footer={<Button onClick={() => setFlowOpen(false)}>知道了</Button>}
      >
        <div className="stack gap-14">
          {[
            {
              n: "1",
              t: "线索进来",
              d: "抖音/信息流广告留资，落地页把姓名+手机号写进系统，自动分配业务员并开始 SOP 计时。重复提交会被幂等键拦下。",
            },
            {
              n: "2",
              t: "首触与接待",
              d: "AI 客服 7×24 用语音或文字接第一轮：问需求、看资质、初判征信，按政策库回答（带引用，没依据就拒答）。高意向转给业务员。",
            },
            {
              n: "3",
              t: "跟进与配方案",
              d: "业务员照着系统给的下一步动作推进；方案中心按资质×征信×需求做准入硬筛，给出主力+备选，并写清为什么不用别的产品。",
            },
            {
              n: "4",
              t: "邀约到店",
              d: "顾问在客户详情里发起邀约，客户侧进度页能看到门店与时间，不用反复打电话问。",
            },
            {
              n: "5",
              t: "到店成交",
              d: "到店前系统生成一页纸（档案+方案+费用+见面先聊哪三句）。接待、谈判、费用确认是人做的事，AI 只做准备。",
            },
            {
              n: "6",
              t: "沉淀与复盘",
              d: "对话回流数据中台：清洗、脱敏、人工审核后变成训练语料与知识库；陪练与质检用它出题，素材用它写案例。",
            },
          ].map((s) => (
            <div className="row gap-12" key={s.n} style={{ alignItems: "flex-start" }}>
              <div
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: 8,
                  background: "var(--brand-soft)",
                  color: "var(--brand-ink)",
                  display: "grid",
                  placeItems: "center",
                  fontSize: 12,
                  fontWeight: 600,
                  flex: "none",
                }}
              >
                {s.n}
              </div>
              <div className="grow">
                <div className="t-sm w-600">{s.t}</div>
                <div className="t-sm" style={{ color: "var(--ink-2)", marginTop: 3, lineHeight: 1.65 }}>
                  {s.d}
                </div>
              </div>
            </div>
          ))}
          <Notice tone="info" icon="shield">
            三条底线：<b>业务判据用代码不用模型</b>（准入、评分、合规、SOP 逾期）；
            <b>高风险动作必须人批</b>（发合同、确认费用、外发素材）；
            <b>合规是流程约束</b>（素材先审后发、语音逐句过红线）。
          </Notice>
        </div>
      </Modal>

      {/* API Key */}
      <Modal
        open={authOpen}
        onClose={() => setAuthOpen(false)}
        title="需要访问凭证"
        hint="后端启用了 API_AUTH_ENABLED，填一把 API_AUTH_KEYS 里的 Key 即可"
        footer={
          <>
            <Button
              onClick={() => {
                clearApiKey();
                setKeyInput("");
                notify("已清除本地 Key");
              }}
            >
              清除
            </Button>
            <Button variant="primary" icon="lock" onClick={saveKey}>
              保存并继续
            </Button>
          </>
        }
      >
        <div className="stack gap-12">
          <div className="field">
            <label>API Key</label>
            <input
              className="input"
              value={keyInput}
              onChange={(e) => setKeyInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && saveKey()}
              placeholder="粘贴 Key 后回车"
              autoFocus
            />
          </div>
          <div className="row gap-6 t-xs dim">
            <Icon name="lock" size={13} />
            Key 只存在浏览器 localStorage，随请求走 X-API-Key 头，不会写进代码。
          </div>
        </div>
      </Modal>
    </>
  );
}
