import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// ============================================================================
// 助贷 AI 中台 Web 工作台 —— Vite 配置
//
// 两个关键点：
//   ① base: "/ui/" —— 生产构建产物由 FastAPI 挂在 /ui 下（见 src/agent_platform/
//      api/app.py 的 _mount_web_dist），静态资源路径必须带这个前缀，否则
//      部署后页面打开但 JS/CSS 全 404（典型的"本地好的、上线白屏"）。
//   ② server.proxy —— dev 模式（npm run dev，端口 5173）把 API 前缀代理到
//      后端 8000。这样前端零配置直连真实服务，不需要在前端代码里写死
//      localhost:8000，也就不存在跨域（同源代理）。
//
// 代理目标写 127.0.0.1 而不是 localhost：Windows 上 localhost 会优先解析到
// IPv6 的 ::1，而后端默认只听 127.0.0.1 —— 表现就是"后端明明起着、
// 代理却 connection refused"。写死 IPv4 地址把这个坑填掉。
//
// server.host 同样必须写 127.0.0.1：Node 17+ 对 localhost 的解析顺序变成
// 字面序，::1 排在 127.0.0.1 前面，vite dev server 会只监听 [::1]，导致
// 只按 IPv4 访问的客户端（含本机某些代理组件）全部 connection refused。
// ============================================================================
const BACKEND = "http://127.0.0.1:8000";

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      // 八个业务前缀 + 探针/指标，全部转给后端
      "/platform": BACKEND,
      "/product": BACKEND,
      "/coaching": BACKEND,
      "/marketing": BACKEND,
      "/gateway": BACKEND,
      "/voice": BACKEND,
      "/flow": BACKEND,
      "/data": BACKEND,
      "/health": BACKEND,
      "/ready": BACKEND,
      "/metrics": BACKEND,
    },
  },
  build: {
    outDir: "dist",
    // 后端 FastAPI 直接服务这些文件；sourcemap 开着小体积可接受
    sourcemap: false,
  },
});
