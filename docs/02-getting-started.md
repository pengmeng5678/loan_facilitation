# 上手指南

## 一、环境要求

| 项 | 要求 |
|---|---|
| Python | ≥ 3.11（开发验证于 3.13） |
| 依赖 | 见 `requirements.txt`（全部有 wheel，无需编译） |
| 外部服务 | **无**。默认即可完整运行（SQLite + 进程内向量库 + 离线 LLM） |

## 二、安装与运行

```bash
# 1) 依赖
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt                  # 含测试与 lint

# 2) 演示数据
python scripts/seed_data.py
#   → .data/policies/products.json            10 款产品
#   → .data/policies/policies.jsonl           40 条政策条款（带生效日期）
#   → .data/policies/conversations.jsonl       4 段历史对话
#   → .data/profiles/customers.json            5 位客户档案
#   → .data/profiles/personas.json             4 个客户画像
#   → .data/marketing/blueprints.json         12 条素材模板（3 渠道）
#   → .data/marketing/dormant_customers.json   8 位待激活客户
#   → .data/marketing/material_performance.json 12 组素材效果种子

# 3) 端到端演示（打印全部核心链路）
python scripts/demo.py

# 4) 测试（693 个用例，全部离线）
pytest

# 5) 服务（注意 --app-dir src，原因见下方"为什么命令要带 --app-dir"）
uvicorn --app-dir src agent_platform.api.app:app --reload --port 8000
open http://localhost:8000/docs

# 6) Web 工作台（另开终端；或先 cd web && npm run build，然后访问 /ui/）
cd web && npm install && npm run dev
open http://localhost:5173
```

### 为什么命令要带 `--app-dir src`

本项目采用 **src-layout**（源码在 `src/` 下），而 `uvicorn` 启动时只把**当前工作目录**放进
`sys.path`。在仓库根目录直接跑 `uvicorn agent_platform.api.app:app` 会得到：

```
ModuleNotFoundError: No module named 'agent_platform'
```

三种解法，任选一种：

| 方式 | 命令 | 适用 |
|---|---|---|
| **加参数**（推荐，零副作用） | `uvicorn --app-dir src agent_platform.api.app:app --reload` | 本地开发、PyCharm Run Configuration |
| 设环境变量 | `PYTHONPATH=src uvicorn agent_platform.api.app:app --reload` | CI、脚本化启动 |
| 装成可编辑包 | `pip install -e .` 之后 `uvicorn agent_platform.api.app:app --reload` | 长期开发，可让 IDE 索引到包元数据 |

> `make run` 已经帮你处理好了（Makefile 里 `export PYTHONPATH := src`），
> 容器里也一样（Dockerfile 里 `PYTHONPATH=/app/src`）。

用 `Makefile` 更省事：

```bash
make install-dev   # 装依赖
make seed          # 灌数据
make demo          # 端到端演示
make test          # 跑测试
make lint          # 代码规范
make run           # 起服务（自动先 seed）
make web-install   # 装前端依赖（web/）
make web-dev       # 起前端 dev server（5173）
make web-build     # 构建前端产物（web/dist → /ui）
make docker-up     # 一键全套（api + postgres + qdrant + redis）
```

### Web 工作台（`web/`）

```bash
cd web
npm run dev        # dev 模式：5173，API 代理到 8000（改前端热更新）
npm run build      # 生产模式：产物到 web/dist，后端重启后访问 /ui/
```

七个页面覆盖全流程：总览 / 线索看板 / SOP 预警 / 审批中心 / 产品问答 / 自助查进度 / 数据中台。
API Key 只在 localStorage；幂等键按业务语义生成（防重复点击，不是随机 uuid）。
Windows 上 dev server 与代理目标都固定 `127.0.0.1`（`localhost` 会解析到 IPv6 `::1`）。
详见 [`08-web.md`](08-web.md)。

---

## 三、配置说明（`.env`）

复制 `.env.example` 为 `.env` 后按需修改。分十一组：

### 1. LLM（决定是否联网）

```ini
LLM_PROVIDER=echo        # echo（离线，默认）| openai（任何 OpenAI 兼容端点）
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-xxxxxxxx
LLM_MODEL=qwen-plus
```

**常见端点**

| 供应商 | `LLM_BASE_URL` |
|---|---|
| 通义千问 DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| 本地 Ollama | `http://localhost:11434/v1` |
| 自建 vLLM | `http://<host>:8000/v1` |

> 离线模式下 `EchoLLM` 会返回确定性的占位内容。它**不参与任何业务判据** ——
> 准入筛选、评分、质检都走确定性代码，所以离线模式下的结果依然是"业务正确"的。

### 2. 向量化

```ini
EMBEDDING_PROVIDER=local     # local（离线哈希向量）| openai
EMBEDDING_DIM=1024           # local 模式下可调（越小越快，测试常用 256）
```

### 3. 状态持久化

```ini
CHECKPOINTER_BACKEND=sqlite  # memory | sqlite | postgres
CHECKPOINTER_SQLITE_PATH=.data/checkpoints.sqlite
POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/zhudai_ai
```

| 后端 | 适用 |
|---|---|
| `memory` | 单元测试（进程内，重启丢状态） |
| `sqlite` | 本地开发 / 单机生产 |
| `postgres` | 多实例生产（状态共享，HITL 恢复必需） |

⚠️ **多 worker 部署（副本数 > 1）时，Checkpointer 与分布式锁都必须换掉**，
这不是优化项：HITL 审批在副本 A 挂起、resume 落到副本 B，sqlite 存根取不到
状态 —— 表现为"审批通过了但流程没继续"，无报错、只有业务结果错。
PG 配置错误会**直接启动失败**并给出排查指引（fail-fast，不静默降级 memory）。
```ini
CHECKPOINTER_BACKEND=postgres
POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/zhudai_ai
```

### 3.5 分布式锁后端（多 worker 硬要求）

```ini
FLOW_LOCK_BACKEND=sqlite        # sqlite（单机默认）| redis（多 worker 必须）
REDIS_URL=redis://localhost:6379/0
```

SQLite 锁只在本进程内互斥，多 worker 下两条请求同时改同一条线索会**双双拿到锁**。
Redis 版用三件套保证正确性：`SET key owner NX PX ttl`（互斥 + 租约防死锁）、
Lua"比 owner 再 DEL"（原子释放，不会误放别人的锁）、Lua"比 owner 再 PEXPIRE"（续约）。

**fail-closed**：Redis 连接失败 → 业务接口返回 503「未做任何修改，重试安全」，
绝不静默降级 SQLite。验证是否切成功：`GET /flow/stats` 看 `lock_backend` 字段。

### 3.6 可观测：`GET /metrics`

Prometheus 格式（`text/plain; version=0.0.4`），无需 API Key（生产请在反代层按
来源 IP 收紧）。指标族：`app_info` / `runtime` / `gateway_*` / `flow_*` / `scheduler_*` / `data_*`。
采集失败也返回 200 —— 监控接口不能成为故障源。

### 3.7 数据中台（项目四）

```ini
DATA_PLATFORM_DATA_DIR=.data/data_platform
DATA_PLATFORM_SQLITE_PATH=.data/data_platform/data_platform.sqlite
DATA_SOURCE_PATH=.data/data_platform/wechat_source.sqlite   # 微信 MSG 库路径（只读打开）
DATA_EXPORT_DIR=.data/data_platform/exports
DATA_WINDOW_MINUTES=5            # 会话切块时间窗（相邻消息间隔超过即断开）
DATA_MIN_CHARS=6                 # 过短阈值（有效字符数）
DATA_HIGH_VALUE_QUALITY=0.70     # 高价值质量分阈值
DATA_AUTO_INGEST=true            # 首次启动且原始层为空时自动抽取（幂等，重跑无害）
DATA_FEED_ENABLED=true           # 已审核对话优先作为项目七的案例语料源
```

- `DATA_SOURCE_PATH` 不存在时**自动退回** `POLICY_DATA_DIR/conversations.jsonl`（演示语料），
  所以没有微信库也能跑通整条管线；`scripts/seed_data.py` 会生成同结构的 `wechat_source.sqlite`。
- 重置演示数据用 `make seed`：它用 **SQL 清表**而不是删库文件 —— 开发服务器可能正开着这个库。
- 关掉 `DATA_AUTO_INGEST` 后，抽取只能通过 `POST /data/ingest` 或 Web「数据中台」页手动触发。

### 4. 向量库

```ini
VECTOR_BACKEND=local    # local（进程内 numpy）| qdrant
QDRANT_URL=http://localhost:6333
```

### 5. 检索相关性闸门 ⚠️ **换模型必须重新校准**

```ini
RAG_MIN_RERANK_SCORE=0.15    # 词项层（主判据）
RAG_MIN_DENSE_SCORE=0.35     # 语义层（辅助）
```

低于阈值 → 判定"无政策依据" → 拒答并转人工。

**校准方法**：拿 20~30 条"该答"和"该拒"的问题各跑一遍，看两组得分的分布是否可分离：

```python
from app_context import build_app_context
ctx = build_app_context()
for q in ["公积金信用贷利率是多少", "今天天气怎么样", ...]:
    hits = ctx.product.retriever.retrieve(q, top_k=3)
    top = hits[0] if hits else None
    print(q, "→", "拒答" if top is None else f"rerank={top.rerank_score:.3f} dense={top.dense_score:.3f}")
```

取"该拒组最大值"与"该答组最小值"之间的分界点作为阈值。

### 6. 推理网关（项目六）⚠️ 默认关闭

```ini
GATEWAY_ENABLED=false                  # false = 业务直连 LLM，网关完全不参与（零风险上线）
GATEWAY_SQLITE_PATH=.data/gateway.sqlite
GATEWAY_MODEL_SMALL=qwen-turbo         # 分档模型：轻任务走小模型
GATEWAY_MODEL_MEDIUM=qwen-plus
GATEWAY_MODEL_LARGE=qwen-max
GATEWAY_TIER_THRESHOLD_MEDIUM=0.35     # 复杂度分档阈值
GATEWAY_TIER_THRESHOLD_LARGE=0.70
GATEWAY_BASELINE_TIER=large            # 成本基线（算"省了多少"的对照档位）
GATEWAY_SEMANTIC_CACHE_THRESHOLD=0.94  # ⚠️ 换 Embedding 必须重新校准
GATEWAY_EVAL_MAX_REGRESSION=0.02       # CI 准入允许的退化幅度
```

置 `true` 后，网关会**作为 `LLMProvider` 注入平台** —— 路由、缓存、限流、配额、
密钥轮转、成本核算、span 级观测、评测门禁全部生效，**业务代码一行都不用改**。
详见 [`04-gateway.md`](04-gateway.md)。

### 7. 素材推广与客户激活（项目七）

```ini
MARKETING_DATA_DIR=.data/marketing
MARKETING_DEFAULT_CHANNEL=moments      # moments | douyin | xiaohongshu
MARKETING_DORMANT_DAYS=7               # 超过多少天未互动视为沉睡客户
MARKETING_ACTIVATION_COOLDOWN_DAYS=3   # 两次激活的最小间隔（防骚扰）
MARKETING_MAX_ACTIVATION_PER_CUSTOMER=2 # 累计触达上限（防骚扰）
```

前两项是"对症匹配"的配置，后三项是**防骚扰的业务红线**（调大之前先想清楚会不会被拉黑）。
详见 [`05-marketing.md`](05-marketing.md)。

### 8. 流程数字员工（项目八）

```ini
FLOW_ENABLED=true
FLOW_DATA_DIR=.data/flow
FLOW_SQLITE_PATH=.data/flow/flow.sqlite
API_PREFIX_FLOW=/flow
# —— SLA：超过即进漏跟进预警，改之前先跟业务主管对齐口径
FLOW_FIRST_CALL_SLA_MINUTES=30       # 线索分配后多久内必须完成首呼
FLOW_WECHAT_SLA_HOURS=2              # 首呼后多久内必须加微
FLOW_FOLLOWUP_SLA_HOURS=24           # 跟进间隔超过多久算停滞
FLOW_INVITE_CONFIRM_SLA_HOURS=4      # 邀约后多久内必须确认到店时间
# —— 费用口径：等额本息 + 服务费分档
FLOW_DEFAULT_LOAN_MONTHS=36
FLOW_DEFAULT_ANNUAL_RATE=0.072
FLOW_SERVICE_FEE_RATE=0.03           # < 30 万：3%
FLOW_FEE_RATE_LARGE_LOAN=0.02        # ≥ 30 万：2%
FLOW_LARGE_LOAN_THRESHOLD=300000
# —— 可靠性与边界
FLOW_READINESS_WARN_MINUTES=10       # 到店前多久还没准备就绪 → 告警
FLOW_IDEMPOTENCY_TTL_HOURS=24        # 幂等键保留时长（客户重复提交的窗口）
FLOW_LOCK_TTL_SECONDS=30             # 分布式锁 TTL（持锁方崩了自动释放）
FLOW_HIGH_RISK_ACTIONS=send_contract,confirm_fee  # 必须走审批的动作
FLOW_EVAL_MAX_REGRESSION=0.03        # 行为评测允许的退化幅度
# —— 定时扫描与通知（生产打开；本地/测试保持关闭，不起后台任务）
FLOW_SCHEDULER_ENABLED=false         # true = 常驻 SOP 扫描（compose 默认已开）
FLOW_SCHEDULER_INTERVAL_SECONDS=300  # 扫描间隔（秒），下限 10s
FLOW_NOTIFY_WEBHOOK_URL=             # 企微/钉钉机器人 webhook；留空 = 只写日志
FLOW_NOTIFY_TIMEOUT_SECONDS=3        # 通知超时；失败只记日志，绝不拖垮扫描
```

三组配置分别对应三个**业务红线**，调之前先想清楚后果：

- **SLA 调大** = 客户等待变长，漏跟进率会回到 15% 的水平；
- **幂等 TTL 调小** = 客户第二次提交会真的再建一条线索；
- **`FLOW_HIGH_RISK_ACTIONS` 删项** = 该动作失去审批保护（发合同/确认费用**绝不能删**）。

详见 [`07-flow-ops.md`](07-flow-ops.md)。

### 9. 语音客服（项目一）⚠️ 默认 dry-run

```ini
VOICE_ENABLED=false                  # false = 演练：请求只记录不下发（可完整演示/测试）
RTC_APP_ID=                          # RTC 控制台 → 应用管理
RTC_APP_KEY=
VOLC_ACCESS_KEY=                     # 访问控制 → 密钥管理（OpenAPI 签名用）
VOLC_SECRET_KEY=
VOICE_TTS_APP_ID=
VOICE_TTS_CLUSTER=volcano_tts        # 复刻音色改 volcano_icl
VOICE_TTS_VOICE_TYPE=                # 复刻音色形如 S_xxxxx
VOICE_PUBLIC_BASE_URL=               # ⚠️ 必须公网可达（本地用 ngrok）
VOICE_CALLBACK_API_KEY=              # ⚠️ 生产必填，否则公网回调等于免费大模型代理
VOICE_MAX_TOKENS=220                 # 代码里夹在 150~300（语音说不了 2048）
```

三个最容易踩的点：

1. **`VOICE_PUBLIC_BASE_URL` 必须公网可达**。RTC 云端要 POST 到我们的服务，
   `localhost` 它访问不到 —— 表现是**日志一切正常但 AI 不说话**（最难查的一种症状）。
   本地开发：`ngrok http 8000`，把拿到的 https 地址填进去（不要带路径）。
2. **不配密钥也能跑**：`RtcClient` 退化为 `RecordingTransport`，
   请求原样记录下来，于是测试/demo 能断言"我们下发了什么配置"而不用真的开房花钱。
3. **音色与集群必须匹配**：复刻音色（`S_xxx`）走 `volcano_icl`，预置音色走 `volcano_tts`，
   混用直接合成失败。

详见 [`06-voice.md`](06-voice.md)。

### 10. 平台治理阈值

```ini
MAX_AGENT_STEPS=12           # 超过即熔断转人工（防死循环烧钱）
MAX_AGENT_TOKENS=60000       # token 预算上限
TOOL_MAX_RETRIES=3
CIRCUIT_FAILURE_THRESHOLD=5
APPROVAL_TIMEOUT_SECONDS=300
APPROVAL_TIMEOUT_STRATEGY=abort    # abort | escalate（都不会自动批准）
```

> ⚠️ `MAX_AGENT_STEPS` 调大时无需担心撞上 LangGraph 的递归上限 ——
> `graph/builder.py:recursion_limit_for()` 会按 `max_steps` 自动计算，这是刻意设计的（否则熔断路径永远走不到）。

### 11. API 鉴权（生产硬开关）⚠️ 默认关闭

```ini
API_AUTH_ENABLED=false
API_AUTH_KEYS=key1,key2              # 逗号分隔；轮换期可同时挂两把（新 key 先上、旧 key 后下）
```

打开后**七个前缀全部要凭证**，两种任选其一：

```bash
curl -H "X-API-Key: key1" localhost:8000/flow/leads
curl -H "Authorization: Bearer key1" localhost:8000/flow/leads
```

设计取舍（对应生产就绪度评估的 P0-1）：

- **中间件而不是逐路由 `Depends`**：七个前缀 + 以后新增路由，漏挂一个依赖
  就是一条无鉴权入口；中间件在进程最外圈 default-deny，漏开的代价只是"多要一个 key"。
- **白名单**：`/health` `/ready` `/`（探针要 key 等于没有探针）、`/docs` `/openapi.json`
  `/static/*`（保调试体验，生产可在 Nginx/APISIX 层再收紧）、`/voice/callback/*`
  （有独立更强的 `VOICE_CALLBACK_API_KEY`，豁免是因为它有自己的门）。
- **fail-closed**：开了开关却没配密钥 = 全部 401，而不是"当作没开"悄悄裸奔。
- **时序安全**：`hmac.compare_digest` 逐密钥比较，不因前几位对了缩短比较时间。

---

## 四、接入真实数据的三个位置

| 想替换 | 改这里 | 数据格式 |
|---|---|---|
| 产品目录 | `.data/policies/products.json` | `Product` 模型（见 `product_assistant/domain/models.py`） |
| 政策条款 | `.data/policies/policies.jsonl` | 每行一个 `PolicyDocument` |
| 客户档案 | `.data/profiles/customers.json` | `CustomerProfile` 模型 |
| 客户画像 | `.data/profiles/personas.json` | `Persona` 模型 |
| 历史对话 | `.data/policies/conversations.jsonl` | 见 `scripts/seed_data.py` 的 `CONVERSATIONS` |
| 素材模板 | `.data/marketing/blueprints.json` | `Blueprint` 模型（结构：钩子/痛点/方案/CTA） |
| 待激活客户 | `.data/marketing/dormant_customers.json` | 含犹豫原因与沉睡天数 |
| 素材效果 | `.data/marketing/material_performance.json` | 渠道 × 主题的漏斗数据（供归因） |
| 语音场景 | `.data/voice/scenes.json` | `VoiceScene` 模型（人设/音色/检索策略） |
| 语音提示词 | `.data/voice/prompts.json` | `PromptVersion` 列表（版本化，别手改 status） |

或用代码直接灌：

```python
from app_context import build_app_context
ctx = build_app_context()
ctx.product.profiles.upsert(my_customer_profile)    # 单条更新
ctx.product.repository.load()                        # 从磁盘重载
ctx.product.retriever.index(ctx.product.repository.all_chunks())   # 重建索引
```

---

## 五、高频问答

**Q：不装依赖能跑吗？**
不能，但依赖极少且有 wheel。核心是 `langgraph` + `fastapi` + `pydantic`。

**Q：一定要用 PostgreSQL / Qdrant / Redis 吗？**
不。默认 SQLite + 进程内向量库就能跑，只在多实例部署时才需要它们
（PG 共享 Agent 状态、Redis 做跨进程分布式锁，见 3.5 节）。

**Q：离线模式下 Agent 会不会给出错误答案？**
不会"业务性错误"。业务判据（准入、评分、质检、拒答）都是确定性代码；
离线模式只是让"语言表达"变成占位话术。接上真实模型后表达质量会提升，业务逻辑不变。

**Q：测试为什么要好几秒？**
因为每次都要真实构建向量索引、BM25 索引与图谱（不是 mock）。
这是刻意的：测的是**真实链路**。

**Q：如何新增一个业务 Agent？**
在 `app_context.py` 里三步：建服务 → `service.register_tools(runtime.tools)` →
`runtime.register(build_xxx_agent(service, runtime.tools))`。平台代码零改动。
（如需被评测，再加一步：把调用包装成 `targets[场景] = callable`。）

**Q：打开 `/docs` 是白屏（标签页标题正常、正文一片空白）？**
不是服务挂了，是**浏览器加载不到 Swagger UI 的静态资源**。
FastAPI 默认从 `cdn.jsdelivr.net` 拉 `swagger-ui-bundle.js`，国内网络经常连不上，
于是 HTML 返回了（标题能显示）、JS 却永远加载失败，`div#swagger-ui` 里自然什么都没有。
F12 → Console 会看到 `Uncaught ReferenceError: SwaggerUIBundle is not defined`。

本项目已经把 `swagger-ui-dist@5.17.14` 的 JS/CSS 随代码发货（`src/agent_platform/api/static/swagger-ui/`），
`/docs` 引用的是本进程的 `/static/...`，**零外网可用**。如果你还看到白屏：

1. 确认服务是**改完之后**重启的（`--reload` 有时不会重新挂载 `StaticFiles`，重启一次最稳）；
2. `curl -I http://localhost:8000/static/swagger-ui/swagger-ui-bundle.js` 应为 `200`，不是 `404`；
3. 启动日志里应出现「API 文档：使用本地 Swagger UI」；若出现「未找到本地静态资源…回退到 CDN」，
   说明 `static/` 目录没被打包进去。

`/redoc` 已主动关闭 —— 它同样依赖外网 `redoc.standalone.js`，与其留一个会白屏的入口，不如只留 `/docs`。
