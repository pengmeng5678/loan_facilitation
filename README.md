# 助贷 AI 中台（zhudai-ai-suite）

金融助贷场景的 **AI 中台工程实现**，包含八个可独立运行、也可协同工作的服务：

| 服务 | 对应项目 | 核心能力 | 入口 |
|---|---|---|---|
| `agent_platform` | **项目五** 企业多智能体编排平台 | 有状态编排 · MCP 工具生态（作用域裁剪 + **适配层**：ToolSpec↔MCP 双向、stdio/StreamableHTTP 双传输、默认不暴露 high 风险工具）· HITL 人工审批 · 三级意图路由（规则→向量→LLM + 低置信澄清）· 失败自愈与预算熔断 · trace_id 全链路可观测 | `/platform`、`/platform/mcp` |
| `data_platform` | **项目四** 助贷对话数据中台 | 微信库抽取（wxid→姓名/群发送者还原）· 9 级分类 · 三层幂等脱敏（价格硬红线）· 5 分钟切块 · 人工审核闸门 · 四格式导出 · 向量切片与知识文章 | `/data` |
| `product_assistant` | **项目二** 助贷产品知识助手与智能方案匹配 | 政策 RAG（混合检索/RRF/Rerank/生效日期过滤/引用溯源）· 引用一致性校验（不一致即拒答）· 结构化方案匹配 · GraphRAG · 方案书生成 | `/product` |
| `coaching` | **项目三** 业务员话术陪练与 AI 考核 | 模拟客户对抗陪练 · 四维评分（合规一票否决）· 全量会话质检 · 高转化话术挖掘 | `/coaching` |
| `marketing` | **项目七** 销售素材推广与客户激活 AI | 三渠道语感适配素材生成 · 合规先审后发 · 成单案例脱敏包装 · 未成单客户对症激活（防骚扰）· 素材效果归因 | `/marketing` |
| `voice_service` | **项目一** AI 实时语音对话智能客服 | 火山 RTC + 双向流 ASR + TTS 音色复刻 + **第三方 LLM 回调**（知识/提示词/合规都握在自己手里）· 逐句内容安全（含断字/零宽/全角/拼音变体归一化）· 首字延迟可观测 · Golden Set 准入 | `/voice` |
| `flow_ops` | **项目八** 助贷业务流程数字员工 | 线索状态机 + SOP 漏跟进预警 · 到店前自动准备（档案+方案初稿+费用预估一页纸）· HITL 审批门 · 幂等+分布式锁 · 自助查进度 · 全程审计 | `/flow` |
| `gateway` | **项目六** 大模型推理网关与 Agent 可观测评测平台 | 复杂度路由 · 流式调用（首字前降级）· provider 级熔断 · 精确/语义缓存 · 上下文裁剪 · 限流配额密钥轮转 · 成本台账与会话回放 · Golden Set 评测 + CI 准入 | `/gateway` |

> 项目一、二、三、七、八是**装配在项目五之上**的业务 Agent —— 它们不重写编排、状态、中断、
> 熔断、观测，只提供自己的"能力函数"。这正是"新 Agent 接入周期从 60 人日降到 30 人日"的工程含义。
>
> 项目六则是**基础设施**：它把自己包装成一个 `LLMProvider` 交给平台，业务代码只看到
> "一个更便宜的 LLM"；`GATEWAY_ENABLED=false` 时可整个摘掉，业务不受影响。
>
> 项目一是**接入层**：音频走 RTC 的 UDP 通道（浏览器 ↔ 火山边缘节点 ↔ 云端 Agent），
> Python 服务既不收也不发音频，只做三件事 —— 签进房凭证、下发会话配置、处理 LLM 回调。
>
> 项目八是**流程层**：把线索到签约之间的"人肉流程"改造成"状态机 + 工具化 + 人工把关"。
> 自动化范围 = 咨询 → 邀约 → 方案生成 → 费用评估；**接待 / 谈判 / 费用确认保留人工** ——
> 这条边界写在代码里（`HumanStage` 是 Agent 禁区，高风险动作必须 `ApprovalGate` 放行），
> 不是只写在文档里。
>
> 项目四是**数据源头**：其他项目的数据都从这里取。它**没有独立 Agent**（不面向客户对话），
> 只向平台注册 `data.*` 工具（审核是 high risk，必须过 HITL）。已审核对话会优先作为项目七的
> 案例语料源 —— "没被审核的数据进不了素材"，这条约束落在装配层
> （`app_context._make_conversation_provider`），不是靠人记得。

---

## 🧭 新人入职从这里开始

第一次接触这个仓库？不要从 `src/` 目录树开始读，也不要按文档编号顺序读。

**→ [`docs/00-onboarding.md`](docs/00-onboarding.md) —— 2 天学习地图**

它把 2 天切成 10 个时间块，每块告诉你读什么、做什么、留下什么产出、怎么自检。
Day 1 只求"能讲"（业务链路 + 数据流向），Day 2 才求"能改"（定位代码 + 守住红线）。

两个会反复用到的工具：

```bash
python scripts/onboarding_check.py --probe ALLOWED_TRANSITIONS   # 反查符号在哪个文件
python scripts/onboarding_check.py --map docs/10-code-map.md     # 校验文档里的代码位置没过期
```

**→ [`docs/10-code-map.md`](docs/10-code-map.md) —— 源码地图**

"我想改 X 该去哪个文件"的 40 行反查表，加九个包各一页说明（含每包"不许改什么"），
以及五个"看似合理的错误改法"——新人第一周犯的错几乎都在那五个里。

> **给开源读者**：这两份文档存在的意义是让一个陌生人在 2 天内独立看懂
> 源码、业务逻辑与数据流向，不需要有人带着讲。它们本身就是这个仓库工程化程度的一部分。

---

## 一、快速开始（3 分钟）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 生成演示数据（10 款产品 / 40 条政策 / 5 位客户 / 4 个客户画像 / 4 段历史对话 /
#                12 条素材模板 / 8 位待激活客户 / 12 组素材效果 / 3 个语音场景 /
#                4 条流程线索（含 1 条首呼逾期，用于 SOP 预警演示）/
#                微信库结构的数据中台源（7 个会话 / 33 条消息，覆盖 9 类分类与脱敏边界））
python scripts/seed_data.py

# 3. 跑端到端演示（不需要任何 API Key）
python scripts/demo.py

# 4. 运行测试（693 个用例，全部离线）
pytest

# 5. 启动 HTTP 服务
#    注意 --app-dir src：本项目是 src-layout，源码在 src/ 下，
#    不加这个参数 uvicorn 找不到 agent_platform（会报 ModuleNotFoundError）
uvicorn --app-dir src agent_platform.api.app:app --reload
#    → 打开 http://localhost:8000/docs 查看交互式 API 文档
#      （Swagger UI 静态资源已随代码发货，不依赖 jsdelivr CDN，国内网络无需代理）
#    → 若想用不带 --app-dir 的命令，先执行 pip install -e . （见 docs/02 第二节）

# 6. 启动 Web 工作台（另开一个终端，二选一）
#    a) dev 模式：Vite dev server + API 代理（改前端热更新，后端不用重启）
cd web && npm install && npm run dev        # → http://localhost:5173
#    b) 生产模式：前端构建产物由后端直接服务，一个端口全都有
cd web && npm run build                     # 产物到 web/dist
#    然后重启后端 → http://localhost:8000/ui/
```

**默认 `LLM_PROVIDER=echo`（离线确定性模式）**，无需密钥、不联网即可跑通全部链路 ——
新人和 CI 都不会被环境卡住。要接真实模型，复制 `.env.example` 为 `.env` 并改成：

```ini
LLM_PROVIDER=openai
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # 或 DeepSeek / Ollama / vLLM
LLM_API_KEY=sk-xxxx
LLM_MODEL=qwen-plus
```

**上生产前打开这三个开关**（都在 `.env`，默认全部关闭 —— 本地开发零配置可跑）：

```ini
API_AUTH_ENABLED=true            # 七个 API 前缀全部要 key（X-API-Key 或 Bearer）；
API_AUTH_KEYS=<随机长 key>        # 不配密钥 = fail-closed 全 401，不会悄悄裸奔
FLOW_SCHEDULER_ENABLED=true      # SOP 定时扫描常驻（漏跟进在超时那一刻被发现）
FLOW_NOTIFY_WEBHOOK_URL=https://qyapi.weixin.qq.com/...  # 预警/审批单推到企微群；
                                 # 留空则只写日志。通知失败只记日志，绝不拖垮扫描
```

白名单（不带 key 也放行）：`/health` `/ready` `/`、`/docs` `/openapi.json` `/static/*`、
`/voice/callback/*`（有独立更强的回调鉴权 `VOICE_CALLBACK_API_KEY`）。

### Docker

```bash
docker compose up -d --build     # api + postgres + qdrant + redis
curl localhost:8000/ready
```

### Web 工作台

`web/` 是一个 **TypeScript + Vite + React** 单页应用，**给销售业务员用**，不是给技术看的：
导航按业务员一天的使用顺序分组，界面上不出现 `rep_01` / `first_call` / `ok` 这类机器键。

```
工作台   今日工作台          今天该干什么：超时线索 → 待办三档 → 阶段分布 → 最近动态
客户     我的客户            6 列看板（待分配→待首触→跟进中→已邀约→到店推进→已成交）+ 详情抽屉
成交支持 方案中心 / 审批中心  资质→方案匹配；HITL 两段式（批准 ≠ 已执行）
客户服务 客户查进度 / 跟进预警 客户自助查（脱敏手机壳）；两级 SOP 预警
管理     团队看板 / 数据资产  累计口径漏斗、流程健康度；对话→语料清洗与审核
```

演示数据（`scripts/seed_data.py`）覆盖 11 个阶段各一条线索、审批三种状态、
以及两条不同量级的 SOP 逾期 —— 打开每一屏都有真实场景，不需要先手工造数据。

**手机上能用**，不是"桌面缩小版"：≤760px 自动换成底部 tab 导航、看板切列表、
多列收单列、抽屉整屏铺满、输入框 16px（避免 iOS 聚焦放大）。
业务员在门店/车里掏手机就能推进线索，不用回工位。
（设计理由与踩过的坑见 [`docs/08-web.md`](docs/08-web.md) 第 1.2 节）

两种跑法：

| 模式 | 命令 | 访问 | 适用 |
|---|---|---|---|
| dev | `cd web && npm run dev` | `http://localhost:5173` | 前端开发（API 代理到 8000，无跨域） |
| 生产 | `cd web && npm run build` | `http://localhost:8000/ui/` | 部署（FastAPI 直接服务 `web/dist`，零 Node 依赖） |

三个刻意的设计：

- **API Key 只存在 localStorage**，首屏探测 `/health`（白名单）判断后端在不在，
  不在就显示连接指引而不是一片报错页面；
- **幂等键按业务语义生成**（`lead_id+store+时间段`），不是随机 uuid ——
  随机键防不住"用户重复点击/网络重试"，与后端 `flow_ops/idempotency.py` 同一个道理；
- **机器语义只在前端翻译一次**（`src/hooks.ts` 的翻译表），页面不自己拼字符串，
  否则同一个 `first_call` 会在三个页面出现三种译法。

详见 [`docs/08-web.md`](docs/08-web.md)。

### 多 Worker 部署（副本数 > 1）

单副本默认配置（SQLite 锁 + SQLite 存根）原样就能跑；**副本数 > 1 时下面两项是硬要求**：

| 组件 | 单副本 | 多副本 | 不换的后果 |
|---|---|---|---|
| 分布式锁（项目八写操作） | SQLite（进程内互斥） | **Redis**（`SET NX PX` + Lua 原子释放/续约） | 两条请求同时改同一条线索会**双双拿到锁** |
| Agent 状态（HITL 恢复） | SQLite 存根 | **PostgreSQL** | 审批挂起后 resume 落到别的副本 → "审批通过了但流程没继续" |

两项都是 **fail-fast / fail-closed，绝不静默降级**：Redis 连不上 → 503「未做任何修改，重试安全」；
PG 配错 → 启动失败并给出排查指引。降级 SILently 比明确失败危险得多。

```bash
# .env（多 worker）
FLOW_LOCK_BACKEND=redis
REDIS_URL=redis://your-redis:6379/0
CHECKPOINTER_BACKEND=postgres
POSTGRES_DSN=postgresql://user:pass@your-pg:5432/zhudai_ai

# 起 N 个副本（compose 已内置 redis 服务，锁后端默认已切 redis）
docker compose up -d --build --scale api=3
curl -H "X-API-Key: $KEY" localhost:8000/flow/stats   # lock_backend 应为 RedisLockBackend
```

配好之后 **Prometheus 直接抓 `GET /metrics`**（`text/plain; version=0.0.4`，无需 Key，
生产在反代层按来源 IP 收紧）：`app_info` / `runtime` / `gateway_*` / `flow_*` / `scheduler_*`。

---

## 二、目录结构

```
.
├── src/
│   ├── agent_platform/            # 项目五：编排平台（底座）
│   │   ├── config.py              #   全局配置（pydantic-settings）
│   │   ├── errors.py              #   分层异常（可重试/可自修正/需熔断）
│   │   ├── llm/                   #   LLM & Embedding 抽象（离线 + OpenAI 兼容）
│   │   ├── state/                 #   编排状态（LangGraph State 定义）
│   │   ├── tools/                 #   工具生态：注册 / 作用域裁剪 / 权限 / 校验
│   │   ├── resilience.py          #   重试 / 熔断 / 预算守卫
│   │   ├── hitl.py                #   人工审批（含超时降级策略）
│   │   ├── checkpointing.py       #   状态持久化（memory / sqlite / postgres，配错 fail-fast）
│   │   ├── observability.py       #   span 级追踪 + 指标（结构化，供 Prometheus 渲染）
│   │   ├── graph/                 #   编排引擎：能力契约 + 标准节点 + 装配器
│   │   ├── agents/                #   Agent 注册中心
│   │   ├── runtime.py             #   统一执行入口（invoke / resume / get_state）
│   │   └── api/                   #   FastAPI 应用、平台路由与 API Key 鉴权中间件
│   │       └── metrics.py         #     GET /metrics（Prometheus exposition 0.0.4）
│   ├── product_assistant/         # 项目二：产品知识助手
│   │   ├── domain/                #   领域模型 + 结构化匹配引擎
│   │   ├── rag/                   #   切分 / 向量库 / BM25 / 混合检索 + Rerank
│   │   ├── knowledge/             #   政策仓储 / 客户档案 / 产品关系图谱
│   │   ├── proposal.py            #   方案书生成
│   │   ├── service.py             #   门面（问答 / 匹配 / 方案书）
│   │   ├── agent.py               #   装配到平台的 6 个能力函数
│   │   └── api.py
│   ├── coaching/                  # 项目三：话术陪练与考核
│   │   ├── domain.py              #   画像 / 会话 / 评分卡 / 质检 / 话术
│   │   ├── roleplay.py            #   模拟客户（会抵抗，不放水）
│   │   ├── judge.py               #   四维评分 + 双盲校准
│   │   ├── qc.py                  #   全量质检规则引擎（两段式）
│   │   ├── mining.py              #   高转化话术挖掘
│   │   ├── store.py               #   SQLite 会话持久化（支持续练与回放）
│   │   ├── service.py / agent.py / api.py
│   │   └── ...
│   ├── marketing/                 # 项目七：素材推广与客户激活
│   │   ├── generator.py           #   素材生成：结构模板 → 渠道语感 → 合规改写 → 质量分
│   │   ├── quality.py             #   渠道语感规范（Channel Spec）+ 素材质量分
│   │   ├── compliance.py          #   两段式合规：规则保召回 + LLM 保精度
│   │   ├── desensitize.py         #   脱敏：手机号 / 证件号 / 银行卡 / 微信 / 金额区间 / 姓名
│   │   ├── cases.py               #   成单案例 → 客户证言式素材（只有成交对话可入库）
│   │   ├── activation.py          #   未成单客户激活：对症匹配 + 四道防骚扰闸门
│   │   ├── attribution.py         #   素材 → 触达 → 留资 → 成单 漏斗归因
│   │   ├── store.py / service.py / agent.py / api.py
│   │   └── ...
│   ├── gateway/                   # 项目六：推理网关与可观测评测平台
│   │   ├── router.py              #   复杂度路由 + 只往下退的降级链
│   │   ├── cache.py               #   前缀缓存（精确）+ 语义缓存（阈值 + TTL）
│   │   ├── trim.py                #   上下文裁剪：去重 / 工具摘要 / 历史分层压缩
│   │   ├── ratelimit.py / keys.py #   令牌桶 + 按作用域配额 / 多密钥轮转与冷却
│   │   ├── gateway.py             #   把上面这些串成一次完整调用（并暴露为 LLMProvider）
│   │   ├── observe.py / store.py  #   span 级追踪 + 成本台账 + 会话回放 / SQLite 持久化
│   │   ├── evaluation/            #   Golden Set + 规则判分 + LLM-as-Judge + CI 准入
│   │   ├── service.py / agent.py / api.py
│   │   └── ...
│   ├── voice_service/             # 项目一：AI 实时语音对话智能客服
│   │   ├── rtc_token.py           #   进房 Token（001 方案）+ 自校验 + 打码
│   │   ├── volc_sign.py           #   火山 OpenAPI V4 签名（SigV4 同构）
│   │   ├── agent_config.py        #   云端配置：ASR（双向流/热词/情绪）/ TTS / LLM（Custom 回调）
│   │   ├── rtc_client.py          #   StartVoiceChat / StopVoiceChat + 离线演练传输层
│   │   ├── censor.py              #   内容安全：五条红线 + 绝对化改写 + PII 打码
│   │   ├── prompt.py              #   提示词版本化（灰度/回滚）+ 知识块稳定拼接
│   │   ├── rag.py                 #   语音检索策略：小 limit / 相关性闸门 / 长度预算
│   │   ├── dialog.py              #   流式生成 → 切句 → 逐句过审 → 整轮复检 → 打断收口
│   │   ├── session.py / metrics.py#   SQLite 会话与延迟拆解 / 首字 p95 与业务指标
│   │   ├── evaluation.py          #   Golden Set + 红线一票否决 + 版本准入
│   │   ├── service.py / agent.py / api.py
│   │   └── ...
│   ├── flow_ops/                    # 项目八：助贷业务流程数字员工
│   │   ├── domain.py                #   领域模型：11 阶段状态机 + 3 个人工节点（Agent 禁区）
│   │   ├── leads.py                 #   状态机流转表 + SLA 规则（漏跟进判据是确定性代码）
│   │   ├── readiness.py             #   到店前一页纸：档案 + 方案初稿 + 费用预估 + 谈话要点
│   │   ├── approval.py              #   HITL 审批门：assert_approved() 是唯一通行证
│   │   ├── idempotency.py           #   幂等键（语义哈希）+ LockGuard（SQLite/Redis 双后端）
│   │   ├── locking.py               #   分布式锁后端：Redis 三件套 / SQLite，fail-closed
│   │   ├── fees.py                  #   等额本息 + 服务费分档（确定性计算，非模型估计）
│   │   ├── progress.py              #   客户自助查进度：双重身份校验 + 服务端脱敏
│   │   ├── workflow.py              #   流程引擎：写操作五步套路（幂等→锁→校验→执行→审计）
│   │   ├── notifier.py              #   通知渠道：Log / Webhook（企微/钉钉），失败不拖垮业务
│   │   ├── scheduler.py             #   SOP 常驻调度：asyncio 循环 + to_thread + 单轮失败自愈
│   │   ├── evaluation.py            #   11 条行为用例 + 基线对比准入门禁
│   │   ├── store.py / service.py / agent.py / api.py
│   │   └── ...
│   ├── data_platform/               # 项目四：助贷对话数据中台（数据源头，无 Agent）
│   │   ├── domain.py                #   四层模型：原始消息 / 对话块 / 暂存条目 / 知识产物
│   │   ├── source.py                #   微信 SQLite 只读适配器 + JSONL + 离线夹具生成器
│   │   ├── masking.py               #   优先级脱敏：身份证先于银行卡，幂等可 verify
│   │   ├── filters.py               #   9 级分类 + 质量分 + QA 抽取 + 场景判定（全规则）
│   │   ├── chunker.py               #   5 分钟窗切块 + 同角色合并 + 块内/跨块去重
│   │   ├── staging.py               #   审核闸门：approve/reject/edit，编辑即重置并重跑脱敏
│   │   ├── exporter.py              #   四格式导出 + 内容哈希幂等（同输入同输出）
│   │   ├── knowledge.py             #   切片 + 向量 + 文章（模型失败规则兜底，标注来源）
│   │   ├── store.py / service.py / api.py
│   │   └── ...
│   └── app_context.py             # 应用装配根（Composition Root）：六个 Agent 在此装配
├── web/                           # Web 工作台（TypeScript + Vite + React，npm run dev/build）
├── tests/                         # 693 个用例（691 通过 + 2 跳过），全部可离线运行
├── scripts/                       # seed_data.py / demo.py
├── docs/                          # 架构 / 上手 / API / 网关 / 素材推广 / 语音客服 / 流程数字员工 / Web / 数据中台 文档
├── Dockerfile · docker-compose.yml · Makefile
└── pyproject.toml · requirements*.txt · .env.example
```

---

## 三、七条贯穿全局的设计原则

> 📌 **新人注意**：这七条不是背景资料，是 **Day 2 上午的精读材料**（见 `docs/00-onboarding.md` 时间块 8）。
> 每条原则的代码坐标与"守它的测试"见 `docs/10-code-map.md` 表 C。违反其中任何一条，
> 测试会红——这些边界是机器守着的，不是靠人记得。

### 1. 业务判据用确定性代码，语言表达用模型

助贷是强监管行业，**准入判断、评分口径、质检规则** 出错的代价是监管处罚与客诉。
因此这些逻辑全部是可审计的确定性代码：

| 环节 | 确定性实现 | LLM 的职责 |
|---|---|---|
| 方案匹配 | `product_assistant/domain/matching.py` 硬性准入 + 多目标排序 | 只做方案书话术润色 |
| 话术评分 | `coaching/judge.py` 四维 rubric + 违规词库 | 叠加语义层评分（合规维度不参与融合） |
| 会话质检 | `coaching/qc.py` 规则引擎粗筛（保召回） | 只对粗筛命中做精判（降误报） |
| 角色扮演 | `coaching/roleplay.py` 画像规则（离线可跑） | 让台词更像真人 |
| 素材合规 | `marketing/compliance.py` 6 条规则 + 脱敏器（一票否决） | 只补充"规则没覆盖的新型话术" |
| 激活防骚扰 | `marketing/activation.py` 阈值 / 静默期 / 触达上限 | 只负责"对症素材"的语义匹配 |
| 语音内容安全 | `voice_service/censor.py` 五条红线 + PII 打码（命中即整轮替换） | —（语音说出去收不回来，不能交给概率） |
| 成本路由 | `gateway/router.py` 复杂度打分 + 任务档位保底 | —（这一层不该交给模型决定） |

好处：**LLM 挂掉或换供应商，业务底线不变**；且全部可单元测试、可复现。

### 2. 任何外部依赖都必须有"离线确定性实现"

`EchoLLM` 与 `LocalHashEmbedding` 让系统在零密钥、零网络的情况下跑通完整链路。
CI 与新人不会因为环境问题卡住，测试也不需要 mock 网络。

### 3. 阈值与权重必须显式可调 + 有校准说明

RAG 的拒答闸门（`RAG_MIN_RERANK_SCORE` / `RAG_MIN_DENSE_SCORE`）与匹配权重
（`MatchingWeights`）都是从配置或代码显式传入的，并附带了**实测校准数据**。
换成真实 Embedding 后必须重新探测校准 —— 这一点写在了代码注释和 `.env.example` 里。

### 4. 高风险动作必须经过人

`platform.send_external_message`（对外发消息）被标记为 `risk_level=high, idempotent=False`，
平台会在编排层自动插入 `interrupt` 检查点：

```
质检发现高风险 → 调用告警工具 → 图挂起 → 审批中心出现待办 → 人工批准 → resume → 告警发出 → 全程留痕
素材激活要发送 → 取客户触达包 → 发送工具挂起 → 人工核对内容 → 批准 → 发出 → 全程留痕
```

审批超时的降级策略只有 `abort`（放弃）和 `escalate`（升级挂起），**永远不会自动批准**。

> 同时用**最小权限**兜底：素材 Agent 只有在"激活路由 + 用户明确说要发"时，
> 作用域里才会出现发送工具，其余情况下它手里根本没有这个工具。

### 5. 基础设施不许反向依赖业务

`gateway`（项目六）里**没有任何一行** `import product_assistant` / `import marketing`。
评测目标由装配层注入（`Callable[[EvalCase], dict]`），并且有**机器化校验测试**
（`test_gateway_does_not_import_business_packages`）防止后人破坏这条边界 ——
否则"基础设施"会慢慢长成"没人敢动的业务逻辑坟场"。

### 6. 合规是流程约束，不是一张检查清单

两条链路的合规都是**结构上无法绕过**，而不是"记得调用一下审核接口"：

| 链路 | 唯一出口 | 绕过不了的机制 |
|---|---|---|
| 素材（项目七） | `MaterialGenerator.assemble()` | 素材草稿只能从 `assemble()` 产出，审核在里面 |
| 语音（项目一） | `VoiceDialogEngine.stream_turn()` | 每一句在吐给 TTS 之前先过 `Censor.check()`，命中即换兜底话术 |

对应地，**兜底话术自己也不能踩红线** —— 这件事有专门的回归测试守着
（`test_fallback_replies_must_not_trip_the_censor_themselves`），
否则会变成"命中 → 换兜底 → 复检又命中 → 再换"的死循环。

### 7. 写操作必须幂等、加锁、留痕

项目八把"重复提交 / 并发重入"当成一类**真实事故**来防，而不是靠调用方自觉。
所有写操作走同一个五步套路：幂等键 → 分布式锁 → 状态机校验 → 业务执行 → 审计。

| 防护 | 实现 | 代码位置 |
|---|---|---|
| 幂等键 | 业务语义拼接后排序哈希；命中缓存后**重新读库**返回当前状态（客户重复提交想知道"我的线索到哪了"） | `flow_ops/idempotency.py` |
| 分布式锁 | owner 校验 + TTL，拿不到快速失败（`lock_busy`），绝不阻塞排队；**双后端**：SQLite（单机）/ Redis（多 worker，`SET NX PX` + Lua 原子释放与续约） | `flow_ops/idempotency.py`、`flow_ops/locking.py` |
| 状态机 | 跳阶段直接拒绝（`illegal_transition`），终态冻结，任何非终态可转 lost | `flow_ops/leads.py` |
| 审计 | 每个动作落 `ok / blocked / failed / idempotent_hit`，谁在什么时候对哪条线索做了什么可追溯 | `flow_ops/audit.py` |

> 锁后端是 **fail-closed**：Redis 挂了返回 503「未做任何修改，重试安全」，
> 绝不静默降级 SQLite —— 跨进程互斥已经失效，还给人"锁在工作"的错觉，比明确失败危险得多。

> 幂等命中返回**首次快照**是一个真实踩过的坑：调用方拿着过期状态去撞状态机，
> 报错还查不出原因。修复后有回归测试守着。

---

## 四、八个项目分别解决了什么

### 项目四 · 助贷对话数据中台

| 痛点 | 实现 | 代码位置 |
|---|---|---|
| 微信记录用不了 | 真实适配器：wxid→显示名、群消息发送者还原、消息类型占位、时间单位归一（只读打开） | `source.py` |
| 一训练就泄密 | **三层幂等脱敏** + 价格硬红线；号码先做**全角→半角归一化**再匹配（堵住"全角变体绕过"，归一化只作用于匹配层、1:1 等长映射保证幂等不被破坏）；导出前 `verify()` 断言零残留，否则整批拒绝 | `masking.py`、`exporter.py` |
| 垃圾数据污染检索 | 9 级分类（噪声直接丢，实测丢弃率 57%）+ 质量分（权重显式可配） | `filters.py` |
| 一份数据多下游 | 5 分钟窗切块 → 审核闸门 → ShareGPT/Alpaca/OpenAI/RAG CSV 四格式导出（内容哈希幂等） | `chunker.py`、`staging.py`、`exporter.py` |
| 全量重建知识产物太贵 | 知识构建默认**增量**（按上次构建水位只处理新批准条目、按 source 幂等覆盖；删除类变更走全量清理残留） | `knowledge.py`、`store.py` |
| 谁批准了哪批数据 | `review_audit` 全量留痕；审计按 rowid 兜底排序（同秒也不会乱） | `store.py` |
| 中台不可信怎么办 | **敏感对话可审但永不进训练集**（`dataset_eligible` 与 `keep` 分离）；项目七只吃 approved 语料 | `domain.py`、`app_context.py` |

### 项目五 · 企业多智能体编排平台

| 痛点 | 实现 | 代码位置 |
|---|---|---|
| Agent 重复造轮子 | 能力契约 + 装配器：业务只写 6 个纯函数 | `graph/capabilities.py`、`graph/builder.py` |
| 工具重复接入、上下文爆炸 | 统一注册 + **作用域裁剪**（实测单轮注入的工具 schema token 从全量 56 工具 ≈3.4k 降到 3–7 个 ≈0.2–0.4k，**省 88%–95%**） | `tools/registry.py` |
| 长任务无法中断恢复 | LangGraph Checkpointer 线程级快照 | `checkpointing.py`、`runtime.py` |
| 失败即崩 / 死循环烧钱 | 错误分级：重试 / 自修正 / 熔断转人工 | `resilience.py`、`graph/nodes.py` |
| 高风险动作无人把关 | HITL 检查点 + 审批中心 + 超时降级 | `hitl.py` |
| 效果好坏靠感觉 | span 级追踪 + 指标 + 决策轨迹回放 | `observability.py` |

### 项目二 · 产品知识助手与智能方案匹配

`RAG（查得到）→ 溯源（信得过）→ 结构化决策（用得上）→ 生成（交付得出去）`

- **检索**：BM25 + 稠密向量双路召回 → RRF 融合 → Rerank → **生效日期过滤** → 引用溯源；
- **拒答**：相关性低于阈值时明确拒答并转人工（"不敢承诺"本身就是产品能力）；
- **匹配**：硬性准入筛选 → 多目标排序（通过率/利率/额度，权重可配置）→ 主力 + 备选组合；
- **图谱**：产品-资质-用途多跳推理，支撑"征信查询多 + 有按揭房 + 想做置换"这类组合场景；
- **方案书**：结构化生成 + 合规免责（`以银行审批为准`、`不构成任何放款承诺`）。

### 项目三 · 业务员话术陪练与 AI 考核

- **陪练**：模拟客户基于真实画像，且**会抵抗**（犹豫/比价/隐瞒/推脱）—— 否则练不出本事；
- **评分**：合规红线 / 需求挖掘 / 异议处理 / 邀约促成 四维 rubric，
  **违规承诺一票否决**（总分压到 60 以下并判不通过）；
- **质检**：违规承诺 / 敏感信息 / SOP 缺失 / 响应时效四类规则，覆盖率 100%，只有 high 级才告警（避免告警疲劳）；
- **挖掘**：只从成交对话中挖掘，且**含违规表述的话术一律排除**，用"客户下一句正向反应"作为有效性证据；
- **校准**：`Judge.calibrate()` 提供双盲一致率与偏差方向，用于迭代评分 Prompt。

### 项目七 · 销售素材推广与客户激活 AI

业务前提：公司**没有内容自运营账号**，获客第二条腿是**业务员在自己账号（朋友圈/抖音/小红书）发素材**。

`结构模板（钩子→痛点→方案→CTA）→ 渠道语感适配 → 合规先审后发 → 脱敏 → 对症激活 → 归因`

- **生成**：三条渠道的读者心态不同，所以语感是**硬约束**（字数区间 / 标签数 / 是否口播），
  不是"同一套文案发三个渠道"；
- **合规**：`assemble()` 是产出素材的唯一路径，先审后发无法绕过 ——
  可自动改写的（承诺性话术 / 绝对化用语）改写留痕后发布，
  不可改写的（客户手机号 / 证件号）**直接拦截并转人工**；
- **脱敏**：做在生成**之前**（手机号 / 证件号 / 银行卡 / 微信 / 金额转区间 / 姓名），
  姓名脱敏不依赖词表，冷门姓也不会漏；
- **激活**：按犹豫原因对症匹配主题（费用/征信/流程/比价），并设**四道防骚扰闸门**
  （沉睡阈值 / 静默期 / 触达上限 / 避开深夜与周末）—— 触达计数来自真实流水而不是离线快照；
- **归因**：最朴素的漏斗 `impression → consult → lead → deal`，按渠道 × 素材聚合；
  **没有数据就不编结论**，素材档案缺失时明确标注且不给加量建议。

### 项目八 · 助贷业务流程数字员工

业务前提：成交前的四段流程（咨询 → 邀约 → 方案生成 → 费用评估）重复且漏不得，
业务员手动跟单的结果是**漏跟进率 15%**、到店前准备**平均 40 分钟**、重复提交**重复放款**。

数字员工守住四条边界：**只走状态机不跳步**（每步 SOP 都是留痕点）、
**接待/谈判/费用确认是人工节点**（Agent 禁区，代码级 `HumanStage` 拒绝进入）、
**高风险动作先审批后执行**（过期只能作废，绝不自动放行）、
**写操作全部幂等 + 分布式锁**（客户重复提交、定时任务重跑都不产生第二条记录）。

| 痛点 | 实现 | 代码位置 |
|---|---|---|
| 漏跟进（首呼 30 分钟没人管） | SLA 倒计时扫描 + 逾期预警 + **2×SLA 宽限期防轰炸** + 认领即静音 | `leads.py`、`workflow.py::sweep_sop` |
| 到店前临时翻记录 | 到店前一页纸：客户档案（项目一/四）+ 方案初稿（项目二）+ 费用预估 + 谈话要点 | `readiness.py` |
| 客户重复问"我进行到哪了" | 自助查询：双重身份校验 + 服务端脱敏（姓名留首字、无征信明细、`new` 阶段不对外） | `progress.py` |
| 重复操作事故 | 幂等键（业务语义排序哈希）+ owner 校验锁 + **命中缓存后重新读库**返回当前状态 | `idempotency.py`、`workflow.py` |
| 长任务中途锁过期被并发闯入 | 分布式锁带**看门狗自动续约**（按 ttl/3 周期续租，锁丢失即回调停止执行；只给长临界区开，短临界区不用，避免多一类失效模式） | `locking.py` |
| 客户催合同/催放款被自动回复 | `ApprovalGate.assert_approved()` 是唯一通行证；过期转 expired 转人工，**绝不自动批准** | `approval.py` |
| 费用口径说不清 | 等额本息确定性计算（**全程 Decimal 精确运算**，杜绝浮点误差）+ 服务费分档（<30 万 3% / ≥30 万 2%）+ 免责声明是必填字段 | `fees.py` |
| 改了规则怕改坏 | 11 条内置行为用例（SOP/幂等/费用/HITL/脱敏）+ 基线对比，退化超阈值拒发 | `evaluation.py` |
| 预警只落库、没人看 | 通知渠道：`LogNotifier`（默认零配置）/ `WebhookNotifier`（企微/钉钉 markdown）；预警一轮一个 digest、审批建单即通知、过期清理也通知；**发送失败只记日志，绝不拖垮扫描** | `notifier.py` |
| 预警靠主管手动点"扫描" | SOP 调度器：asyncio 常驻循环（`to_thread` 不阻塞事件循环）、单轮失败自愈、**分布式调度锁**（多副本同一时刻只有一个实例在扫，抢不到即跳过本轮）+ 2×SLA 宽限期 | `scheduler.py` |

> 项目八**不 import 任何业务项目的代码**：方案初稿调 `plan_provider`、档案调 `profile_provider`，
> 两者由装配层注入 —— 它自己是纯流程层，换一套业务照样能跑。

### 项目六 · 大模型推理网关与 Agent 可观测评测平台

解决"上线之后才会暴露"的三类问题：**成本失控 / 质量看不见 / 出了事查不出**。

| 能力 | 实现 |
|---|---|
| 成本治理 | 复杂度路由（轻任务走小模型）+ 前缀/语义缓存 + 上下文裁剪，实测节省 ~90% |
| 计数与校准 | token 计数**分语种**（中文约 1 token/字，避免按"字符/4"把中文低估 4 倍导致预算/配额形同虚设，tiktoken 可用时优先精确计数）；语义缓存阈值提供 `SemanticCache.calibrate()` 用标注样例探测 |
| 降级安全 | 降级链**只能往下退**；small 档没有后备，要升档必须显式指定 |
| 可用性 | 限流按模型隔离；配额耗尽先降档保服务，**降无可降才 fail-closed** |
| 可观测 | `CostRecord` 粒度到"每会话/每任务单价"，`replay(thread_id)` 回放决策轨迹 |
| 质量门禁 | 规则判分（**合规红线一票否决**）+ LLM-as-Judge（配双盲校准）+ 与基线比 → 拦发布 |

> 最隐蔽的一个坑写在代码注释里：CI 准入必须**先取基线、再跑本次评测** ——
> 反过来的话"基线"就是自己，gate 永远通过。

### 项目一 · AI 实时语音对话智能客服

客户点广告进来，第一件事不是加微信，而是"先问两句看看靠不靠谱"。
语音客服要接住的正是这个瞬间：开口能答、答得对、不承诺、当场推进到下一步。

**架构上最本质的一件事**：不用火山内置 LLM，改成**第三方 LLM 回调**指向自己的服务 ——
于是 RAG、提示词版本、内容安全全部握在自己手里，而音频流仍然走 RTC 的 UDP 通道，
Python 服务永远不是带宽/CPU 瓶颈。

| 难点 | 实现 | 代码位置 |
|---|---|---|
| 进房权限按房间/用户收紧 | 001 Token（nonce + 签发/过期 + room/user + 权限位，HMAC-SHA256）+ 本地自校验 | `rtc_token.py` |
| OpenAPI 调用要签名 | V4 签名（与 SigV4 同构，query 排序 / header 小写 / 日期同日） | `volc_sign.py` |
| 听得准、延迟低 | 双向流 ASR（`StreamMode=2`）+ 业务热词 + 情绪检测 | `agent_config.py` |
| 说话像人且客户能听完 | 回复夹在 150~300 tokens、禁列表/符号、按句 TTS、语速按场景调 | `agent_config.py`、`dialog.py` |
| 首字延迟 5s → 2s 内 | system 只一条 + 知识块按 `chunk_id` 稳定排序（前缀缓存命中） | `prompt.py` |
| 说错话收不回来 | 逐句过审 + 命中即换兜底话术并终止 + 整轮复检 | `censor.py`、`dialog.py` |
| 客户插话 AI 还在说 | RTC 停 TTS，服务端置 `CancelToken`，生成循环在句界退出 | `dialog.py`、`api.py` |
| 改提示词说不清好坏 | Golden Set 七条 + 红线一票否决 + 准入三条拒发理由 | `evaluation.py` |

工程上还有两个刻意设计：

- **零密钥可跑通**：`RtcClient` 在没配 AK/SK 时退化为 `RecordingTransport`，
  请求只记录不下发 —— 于是测试和 demo 能断言"我们下发了什么配置"（ASR 是否双向流、
  TTS 集群对不对、回调地址是否指向自己），而不用真的开房花钱；
- **检索挂了也要说话**：`VoiceRetriever` 的检索能力是函数注入，
  异常一律退化为"没查到 → 转人工"，绝不 500（云端拿不到 200 会重试并放弃，客户听到的是"AI 哑了"）。

> 排障记住一条：**回调地址必须公网可达**（ngrok 或直接部署）。
> `localhost` 时表现为"日志一切正常但 AI 不说话"，这是最坑的一种症状。

---

## 五、测试与质量

```bash
pytest                      # 693 个用例（691 通过 + 2 跳过：真 Redis / 真 PG 集成）
pytest -m integration        # 需要外部服务的集成测试（默认跳过）
ruff check src tests         # 代码规范
```

| 测试文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_platform.py` | 24 | 作用域裁剪与权限边界（真实注册中心 85%–97% 口径）、重试/熔断/预算、HITL 中断与恢复、编排端到端 |
| `tests/test_mcp_adapter.py` | 19 | MCP 适配层：ToolSpec↔MCP 双向映射、版本协商、high 风险工具默认不出现在 tools/list、stdio 与 StreamableHTTP 双传输、HTTP 端点走同一鉴权 |
| `tests/test_thread_map.py` | 4 | thread_id 映射持久化（多副本恢复不丢状态） |
| `tests/test_product_assistant.py` | 26 | 检索与拒答、政策时效过滤、匹配与淘汰归因、方案书合规 |
| `tests/test_coaching.py` | 34 | 模拟客户抗性、评分一票否决、质检规则、话术挖掘、会话持久化 |
| `tests/test_marketing.py` | 61 | 脱敏、合规两段式、渠道语感、案例脱敏、激活防骚扰、归因口径、HITL 发送 |
| `tests/test_voice.py` | 122 | 进房 Token 篡改/过期、V4 签名、云端配置、五条红线与 PII、检索闸门、切句、逐句过审、打断、评测准入、HTTP + SSE 回调 |
| `tests/test_gateway.py` | 64 | 路由与降级链、缓存 TTL/阈值与 calibrate、裁剪、配额 fail-closed、成本口径、评测与 CI 准入 |
| `tests/test_flow_ops.py` | 92 | 状态机与 SOP 宽限期、幂等与锁（含看门狗续约）、审批门三条铁律、费用公式与分档、到店前准备、自助查询脱敏、审计、评测准入、工具注册与作用域、HITL 中断恢复、HTTP 全接口 |
| `tests/test_api.py` | 17 | 全部 HTTP 接口（含离线 Swagger 静态资源） |
| `tests/test_prod_hardening.py` | 12 | `/ready` 不泄露内部异常、API Key 鉴权中间件（default-deny + 白名单 + 双凭证 + 密钥轮换 + fail-closed + 语音回调豁免） |
| `tests/test_flow_notifier.py` | 16 | Log/Webhook 渠道本体、消息体格式（企微/钉钉）、三个通知集成点、通知失败不拖垮业务 |
| `tests/test_flow_scheduler.py` | 16 | 启停生命周期、首轮立即扫描、单轮失败自愈、间隔下限、分布式调度锁、装配层只建不启、lifespan 启动路径 |
| `tests/test_flow_locking.py` | 29 | 双锁后端互斥/owner/TTL/续约/看门狗、fail-closed、工厂路由、真 Redis 集成（默认跳过） |
| `tests/test_checkpointer_backends.py` | 9 | memory/sqlite/PG 三后端、配置层校验、不可达 fail-fast、DSN 脱敏、真 PG 集成（默认跳过） |
| `tests/test_metrics_endpoint.py` | 12 | Prometheus 渲染器（转义/数值格式）、采集器（五个指标族）、HTTP（白名单放行/采集炸了仍 200） |
| `tests/test_data_platform.py` | 78 | 脱敏六类 PII（含全角变体归一化）+ 价格 + 优先级 + 幂等；9 级分类与质量分；时间窗切块与跨会话去重；微信夹具/群发送者/媒体占位；审核状态机与编辑重置；四格式导出与敏感排除；知识增量构建与检索；HTTP 全流程 |
| `tests/test_web_contract.py` | 2 | **真 HTTP 联调**：子进程起 uvicorn 跑 14 步业务全链路（鉴权→录入→状态机→邀约→到店→准备包→费用→HITL→自助查询→SOP→问答→`/metrics`→`/ui`） |

测试全部离线运行（零密钥、零网络，本机约 30 秒）。

---

## 六、延伸阅读

**新人路线（按顺序读）**

- [`docs/00-onboarding.md`](docs/00-onboarding.md) —— 2 天学习地图（唯一入口，先读这份）
- [`docs/10-code-map.md`](docs/10-code-map.md) —— 源码地图：业务概念 → 代码位置反查
- [`docs/11-data-flow.md`](docs/11-data-flow.md) —— 四条数据流主线图解（AI 对话 / 业务流程 / 数据中台 / 素材激活）
- [`docs/12-self-check.md`](docs/12-self-check.md) —— 自检题库（20 题）+ 5 个递进练习 + 验收单
- [`docs/onboarding-plan.md`](docs/onboarding-plan.md) —— 新人文档体系的设计方案与决策记录

**参考资料（按需查阅，不按顺序读）**

- [`docs/01-architecture.md`](docs/01-architecture.md) —— 架构与关键设计决策
- [`docs/02-getting-started.md`](docs/02-getting-started.md) —— 详细上手与配置说明
- [`docs/03-api.md`](docs/03-api.md) —— 接口清单与调用示例
- [`docs/04-gateway.md`](docs/04-gateway.md) —— 成本治理、可观测与评测门禁（项目六）
- [`docs/05-marketing.md`](docs/05-marketing.md) —— 素材生成、合规与客户激活（项目七）
- [`docs/06-voice.md`](docs/06-voice.md) —— RTC 接入、Token/签名、内容安全与评测准入（项目一）
- [`docs/07-flow-ops.md`](docs/07-flow-ops.md) —— 流程数字员工：状态机、SOP、HITL 边界与可靠性（项目八）
- [`docs/08-web.md`](docs/08-web.md) —— Web 工作台：页面、代理/挂载两种模式与多 worker 部署
- [`docs/09-data-platform.md`](docs/09-data-platform.md) —— 对话数据中台：四层数据流、脱敏/分类/审核/导出的五个关键决策（项目四）

---

> 本仓库中的数据（产品编号、银行名、客户信息、历史对话）**均为构造的演示数据**，
> 不涉及任何真实机构与个人信息。生产接入时请替换 `.data/` 下的数据源，
> 并重新校准检索阈值（见 `docs/02-getting-started.md`）。
