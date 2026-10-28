// 业务进度条：把 11 个后端阶段翻译成业务员看得懂的 10 步。
// 这是整个工作台里"业务怎么流转"最直观的一屏 —— 客户现在在哪、下一步该做什么。
import { STAGES } from "../hooks";

export function StageRail({ stage }: { stage: string }) {
  const steps = STAGES.filter((s) => s.key !== "lost");
  const currentIndex = steps.findIndex((s) => s.key === stage);
  const lost = stage === "lost";

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 9 }}>
        <span className="t-sm w-500">业务进度</span>
        <span className="t-xs" style={{ color: lost ? "var(--danger)" : "var(--ink-3)" }}>
          {lost ? "已流失 · 需要重新激活" : `${Math.max(currentIndex + 1, 1)} / ${steps.length} 步`}
        </span>
      </div>
      <div className="stage-rail">
        {steps.map((s, i) => {
          const done = !lost && i < currentIndex;
          const current = !lost && i === currentIndex;
          return (
            <div key={s.key} className={`stage-step ${done ? "done" : ""} ${current ? "current" : ""}`}>
              <div className="stage-bar" />
              <div className="stage-name">{s.short}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
