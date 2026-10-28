// 基础组件：把"按钮/卡片/抽屉/表单/提示"这类重复样式收敛到一处。
// 业务页面只关心内容，不关心圆角与间距。
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { Icon, type IconName } from "./icons";
import { avatarStyle, initial, stageTone } from "../hooks";

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------
type ToastItem = { id: number; text: string; kind: "ok" | "error" | "info" };

const ToastCtx = createContext<{ notify: (text: string, kind?: ToastItem["kind"]) => void }>({
  notify: () => undefined,
});

export function useToast() {
  return useContext(ToastCtx);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const notify = useCallback((text: string, kind: ToastItem["kind"] = "ok") => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev.slice(-2), { id, text, kind }]);
    window.setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, 2600);
  }, []);

  const value = useMemo(() => ({ notify }), [notify]);

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="toast-wrap">
        {items.map((t) => (
          <div key={t.id} className={t.kind === "error" ? "toast error" : "toast"}>
            <Icon name={t.kind === "error" ? "alert" : "check"} size={15} />
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ---------------------------------------------------------------------------
// 按钮
// ---------------------------------------------------------------------------
type Variant = "default" | "primary" | "ghost" | "soft" | "danger" | "ok";

export function Button({
  children,
  onClick,
  variant = "default",
  size = "md",
  icon,
  iconRight,
  disabled,
  loading,
  block,
  title,
  type = "button",
}: {
  children?: ReactNode;
  onClick?: () => void;
  variant?: Variant;
  size?: "sm" | "md" | "lg";
  icon?: IconName;
  iconRight?: IconName;
  disabled?: boolean;
  loading?: boolean;
  block?: boolean;
  title?: string;
  type?: "button" | "submit";
}) {
  const cls = [
    "btn",
    variant !== "default" ? `btn-${variant}` : "",
    size !== "md" ? `btn-${size}` : "",
    block ? "btn-block" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button className={cls} onClick={onClick} disabled={disabled || loading} title={title} type={type}>
      {loading ? <Spinner /> : icon ? <Icon name={icon} size={size === "sm" ? 14 : 16} /> : null}
      {children}
      {iconRight && !loading ? <Icon name={iconRight} size={size === "sm" ? 14 : 16} /> : null}
    </button>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return (
    <span
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        border: "2px solid currentColor",
        borderRightColor: "transparent",
        display: "inline-block",
        opacity: 0.7,
        animation: "spin 0.7s linear infinite",
      }}
    />
  );
}

// ---------------------------------------------------------------------------
// 徽章 / 头像 / 指标
// ---------------------------------------------------------------------------
export function Badge({
  children,
  tone = "gray",
  dot,
}: {
  children: ReactNode;
  tone?: "gray" | "blue" | "green" | "amber" | "red" | "cyan";
  dot?: boolean;
}) {
  return (
    <span className={`badge badge-${tone}`}>
      {dot && <i className="badge-dot" />}
      {children}
    </span>
  );
}

export function StageBadge({ stage, label }: { stage: string; label: string }) {
  const tone = stageTone(stage) as "gray" | "blue" | "green" | "amber" | "red" | "cyan";
  return (
    <Badge tone={tone} dot>
      {label}
    </Badge>
  );
}

export function Avatar({
  name,
  size = "md",
}: {
  name: string;
  size?: "sm" | "md" | "lg";
}) {
  const cls = ["avatar", size === "sm" ? "avatar-sm" : "", size === "lg" ? "avatar-lg" : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} style={parseStyle(avatarStyle(name))}>
      {initial(name)}
    </div>
  );
}

function parseStyle(css: string): Record<string, string> {
  const out: Record<string, string> = {};
  css.split(";").forEach((pair) => {
    const [k, v] = pair.split(":");
    if (k && v) out[k.trim()] = v.trim();
  });
  return out;
}

export function Metric({
  label,
  value,
  unit,
  foot,
  tone,
  icon,
  onClick,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  foot?: ReactNode;
  tone?: "danger" | "warn";
  icon?: IconName;
  onClick?: () => void;
}) {
  return (
    <div
      className={`metric ${tone ? `is-${tone}` : ""}`}
      onClick={onClick}
      style={onClick ? { cursor: "pointer" } : undefined}
    >
      <div className="metric-label">
        {icon && <Icon name={icon} size={14} />}
        {label}
      </div>
      <div className="metric-value">
        {value}
        {unit && <small>{unit}</small>}
      </div>
      {foot && <div className="metric-foot">{foot}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 容器
// ---------------------------------------------------------------------------
export function Card({
  children,
  pad,
  className,
  hover,
  style,
}: {
  children: ReactNode;
  pad?: boolean;
  className?: string;
  hover?: boolean;
  style?: CSSProperties;
}) {
  const cls = ["card", pad ? "card-pad" : "", hover ? "card-hover" : "", className ?? ""]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} style={style}>
      {children}
    </div>
  );
}

export function CardHead({
  title,
  hint,
  right,
}: {
  title: ReactNode;
  hint?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="card-head">
      <div className="grow">
        <h3>{title}</h3>
        {hint && <div className="hint">{hint}</div>}
      </div>
      {right}
    </div>
  );
}

export function Empty({
  icon = "file",
  title,
  desc,
  action,
}: {
  icon?: IconName;
  title: string;
  desc?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Icon name={icon} size={20} />
      </div>
      <div className="empty-title">{title}</div>
      {desc && <div className="empty-desc">{desc}</div>}
      {action && <div style={{ marginTop: 14 }}>{action}</div>}
    </div>
  );
}

export function SkeletonRows({ rows = 3, height = 46 }: { rows?: number; height?: number }) {
  return (
    <div>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height, marginBottom: 8, opacity: 1 - i * 0.15 }} />
      ))}
    </div>
  );
}

export function Notice({
  tone = "info",
  children,
  icon,
}: {
  tone?: "info" | "warn" | "danger" | "ok";
  children: ReactNode;
  icon?: IconName;
}) {
  const iconName = icon ?? (tone === "danger" || tone === "warn" ? "alert" : "shield");
  return (
    <div className={`notice notice-${tone}`}>
      <span className="notice-icon">
        <Icon name={iconName} size={15} />
      </span>
      <div>{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 表单
// ---------------------------------------------------------------------------
export function Field({
  label,
  children,
  hint,
  grow,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
  grow?: boolean;
}) {
  return (
    <div className="field" style={grow ? { flex: 1 } : undefined}>
      <label>{label}</label>
      {children}
      {hint && <span className="hint-text">{hint}</span>}
    </div>
  );
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`input ${props.className ?? ""}`} />;
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={`textarea ${props.className ?? ""}`} />;
}

export function Select({
  value,
  onChange,
  options,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  className?: string;
}) {
  return (
    <select className={`select ${className ?? ""}`} value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: Array<{ value: T; label: string }>;
}) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button key={o.value} className={value === o.value ? "on" : ""} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 抽屉 / 弹层
// ---------------------------------------------------------------------------
export function Drawer({
  open,
  onClose,
  children,
  width,
}: {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  width?: number;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="overlay" onClick={onClose}>
      <div
        className="drawer"
        // 宽度用 CSS 变量传，不用 inline width：手机上要整屏铺满，
        // inline 的 width 是媒体查询覆盖不掉的（会永远留一条缝露出底下的页面）
        style={width ? ({ "--drawer-w": `${width}px` } as CSSProperties) : undefined}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

export function Modal({
  open,
  onClose,
  title,
  hint,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  hint?: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="overlay center" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="row" style={{ justifyContent: "space-between", gap: 12 }}>
            <div>
              <h3 style={{ fontSize: 15 }}>{title}</h3>
              {hint && <div className="hint-text" style={{ marginTop: 3 }}>{hint}</div>}
            </div>
            <button className="icon-btn" onClick={onClose} aria-label="关闭">
              <Icon name="x" size={17} />
            </button>
          </div>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function DrawerHead({
  children,
  onClose,
}: {
  children: ReactNode;
  onClose: () => void;
}) {
  return (
    <div className="drawer-head">
      <div className="row" style={{ alignItems: "flex-start", gap: 12 }}>
        <div className="grow">{children}</div>
        <button className="icon-btn" onClick={onClose} aria-label="关闭">
          <Icon name="x" size={18} />
        </button>
      </div>
    </div>
  );
}
