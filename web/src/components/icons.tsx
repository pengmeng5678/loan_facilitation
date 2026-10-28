// 图标集：统一 24 视框、1.7 描边、currentColor。
// 不引第三方图标库 —— 工作台只用到二十来个图标，自己画可以控制粗细与对齐。

import type { ReactElement } from "react";

export type IconName =
  | "today"
  | "users"
  | "sparkles"
  | "shield"
  | "phone"
  | "chat"
  | "calendar"
  | "arrowRight"
  | "arrowUpRight"
  | "search"
  | "bell"
  | "plus"
  | "chevronRight"
  | "chevronDown"
  | "clock"
  | "alert"
  | "link"
  | "download"
  | "database"
  | "activity"
  | "more"
  | "x"
  | "check"
  | "refresh"
  | "send"
  | "file"
  | "wallet"
  | "mapPin"
  | "user"
  | "zap"
  | "layers"
  | "copy"
  | "lock"
  | "phoneCall"
  | "briefcase";

const PATHS: Record<IconName, ReactElement> = {
  today: (
    <>
      <rect x="3" y="4.5" width="18" height="16" rx="3" />
      <path d="M3 9.5h18M8 3v3M16 3v3" />
      <path d="M7.5 13.5h4M7.5 17h7" />
    </>
  ),
  users: (
    <>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M3.5 19.5c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5" />
      <path d="M16 5.4a3 3 0 0 1 0 5.9M17.4 14.9c1.9.5 3.2 2.1 3.6 4.6" />
    </>
  ),
  sparkles: (
    <>
      <path d="M12 3.5 13.6 9 19 10.6 13.6 12.2 12 17.7 10.4 12.2 5 10.6 10.4 9Z" />
      <path d="M18.5 16.5 19.2 18.8 21.5 19.5 19.2 20.2 18.5 22.5 17.8 20.2 15.5 19.5 17.8 18.8Z" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3.2 19 6v6c0 4.2-2.9 7.6-7 8.8-4.1-1.2-7-4.6-7-8.8V6Z" />
      <path d="m9 12 2.2 2.2L15.5 10" />
    </>
  ),
  phone: (
    <>
      <rect x="6.5" y="2.8" width="11" height="18.4" rx="2.6" />
      <path d="M10.5 5.6h3" />
      <path d="M11 18.4h2" />
    </>
  ),
  phoneCall: (
    <>
      <path d="M8.4 4.5 6.6 5.9c-.6.5-.8 1.3-.6 2 1.6 5.3 5.8 9.5 11.1 11.1.7.2 1.5 0 2-.6l1.4-1.8c.4-.6.4-1.4-.1-1.9l-2-1.7c-.5-.4-1.2-.5-1.8-.1l-1.4.8a12 12 0 0 1-4.6-4.6l.8-1.4c.4-.6.3-1.3-.1-1.8l-1.7-2c-.5-.5-1.3-.5-1.9-.1Z" />
      <path d="M15.5 4.5a5 5 0 0 1 4 4" />
    </>
  ),
  chat: (
    <>
      <path d="M20.5 12c0 3.9-3.8 7-8.5 7-1 0-2-.1-2.9-.4L4 20.2l1.4-3.6A6.4 6.4 0 0 1 3.5 12c0-3.9 3.8-7 8.5-7s8.5 3.1 8.5 7Z" />
      <path d="M8.5 11.8h7M8.5 15h4" />
    </>
  ),
  calendar: (
    <>
      <rect x="3.5" y="5" width="17" height="15.5" rx="2.6" />
      <path d="M3.5 9.5h17M8 3.2v3.4M16 3.2v3.4" />
      <path d="M8 13.5h2.5M13.5 13.5H16M8 17h2.5" />
    </>
  ),
  arrowRight: <path d="M4.5 12h14m-5-5 5 5-5 5" />,
  arrowUpRight: <path d="M7 17 17 7M8.5 7H17v8.5" />,
  search: (
    <>
      <circle cx="10.8" cy="10.8" r="6.3" />
      <path d="m15.5 15.5 4 4" />
    </>
  ),
  bell: (
    <>
      <path d="M18 9.2a6 6 0 0 0-12 0c0 4.2-1.4 5.5-1.9 6.3-.3.4 0 .9.5.9h14.8c.5 0 .8-.5.5-.9-.5-.8-1.9-2.1-1.9-6.3Z" />
      <path d="M9.8 19.4a2.4 2.4 0 0 0 4.4 0" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  chevronRight: <path d="m9.5 6 6 6-6 6" />,
  chevronDown: <path d="m6 9.5 6 6 6-6" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="8.4" />
      <path d="M12 7.5V12l3 1.8" />
    </>
  ),
  alert: (
    <>
      <path d="M12 3.8 21 20H3Z" />
      <path d="M12 9.5v4.2M12 16.8v.2" />
    </>
  ),
  link: (
    <>
      <path d="M10 13.8a4 4 0 0 0 5.6 0l2.6-2.6a4 4 0 0 0-5.6-5.6L11.5 6.7" />
      <path d="M14 10.2a4 4 0 0 0-5.6 0l-2.6 2.6a4 4 0 0 0 5.6 5.6l1.1-1.1" />
    </>
  ),
  download: (
    <>
      <path d="M12 4v10.5m-4-4 4 4 4-4" />
      <path d="M4.5 17.5v1.6c0 .5.4.9.9.9h13.2c.5 0 .9-.4.9-.9v-1.6" />
    </>
  ),
  database: (
    <>
      <ellipse cx="12" cy="6.2" rx="7.5" ry="2.8" />
      <path d="M4.5 6.2v11.6c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8V6.2" />
      <path d="M4.5 12c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8" />
    </>
  ),
  activity: <path d="M3.5 12.5h3.2l2.6-6.4 4 12.4 2.4-6h4.8" />,
  more: (
    <>
      <circle cx="5.5" cy="12" r="1.3" />
      <circle cx="12" cy="12" r="1.3" />
      <circle cx="18.5" cy="12" r="1.3" />
    </>
  ),
  x: <path d="m6 6 12 12M18 6 6 18" />,
  check: <path d="m5 12.5 4.5 4.5L19 7" />,
  refresh: (
    <>
      <path d="M20 12a8 8 0 1 1-2.6-5.9" />
      <path d="M20 4.5V10h-5.2" />
    </>
  ),
  send: (
    <>
      <path d="M20.5 3.5 3.8 10.2l6.4 2.6 2.6 6.4Z" />
      <path d="m10.2 12.8 4.3-4.3" />
    </>
  ),
  file: (
    <>
      <path d="M14 3.5H7.4c-1 0-1.9.8-1.9 1.9v13.2c0 1 .8 1.9 1.9 1.9h9.2c1 0 1.9-.8 1.9-1.9V8Z" />
      <path d="M14 3.5V8h4.5M8.5 13h7M8.5 16.5h5" />
    </>
  ),
  wallet: (
    <>
      <rect x="3.2" y="6" width="17.6" height="13" rx="3" />
      <path d="M3.2 10h17.6M16.5 14.5h1.8" />
      <path d="M6.5 6V4.8c0-.7.6-1.3 1.3-1.2l9 1.4" />
    </>
  ),
  mapPin: (
    <>
      <path d="M12 21c4-4.4 6-7.4 6-10a6 6 0 1 0-12 0c0 2.6 2 5.6 6 10Z" />
      <circle cx="12" cy="10.8" r="2.2" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8.2" r="3.6" />
      <path d="M4.8 20c.7-3.7 3.6-5.8 7.2-5.8s6.5 2.1 7.2 5.8" />
    </>
  ),
  zap: <path d="M13 3 5.5 13.2H11l-1 7.8 7.5-10.2H12Z" />,
  layers: (
    <>
      <path d="m12 3.5 8.5 4.3-8.5 4.2-8.5-4.2Z" />
      <path d="m3.5 12.4 8.5 4.2 8.5-4.2M3.5 16.7l8.5 4.2 8.5-4.2" />
    </>
  ),
  copy: (
    <>
      <rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.2" />
      <path d="M15.5 8.5V6.2c0-1-.8-1.9-1.9-1.9H6.2c-1 0-1.9.8-1.9 1.9v7.4c0 1 .8 1.9 1.9 1.9h2.3" />
    </>
  ),
  lock: (
    <>
      <rect x="4.8" y="10.2" width="14.4" height="10" rx="2.4" />
      <path d="M8.2 10.2V7.6a3.8 3.8 0 0 1 7.6 0v2.6" />
    </>
  ),
  briefcase: (
    <>
      <rect x="3.2" y="7.5" width="17.6" height="12.5" rx="2.6" />
      <path d="M8.8 7.5V5.8c0-1 .8-1.8 1.8-1.8h2.8c1 0 1.8.8 1.8 1.8v1.7" />
      <path d="M3.2 13h17.6" />
    </>
  ),
};

export function Icon({
  name,
  size = 18,
  strokeWidth = 1.7,
  className,
}: {
  name: IconName;
  size?: number;
  strokeWidth?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
