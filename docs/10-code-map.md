# 源码地图：从业务概念反查代码

> **怎么用这份文档**：你想改某个业务行为时，不要从 `src/` 目录树开始翻，直接从下面的表 A 找到文件和符号。
> 每个锚点写成 `文件::符号` 的形式（用符号名而不是行号——代码一改行号就失效，符号名不会）。
> 这些锚点可以被机器校验：`python scripts/onboarding_check.py --map docs/10-code-map.md`。
>
> **配套**：读这份文档之前，先按 `docs/00-onboarding.md` 的 Day 1 跑通系统、建立业务全景。
> 这份地图解决的是"代码在哪"，不解决"业务为什么这样流"。

---

## 一、表 A：业务动作 → 代码位置

按"我想改什么"组织，覆盖新人最常动的 40 处。

### 1.1 线索状态机（项目八的心脏）

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 加一个新阶段 | `src/flow_ops/domain.py::LeadStage` | `leads.py::ALLOWED_TRANSITIONS` 补边 + `workflow.py::_STAGE_ORDER` 插入 + 测试 |
| 改阶段流转规则（允许/禁止某条边） | `src/flow_ops/leads.py::ALLOWED_TRANSITIONS` | `tests/test_flow_ops.py` 补正反用例 |
| 改漏斗/看板的阶段顺序 | `src/flow_ops/workflow.py::_STAGE_ORDER` | 前端 `web/src/hooks.ts` 的 `stageLabel` |
| 加一个"阶段"对应的 SOP 动作 | `src/flow_ops/leads.py::build_sop_rules` | `.env` 的 `FLOW_SOP_*` 阈值 |
| 改 SOP 超时时长 | `src/agent_platform/config.py` | `Settings::flow_first_call_sla_minutes` 等；口径见 `docs/02-getting-started.md` 第 3.8 节 |
| 改 SOP 扫描频率/启停 | `src/flow_ops/scheduler.py::SopScheduler` | `.env` 的 `FLOW_SCHEDULER_ENABLED` |

> **注意**：`ALLOWED_TRANSITIONS` 是 `dict[LeadStage, frozenset[LeadStage]]`，加一条边 = 加一个集合元素。
> 非法流转会在 `leads.py` 的校验函数里抛错，错误信息会列出"允许的目标"——这是新人调试时最先看到的东西。

### 1.2 审批与高风险动作（HITL）

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 把某个动作列入/移出高风险清单 | `src/agent_platform/config.py` | `Settings::flow_high_risk_action_set`；配 `.env` 的 `FLOW_HIGH_RISK_ACTIONS` |
| 改审批单的行为（批准/驳回/过期） | `src/flow_ops/approval.py::ApprovalGate` | `approval.py::ACTION_DESCRIPTIONS` 加中文描述 |
| 改"批准 ≠ 已执行"的落库时机 | `src/flow_ops/approval.py` | `ApprovalGate::mark_executed`；注意 `workflow.py` 里调用它的位置（**顺序不能反**） |
| 加一种新的高风险动作类型 | `src/flow_ops/approval.py::ACTION_DESCRIPTIONS` | 前端 `web/src/hooks.ts` 的 `APPROVAL_ACTION_LABELS` |

> **为什么 `mark_executed` 的调用顺序不能反**：`workflow.py` 的 `execute_approved` 是**先调 CRM、再标已执行、最后写审计**。
> 如果先标后调，会出现"状态显示已执行，但其实通知没发出去"——新人最容易在这里埋 bug。

### 1.3 费用与额度（销售最关心的数字）

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 改服务费分档/试算公式 | `src/flow_ops/fees.py::FeeEstimator` | `tests/test_flow_ops.py` + 文档口径 |
| 改额度测算的"能不能乘倍数" | `src/product_assistant/domain/matching.py::_INCOME_QUOTA_BASES` | 同文件的风险提示文案 |
| 改产品准入的排序权重 | `src/product_assistant/domain/matching.py::MatchingWeights` | `::MatchingEngine` |
| 加一款新产品 | `.data/policies/products.json` | 数据文件，无需改代码（这是刻意设计） |

> **`_INCOME_QUOTA_BASES` 是什么**：只有基数落在"月收入/公积金基数/社保基数/个税/工资/打卡"这些词上，
> 才允许 `月收入 × quota_multiple`。房产抵押的 `quota_multiple=0.7` 是**抵押率**不是收入倍数，
> 基数写的是"房产评估价"——不乘。漏了这个门控，房产抵押贷会算出"月收入 3 万 × 0.7 = 2.1 万"这种荒谬额度。

### 1.4 幂等、锁、审计（写操作三件套）

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 改幂等键的构成 | `src/flow_ops/idempotency.py::make_idempotency_key` | **前后端必须同源**（`web/src/api.ts`） |
| 改锁的获取/释放/续约 | `src/flow_ops/idempotency.py::LockGuard` | `flow_ops/locking.py`（Redis/SQLite 双后端） |
| 查审计四态怎么落库 | `src/flow_ops/store.py` | `approvals` / `audit_log` 表；结果枚举在 `flow_ops/audit.py` |
| 加一种审计结果态 | `src/flow_ops/audit.py` | 结果枚举；前端翻译加在 `web/src/hooks.ts` 的 `auditResultLabel` |

> **幂等键的铁律**：必须按业务语义生成（如 `intake:${customer_id}:${source}:${owner}`），
> **不能是随机 UUID**。随机键防不住"用户重复点击/网络重试"，等于没防。

### 1.5 到店、进度、通知

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 改到店前一页纸的内容结构 | `src/flow_ops/readiness.py::ReadinessBuilder` | 前端抽屉"到店准备" tab |
| 改客户自助查进度的脱敏口径 | `src/flow_ops/progress.py::ProgressService` | 前端 `web/src/pages/ProgressPage.tsx` |
| 加一个通知渠道 | `src/flow_ops/notifier.py::build_notifier` | 实现 `::Notifier` Protocol（失败绝不抛） |
| 改 CRM 对接 | `src/flow_ops/crm.py` | CRM 客户端类；同样要有离线实现 |

### 1.6 语音客服（项目一）

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 加/改一条内容安全红线 | `src/voice_service/censor.py::Censor` | 兜底话术的回归测试（**兜底话术自己不能踩红线**） |
| 改变体/拼音规避的识别 | `src/voice_service/censor.py::normalize_for_scan` / `_PINYIN_ALIASES` | 新增拼音别名要挂到正确的 `rule_id`；变体用例加在 `tests/test_voice.py` |
| 改脱敏规则 | `src/voice_service/censor.py::mask_pii` | `::CensorRule` |
| 改 LLM 回调处理 | `src/voice_service/api.py` | 回调端点；`rtc_client` dry-run 时不真下发 |
| 改首字延迟/Golden Set 口径 | `src/voice_service/evaluation.py` | 评测模块；指标出口在 `metrics.py` |

### 1.7 推理网关（项目六）

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 改复杂度路由的判据 | `src/gateway/router.py::ComplexityRouter` | `docs/04-gateway.md` |
| 改缓存命中策略 | `src/gateway/cache.py::PrefixCache` `::SemanticCache` `::CacheLayer` | 缓存的 key 生成 `::prompt_key` |
| 改上下文裁剪 | `src/gateway/trim.py` | 裁剪逻辑；连带 token 统计口径 |
| 改流式降级窗口/中途失败策略 | `src/gateway/gateway.py::LLMGateway::stream_complete` | `tests/test_gateway.py` 的流式三态用例 |
| 改 provider 级熔断 | `src/agent_platform/resilience.py::CircuitBreaker` | 网关实例化与按 `provider.name` 记账在 `src/gateway/gateway.py::LLMGateway`；阈值在 `Settings.circuit_failure_threshold`；状态出口 `/gateway/stats` 的 `breaker` |
| 改成本台账/评测集 | `src/gateway/store.py` / `observe.py` | 台账表；Golden Set 走外部 jsonl |

### 1.8 素材推广与数据中台（项目七/四）

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 改素材"先审后发"的合规检查 | `src/marketing/compliance.py` | 合规模块；唯一出口是 `generator.py::assemble` |
| 改渠道语感/案例包装 | `src/marketing/generator.py::assemble` | 模板 json |
| 改激活策略/防骚扰 | `src/marketing/activation.py` | 激活模块；语料与归因在 `cases.py` `attribution.py` |
| 改脱敏规则 | `src/data_platform/masking.py::MaskingEngine` | 候选优先级 |
| 改审核闸门的三条边 | `src/data_platform/staging.py::ReviewGate` | **编辑即重置**，不能加"直接通过"的边 |
| 改导出格式 | `src/data_platform/exporter.py` | 导出模块；key 必须内容哈希 |

### 1.9 平台底座（意图路由 / 追踪 / RAG 校验）

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 改三级意图路由的采纳顺序 | `src/agent_platform/routing/composite.py::ThreeTierRouter` | 规则词表 `routing/presets.py`；评测集 `data/intent_eval.jsonl` |
| 改意图关键词/优先级 | `src/agent_platform/routing/presets.py::loan_intent_keywords` | 重跑 `python scripts/eval_intent.py --min-f1 0.85` |
| 改意图澄清阈值 | `src/agent_platform/routing/base.py::ClarifyPolicy` | `ThreeTierRouter` 的构造参数 |
| 改 trace_id 的生成/传播 | `src/agent_platform/tracing.py::ensure_trace_id` | span 绑定在 `observability.py::Tracer`；日志与 `span_*_ms` 指标同处 |
| 让 Critic 真正打回重做 | `src/agent_platform/graph/capabilities.py::AgentCapabilities` | 字段 `max_critique_rounds`；写工具重放保护在 `graph/nodes.py::make_execute_node` |
| 改 RAG 答案的引用一致性校验 | `src/product_assistant/rag/verify.py::verify_answer` | 拒答分支在 `src/product_assistant/service.py::ProductAssistantService` |

### 1.10 前端

| 我想改什么 | 锚点（文件::符号） | 顺带要动 |
|---|---|---|
| 把机器键翻成人话 | `web/src/hooks.ts` | `stageLabel` `actionLabel` `ownerLabel` `actorLabel` `auditText`；**只在这一层翻，页面不许自己拼字符串** |
| 改响应式断点/布局 | `web/src/styles.css` | `.g-2` `.g-3` `.g-split` `.g-side` `.g-kpi`；**禁止内联 `gridTemplateColumns`**（媒体查询覆盖不掉） |
| 加一个页面 | `web/src/pages/` + `App.tsx` | 路由表；`hooks.ts` 补翻译 |
| 改 API 调用 | `web/src/api.ts` | 端点封装；幂等键与后端同源 |

---

## 二、表 B：九个包，各一页

每个包固定六项：**职责 / 入口 / 核心类 / 依赖谁 / 被谁依赖 / 不许改什么**。

### 2.1 `agent_platform` —— 企业多智能体编排平台（项目五，底座）

- **职责**：Agent 运行时、LangGraph 编排、工具注册与作用域裁剪、HITL、checkpoint、API 装配。**它是底座，不含有任何业务判据。**
- **入口**：`src/agent_platform/api/app.py::create_app`（整个系统的 HTTP 装配根）
- **核心类**：`src/agent_platform/runtime.py::AgentRuntime`、`src/agent_platform/tools/registry.py::ToolRegistry`、`graph/builder.py`、`hitl.py`
- **依赖谁**：只依赖第三方（LangGraph、FastAPI），不依赖任何业务包
- **被谁依赖**：所有业务包
- **不许改什么**：**不许让平台 import 业务包**。评测目标、provider 一律由装配层（`src/app_context.py`）注入。有机器化测试守这条边界，违反会直接红。

### 2.2 `flow_ops` —— 助贷业务流程数字员工（项目八，业务主线）

- **职责**：11 阶段状态机、SOP 漏跟进预警、HITL 审批门、到店准备包、费用试算、幂等+锁+审计。
- **入口**：`src/flow_ops/service.py::FlowOpsService`、`api.py`（流程端点）
- **核心类**：`leads.py`（状态机）、`workflow.py`（流程编排）、`approval.py::ApprovalGate`、`fees.py::FeeEstimator`、`readiness.py::ReadinessBuilder`、`progress.py::ProgressService`、`scheduler.py::SopScheduler`、`store.py`
- **依赖谁**：`agent_platform`、`product_assistant`（方案与档案通过 provider 注入，不直接 import 其内部）
- **被谁依赖**：`app_context.py` 装配
- **不许改什么**：**状态机不许被绕过**。任何推进都必须走 `workflow.py` 的合法流转；不允许直接 UPDATE 数据库改阶段——那样 SOP、审计、漏斗全部失真。

### 2.3 `product_assistant` —— 产品知识助手与智能方案匹配（项目二）

- **职责**：政策条款 RAG、客户资质 → 产品准入 → 可解释方案排序、拒答。
- **入口**：`src/product_assistant/service.py::ProductAssistantService`
- **核心类**：`domain/matching.py::MatchingEngine`、`domain/matching.py::MatchingWeights`、`proposal.py`、`api.py`
- **依赖谁**：`agent_platform`
- **被谁依赖**：`flow_ops`（准备包的方案初稿）、`app_context.py`
- **不许改什么**：**准入筛选、评分、拒答判据必须用确定性代码**，LLM 只做语言层。把"这个客户能不能过"交给模型判断，是这个包里唯一不可接受的事。

### 2.4 `voice_service` —— AI 实时语音对话智能客服（项目一）

- **职责**：火山 RTC 签房、双向流 ASR/TTS、**第三方 LLM 回调**、逐句内容安全、首字延迟观测。
- **入口**：`src/voice_service/service.py::VoiceService`、`api.py`（回调端点）
- **核心类**：`censor.py::Censor`、`rtc_client.py`、`session.py`、`dialog.py`、`evaluation.py`
- **依赖谁**：`agent_platform`、`gateway`（LLM 调用）
- **被谁依赖**：`app_context.py`
- **不许改什么**：**每一句对外的话，出口前必过 `Censor.check()`**。兜底话术同样要过——"兜底话术自己踩红线"是真实发生过的事故类型，有回归测试守。生产环境 `VOICE_CALLBACK_API_KEY` 必填。

### 2.5 `gateway` —— 大模型推理网关与可观测评测平台（项目六）

- **职责**：复杂度路由、前缀/语义缓存、上下文裁剪、限流配额、密钥轮转、成本台账、会话回放、Golden Set 评测。
- **入口**：`src/gateway/service.py::GatewayService`
- **核心类**：`router.py::ComplexityRouter`、`cache.py::CacheLayer`、`trim.py`、`keys.py`、`observe.py`、`store.py`
- **依赖谁**：`agent_platform`、第三方 LLM SDK
- **被谁依赖**：`voice_service`、其他需要 LLM 的包
- **不许改什么**：**不许 import 任何业务包**。它不知道什么是"线索"、什么是"产品"，只认识 message 和 token。这条边界有测试守着。

### 2.6 `coaching` —— 业务员话术陪练与 AI 考核（项目三）

- **职责**：角色对练、评分锚点、质检规则、话术挖掘。
- **入口**：`src/coaching/service.py::CoachingService`
- **核心类**：`roleplay.py`、`judge.py`、`qc.py`、`mining.py`、`domain.py`
- **依赖谁**：`agent_platform`、`gateway`
- **被谁依赖**：`app_context.py`
- **不许改什么**：**评分必须给明确锚点**，通过率保守估算。不能出一个"78.5 分"却不告诉业务员分扣在哪。

### 2.7 `marketing` —— 销售素材推广与客户激活 AI（项目七）

- **职责**：素材生成、渠道语感、合规先审后发、脱敏、案例包装、对症激活 + 防骚扰、归因。
- **入口**：`src/marketing/service.py::MarketingService`
- **核心类**：`generator.py::assemble`、`compliance.py`、`desensitize.py`、`activation.py`、`cases.py`、`attribution.py`
- **依赖谁**：`agent_platform`、`gateway`、`data_platform`（可选，已审对话）
- **被谁依赖**：`app_context.py`
- **不许改什么**：**素材只有 `assemble()` 一个出口，且必须先过合规再下发**。绕过它在别处直接发消息，合规就成了空话。

### 2.8 `data_platform` —— 助贷对话数据中台（项目四）

- **职责**：raw 不可变 → 9 级分类 + 三层脱敏 → 审核闸门 → 四格式导出 + 知识切片。
- **入口**：`src/data_platform/service.py::DataPlatformService`
- **核心类**：`masking.py::MaskingEngine`、`staging.py::ReviewGate`、`exporter.py`、`filters.py`、`chunker.py`、`knowledge.py`
- **依赖谁**：`agent_platform`（只注册 `data.*` 工具，不注册 Agent）
- **被谁依赖**：`marketing`（已审对话）、`product_assistant`（知识）
- **不许改什么**：**`keep`（敏感可审）与 `dataset_eligible`（可进训练集）必须分离**；审核只有三条边，编辑即重置并重跑脱敏。给审核加一条"一键通过"的捷径，这个中台就失去存在意义。

### 2.9 `web/` —— 业务工作台（TypeScript + Vite + React）

- **职责**：八个页面，业务员/主管的日常操作入口。
- **入口**：`src/main.tsx` → `App.tsx`（hash 路由）→ `components/Shell.tsx`（导航外壳）
- **核心**：`hooks.ts`（**唯一的翻译层**）、`api.ts`（端点封装 + 幂等键）、`components/LeadDrawer.tsx`（客户详情四 tab）、`pages/`（8 页）
- **依赖谁**：只调 HTTP 接口
- **被谁依赖**：无（最上层）
- **不许改什么**：**页面里不许出现业务规则副本**。"if 阶段==xx 就显示 yy"这种判据必须来自后端；前端只做展示与触发。机器键翻译只能加在 `hooks.ts`。

---

## 三、表 C：七条设计原则的代码坐标

README 第三节讲了"为什么"，这里给"在哪看、谁在守"。

| # | 原则 | 代码坐标 | 守它的测试 |
|---|---|---|---|
| 1 | 业务判据用确定性代码 | `matching.py::MatchingEngine`、`censor.py::Censor`、`fees.py::FeeEstimator`、`leads.py::ALLOWED_TRANSITIONS` | 各包的领域测试 |
| 2 | 外部依赖都有离线实现 | 各包的 Echo/Local/InMemory 实现 | `tests/test_offline_*.py` |
| 3 | 阈值权重显式可调 | `config.py::Settings` + `.env` | 配置加载测试 |
| 4 | 高风险动作必须经过人 | `approval.py::ApprovalGate` | `test_flow_ops.py` 的 HITL 用例 |
| 5 | 基础设施不许反向依赖业务 | `gateway/` 的 import 约束 | `test_platform.py` 的机器化校验 |
| 6 | 合规是流程约束不是清单 | `src/marketing/generator.py::assemble`、`censor.py::Censor` | 素材/话术的合规回归 |
| 7 | 写操作幂等+加锁+留痕 | `idempotency.py::make_idempotency_key`、`::LockGuard`、`store.py` 审计四态 | `test_idempotency*`、`test_flow_ops.py` |

---

## 四、五个"看似合理的错误改法"

新人第一周最容易犯的错，不是不会写，是**在错的层写**。逐个讲透。

### 反例 1：把判据搬到前端

> "前端判断一下，如果阶段是 `invited` 就显示倒计时，后端不用改了。"

**错在哪**：判据一旦进了前端，就有两份实现。后端改了 SOP，前端还在按老规则显示，业务员看到的是错的。
**正确位置**：后端 `leads.py::build_sop_rules` 算好，前端只渲染。前端唯一的自由是"怎么显示得好看"。

### 反例 2：幂等键用随机 UUID

> "加个 uuid 保证唯一，重复提交就防住了。"

**错在哪**：随机键每次请求都不同，"重复点击"和"网络重试"生成的是两个不同键，两条都执行了。
**正确位置**：`idempotency.py::make_idempotency_key`，按业务语义拼（`intake:客户:渠道:负责人`）。前后端必须同源。

### 反例 3：Redis 连不上就降级 SQLite

> "Redis 挂了先降级，保证可用性。"

**错在哪**：多 worker 下降级成 SQLite 锁，两个副本会同时抢到同一把锁，重复发通知、重复推进状态。
**正确位置**：`locking.py` 是 **fail-closed**——Redis 不可用直接返回 503「未做任何修改，重试安全」，绝不静默降级。可用性让给正确性。

### 反例 4：先标已执行，再调 CRM

> "先把状态改成已执行，用户看着舒服，然后再发通知。"

**错在哪**：如果 CRM 调用失败，状态已经是"已执行"，但事情没办。审计会说谎。
**正确位置**：`workflow.py::execute_approved` 的顺序是 **CRM → mark_executed → 审计**，反了就出"标了已执行但其实没发"。

### 反例 5：给审核加"一键通过"

> "审核太麻烦了，加个批量通过按钮。"

**错在哪**：`data_platform` 的存在意义就是"人审"。批量通过等于没有闸门，脏数据直接进训练集。
**正确位置**：`staging.py::ReviewGate` 只有三条边，逐条人审。嫌慢就优化单条审核体验，不是跳过它。

---

## 五、命名约定与常见陷阱

| 你看到的 | 实际含义 | 陷阱 |
|---|---|---|
| `stage_entered_at` | 进入**当前**阶段的时间 | 不是线索创建时间；SOP 判据用它 |
| `ALLOWED_TRANSITIONS` 的 key/value | key 是起点，value 是**允许的目标集合** | 加边要加在 value 里，不是加新 key |
| `quota_multiple` | **收入类基数**才是倍数；抵押类是抵押率 | 见 `_INCOME_QUOTA_BASES` |
| 审计 `result` 四态 | `ok` / `blocked` / `failed` / `idempotent_hit` | `idempotent_hit` 是**成功拦截**，不是失败 |
| 前端 `ownerLabel` 表 | `rep_01` → 张晓东 | 新员工要往 `hooks.ts` 加，不要在页面里 if-else |
| `.data/` 下的 json | 演示数据，改它不用改代码 | 生产数据走 `.env` 指的真实路径 |
| `executed_at` | 事情办没办 | 与 `status`（人批没批）是两件事，见反例 4 |

---

## 六、这份文档怎么保持不过期

1. **锚点用符号名，不用行号**——本文所有 `::符号` 都能被 `scripts/onboarding_check.py` 校验，符号被改名/删除会立刻报错。
2. **数字给取数命令**：测试用例数用 `pytest --collect-only` 汇总（**别再加 `-q`**：pyproject 的 `addopts` 里已有一个 `-q`，叠加成 `-qq` 会把汇总行吞掉），不要手抄。
3. **新增模块必须同步表 B**：这是提交清单的一项。
4. **发现某个锚点错了，改文档，不要绕过**——文档说谎比没有文档更糟。
