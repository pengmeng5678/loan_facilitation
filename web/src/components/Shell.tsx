// 应用外壳：深色侧栏 + 浅色内容区。
// 分组不是按技术模块，而是按业务员一天的动作顺序排：今天 → 客户 → 成交 → 服务。
//
// 手机上换一套导航：业务员是在店里、车上、客户对面掏手机用的，
// 深色竖侧栏在 390px 屏上只能缩成一列看不懂的图标，所以窄屏改成
// 底部 4 + 「更多」的 tab bar（拇指够得到），侧栏整条隐藏。
import { useState, type ReactNode } from "react";
import { Icon, type IconName } from "./icons";
import { Avatar } from "./ui";
import { ownerLabel } from "../hooks";

export type RouteKey =
  | "today"
  | "customers"
  | "solution"
  | "approvals"
  | "progress"
  | "alerts"
  | "team"
  | "data";

interface NavItem {
  key: RouteKey;
  label: string;
  icon: IconName;
  hint: string;
}

const NAV: Array<{ group: string; items: NavItem[] }> = [
  {
    group: "工作台",
    items: [{ key: "today", label: "今日工作台", icon: "today", hint: "今天该跟进谁、有哪些卡住的事" }],
  },
  {
    group: "客户",
    items: [{ key: "customers", label: "我的客户", icon: "users", hint: "从线索到成交的全流程推进" }],
  },
  {
    group: "成交支持",
    items: [
      { key: "solution", label: "方案中心", icon: "sparkles", hint: "产品政策问答 + 客户方案匹配" },
      { key: "approvals", label: "审批中心", icon: "shield", hint: "合同发送、费用确认必须人工批" },
    ],
  },
  {
    group: "客户服务",
    items: [
      { key: "progress", label: "客户查进度", icon: "phone", hint: "把脱敏进度页给客户看" },
      { key: "alerts", label: "跟进预警", icon: "alert", hint: "超时未跟进的线索" },
    ],
  },
  {
    group: "管理",
    items: [
      { key: "team", label: "团队看板", icon: "activity", hint: "转化漏斗与团队健康度" },
      { key: "data", label: "数据资产", icon: "database", hint: "对话语料清洗、审核与导出" },
    ],
  },
];

const TITLES: Record<RouteKey, { title: string; sub: string }> = {
  today: { title: "今日工作台", sub: "先处理超时和待办，再看数据" },
  customers: { title: "我的客户", sub: "按业务阶段推进，每一步只做一件事" },
  solution: { title: "方案中心", sub: "客户资质 × 产品准入，给出可解释的方案" },
  approvals: { title: "审批中心", sub: "高风险动作：批准不等于已执行" },
  progress: { title: "客户查进度", sub: "脱敏视图，客户只看得到自己的节点" },
  alerts: { title: "跟进预警", sub: "系统兜底：漏跟进会被自动揪出来" },
  team: { title: "团队看板", sub: "线索转化漏斗与流程健康度" },
  data: { title: "数据资产", sub: "对话 → 清洗 → 审核 → 语料与知识库" },
};

/** 手机底部常驻的 4 个入口：业务员一天里真正会反复点的就这几个 */
const TABS: Array<{ key: RouteKey; label: string; icon: IconName }> = [
  { key: "today", label: "今日", icon: "today" },
  { key: "customers", label: "客户", icon: "users" },
  { key: "solution", label: "方案", icon: "sparkles" },
  { key: "approvals", label: "审批", icon: "shield" },
];

export function Shell({
  route,
  onNavigate,
  owner,
  owners,
  onOwnerChange,
  pendingCount,
  alertCount,
  onShowFlow,
  children,
}: {
  route: RouteKey;
  onNavigate: (key: RouteKey) => void;
  owner: string;
  owners: Array<{ value: string; label: string }>;
  onOwnerChange: (owner: string) => void;
  pendingCount: number;
  alertCount: number;
  onShowFlow: () => void;
  children: ReactNode;
}) {
  const meta = TITLES[route];
  const [moreOpen, setMoreOpen] = useState(false);

  const go = (key: RouteKey) => {
    setMoreOpen(false);
    onNavigate(key);
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">贷</div>
          <div className="brand-text">
            <div className="brand-name">助贷业务工作台</div>
            <div className="brand-sub">AI 中台 · 南京</div>
          </div>
        </div>

        <nav className="nav">
          {NAV.map((group) => (
            <div className="nav-group" key={group.group}>
              <div className="nav-label">{group.group}</div>
              {group.items.map((item) => {
                const active = route === item.key;
                const badge =
                  item.key === "approvals" ? pendingCount : item.key === "alerts" ? alertCount : 0;
                return (
                  <button
                    key={item.key}
                    className={active ? "nav-item active" : "nav-item"}
                    onClick={() => onNavigate(item.key)}
                    title={item.hint}
                  >
                    <span className="nav-icon">
                      <Icon name={item.icon} size={17} />
                    </span>
                    <span className="nav-text">{item.label}</span>
                    {badge > 0 && <span className="nav-badge">{badge > 99 ? "99+" : badge}</span>}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <Avatar name={owner || "陈立主管"} size="sm" />
          <div className="who">
            <b>{ownerLabel(owner || "mgr_01")}</b>
            <span>{owner ? "业务顾问 · 试用账号" : "主管视角 · 全部业务员"}</span>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="page-head">
            <div className="page-title">{meta.title}</div>
            <div className="page-sub">{meta.sub}</div>
          </div>

          <div className="topbar-actions">
            <button
              className="btn btn-sm btn-ghost flow-btn"
              onClick={onShowFlow}
              title="用 1 分钟看懂这套系统怎么跑"
            >
              <Icon name="layers" size={15} />
              <span className="flow-btn-text">业务链路</span>
            </button>

            <label className="row gap-6 t-sm dim topbar-identity" style={{ whiteSpace: "nowrap" }}>
              当前身份
              <select
                className="select"
                style={{ height: 30, width: 108, fontSize: 12.5 }}
                value={owner}
                onChange={(e) => onOwnerChange(e.target.value)}
              >
                {owners.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>

            <button
              className={alertCount > 0 ? "icon-btn has-dot" : "icon-btn"}
              onClick={() => onNavigate("alerts")}
              title={alertCount > 0 ? `${alertCount} 条待处理预警` : "暂无预警"}
              aria-label="预警"
            >
              <Icon name="bell" size={18} />
            </button>
          </div>
        </header>

        <main className="content">{children}</main>
      </div>

      {/* 仅在窄屏显示：底部 tab bar + 更多 */}
      <nav className="tabbar" aria-label="主导航">
        {TABS.map((t) => {
          const badge = t.key === "approvals" ? pendingCount : 0;
          return (
            <button
              key={t.key}
              className={route === t.key ? "tab active" : "tab"}
              onClick={() => go(t.key)}
            >
              <span className="tab-icon">
                <Icon name={t.icon} size={20} />
                {badge > 0 && <span className="tab-dot" />}
              </span>
              <span className="tab-label">{t.label}</span>
            </button>
          );
        })}
        <button className={moreOpen ? "tab active" : "tab"} onClick={() => setMoreOpen(true)}>
          <span className="tab-icon">
            <Icon name="more" size={20} />
            {alertCount > 0 && <span className="tab-dot" />}
          </span>
          <span className="tab-label">更多</span>
        </button>
      </nav>

      {moreOpen && (
        <div className="sheet-mask" onClick={() => setMoreOpen(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="sheet-head">
              <b>全部功能</b>
              <button className="icon-btn" onClick={() => setMoreOpen(false)} aria-label="关闭">
                <Icon name="x" size={18} />
              </button>
            </div>

            <div className="sheet-body">
              {NAV.map((group) => (
                <div key={group.group}>
                  <div className="sheet-label">{group.group}</div>
                  {group.items.map((item) => (
                    <button
                      key={item.key}
                      className={route === item.key ? "sheet-item active" : "sheet-item"}
                      onClick={() => go(item.key)}
                    >
                      <span className="sheet-ico">
                        <Icon name={item.icon} size={17} />
                      </span>
                      <span className="sheet-copy">
                        <b>{item.label}</b>
                        <span>{item.hint}</span>
                      </span>
                      {((item.key === "approvals" && pendingCount) ||
                        (item.key === "alerts" && alertCount)) ? (
                        <span className="nav-badge">{item.key === "approvals" ? pendingCount : alertCount}</span>
                      ) : null}
                    </button>
                  ))}
                </div>
              ))}

              <div className="sheet-label">演示与身份</div>
              <button className="sheet-item" onClick={() => { setMoreOpen(false); onShowFlow(); }}>
                <span className="sheet-ico">
                  <Icon name="layers" size={17} />
                </span>
                <span className="sheet-copy">
                  <b>业务链路一页看懂</b>
                  <span>从投流留资到放款收费，1 分钟走完全流程</span>
                </span>
              </button>

              <div className="sheet-owner">
                <span className="t-sm dim">当前身份</span>
                <select
                  className="select"
                  value={owner}
                  onChange={(e) => onOwnerChange(e.target.value)}
                >
                  {owners.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
