# =============================================================================
# 助贷 AI 中台 —— 生产镜像
# 多阶段构建：webbuilder 构建前端 → builder 装依赖 → runtime 只带运行时产物
# =============================================================================
FROM node:20-alpine AS webbuilder

WORKDIR /web
# 锁文件优先：npm ci 严格按 lock 安装（CI/生产可复现），且利用层缓存
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build
# 产物在 /web/dist —— runtime 阶段拷到 /app/web/dist，FastAPI 挂 /ui

# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
# 装到独立前缀，便于拷进 runtime 阶段
RUN pip install --prefix=/install -r requirements.txt

# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# 非 root 运行，符合生产安全基线
RUN useradd --create-home --shell /bin/bash appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    API_HOST=0.0.0.0 \
    API_PORT=8000

COPY --from=builder /install /usr/local
COPY --from=webbuilder /web/dist ./web/dist

WORKDIR /app
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY .env.example ./

# 运行期数据目录（挂载卷持久化）
RUN mkdir -p /app/.data && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查：走 /health，K8s / compose 可直接复用
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# 生产用多 worker（--workers 2）。⚠️ 多 worker 的两条硬约束，缺任何一条都会
# 产生"无报错的业务错误"，且都已做成 fail-fast，不会静默降级：
#   ① FLOW_LOCK_BACKEND=redis + REDIS_URL  —— SQLite 锁只在本进程内互斥，
#      并发改同一条线索会双双拿到锁（Redis 挂了返回 503，不会悄悄降级）；
#   ② CHECKPOINTER_BACKEND=postgres + POSTGRES_DSN —— HITL 挂起后 resume
#      落到别的副本会取不到状态（配错直接启动失败）。
# compose 已默认配好这两项（含 redis 服务）；裸 docker run 请自行带上。
# 首次启动自动灌演示数据（已有则跳过）——否则容器里"能起来但查不到任何政策"，
# 是最容易在演示/预发环境踩的坑。
CMD ["sh", "-c", "test -f .data/policies/products.json || python scripts/seed_data.py; exec python -m uvicorn agent_platform.api.app:app --host ${API_HOST} --port ${API_PORT} --workers 2"]
