# 数据流向：四条主线图解

> **这份文档解决"数据怎么流"**：一条客户消息、一条线索、一批对话、一条素材，分别从进系统到落库经过哪些类、在哪一步被拦、在哪一步留痕。
> 配套：代码位置用 `文件::符号` 标注，全部可被 `python scripts/onboarding_check.py --map docs/11-data-flow.md` 机器校验。
> 读之前：先按 `docs/00-onboarding.md` 的 Day 1 跑通系统、建立业务全景。

---

## 零、先建立坐标系：所有流都汇进三个"总闸"

不管哪条流，落到最后都是这三样东西：

```
                     ┌────────────────────────────────────────────┐
   任何 LLM 调用  →  │ 项目六 网关（LLMGateway.complete）           │ 裁剪 → 路由 → 缓存
                     │                                    限流 → 配额 → 降级链 → 成本台账 │
                     └────────────────────────────────────────────┘
                     ┌────────────────────────────────────────────┐
   任何 Agent 运行  →  │ 项目五 编排（AgentRuntime.invoke → 图）     │ route → plan → execute
                     │                                    预算熔断 → HITL → critique → respond │
                     └────────────────────────────────────────────┘
                     ┌────────────────────────────────────────────┐
   任何写操作      →  │ 项目八 五步套路（幂等 → 锁 → 校验 → 执行 → 审计）│ 审计四态落 store
                     └────────────────────────────────────────────┘
```

下面四条主线，都是在这三个总闸上"插业务"。

---

## 一、AI 对话流（覆盖项目一 / 二 / 三）

客户说的每一句话，从进系统到落库的完整路径。**这是最容易被问的一条**——语音、问答、陪练三条支路，主干是同一条。

### 1.1 语音支路（项目一，RTC 回调）

```
客户开口
  │
  ▼
RTC 云端 ASR（双向流，只回最终文本）── 音频永远不走 Python 服务
  │
  ▼
POST /voice/callback/chat（OpenAI 协议，stream=true）
  │  src/voice_service/api.py::callback_chat
  │  ├─ _check_callback_auth：独立回调 Key（VOICE_CALLBACK_API_KEY），不是普通 API Key
  │  └─ 客户端断开/插话 → CancelToken 置位（dialog.py::CancelToken）
  ▼
VoiceService.stream_reply ─── src/voice_service/service.py::stream_reply
  │  （解析 session、分配权威轮次号、跑完把轮次 append 落库）
  ▼
VoiceDialogEngine.stream_turn ─── src/voice_service/dialog.py::stream_turn
  │
  ├─ ① censor.check_user_text：客户要求造假/要求承诺 → 直接拒绝 + 转人工
  │     src/voice_service/censor.py::check_user_text
  ├─ ② VoiceRetriever.retrieve：1~3 条知识，带相关性闸门
  │     src/voice_service/rag.py::VoiceRetriever
  ├─ ③ 无依据 → 不调模型，直接转人工（首字 0.1s，且不会编）
  ├─ ④ PromptRegistry.get + render：system 只有一条，知识块拼在里面
  │     src/voice_service/prompt.py::PromptRegistry
  │     └─ 知识按 chunk_id 固定排序 → 前缀缓存命中 → 首字 5s → 2s 内
  ├─ ⑤ 流式生成 → SentenceSplitter 切句 → 每句 Censor.check → 逐句吐给 TTS
  │     src/voice_service/dialog.py::SentenceSplitter
  │     命中红线：这句不念，换兜底话术，整轮终止
  ├─ ⑥ 整轮全量复检：防"跨句拼出来的红线词"
  └─ ⑦ store.append_turn：轮次 + 延迟拆解（RAG/首字/总时长）落 SQLite
        src/voice_service/service.py::stream_reply → store.append_turn
  │
  ▼
SSE data: {"choices":[{"delta":{...}}]} → RTC 云端 TTS → 客户听到
```

### 1.2 产品问答 / 陪练支路（项目二 / 三）

```
业务员问政策（项目二）
  POST /product/qa → ProductAssistantService.answer_question
  src/product_assistant/service.py::answer_question
    ├─ 混合检索（BM25 + 向量 → RRF → Rerank → 生效日期过滤）
    ├─ 相关性低于阈值 → refused=True，明确拒答并说明理由（"没有依据就不回答"）
    └─ 有依据 → _synthesize_answer 组织回答，附 citations 引用溯源

学员练话术（项目三）
  POST /coaching/sessions → /reply → /finish → CoachingService
    ├─ roleplay.py：模拟客户基于画像，且会抵抗（不放水）
    ├─ judge.py：四维 rubric，违规承诺一票否决
    └─ qc.py：全量质检规则引擎，覆盖率 100%
       （/coaching/qc/alert 告警主管是高风险动作，走平台 HITL——demo ⑨）
```

### 1.3 三条支路共用的一段：网关调用链

**每一次 LLM 调用，都要走完这条链**（`src/gateway/gateway.py::LLMGateway::complete`）：

```
complete(messages, task=...)
  │
  ① trim_messages            裁剪：去重 / 工具输出摘要 / 历史分层压缩
  │                          src/gateway/trim.py::trim_messages
  ② ComplexityRouter.route   复杂度打分 → small / medium / large 档
  │                          src/gateway/router.py::ComplexityRouter
  ③ CacheLayer.lookup        按 task 查缓存策略（_TASK_CACHE_DEFAULT）：
  │                          policy_qa / compliance / eval = off（答案错了就是事故）
  │                          roleplay / material = semantic（语言近似可复用）
  │                          src/gateway/cache.py::CacheLayer
  │    命中 → 成本记 0、baseline 照记（saved 才是真实节省）→ 直接返回
  │
  ④ RateLimiter.allow        限流（按模型隔离）── src/gateway/ratelimit.py::RateLimiter
  ⑤ QuotaManager.consume     配额超限 → 先降级 small 保服务；
  │                          已在最小档 → fail-closed 抛错，绝不无上限烧钱
  ⑥ _call_with_fallback      按降级链依次尝试，只往下退；
  │                          small 档没有后备，要升档必须显式指定
  ⑦ cache.store              写缓存（按策略）
  ⑧ _record_cost             成本台账：本次成本 + 不治理的基线成本 + saved
                             src/gateway/observe.py::GatewayObserver
```

### 1.4 Agent 支路（项目五，对话流之上的编排）

当一条请求不是"直接调 Service"，而是"让 Agent 自己决定调哪些工具"时，走编排图
（`src/agent_platform/runtime.py::AgentRuntime::invoke`）：

```
invoke(agent, user_input, thread_id)
  │
  route   意图路由（nodes.py::make_route_node）
  plan    任务规划 + **本轮工具作用域裁剪**（nodes.py::make_plan_node）
  execute ReAct 循环（nodes.py::make_execute_node），每一步：
  │        ├─ 预算守卫：step/token 超限 → flow=escalate 熔断转人工
  │        ├─ caps.decide：模型决策下一步调哪个工具
  │        ├─ HITL：工具 risk_level=high → interrupt() 挂起等人工
  │        │        （approval_key = thread:step:tool，确定性 ID，重放不重复建单）
  │        │        恢复重放时采用已有结论，绝不二次挂起
  │        └─ tools.invoke（带重试策略）→ observation 写回 state
  critique 产出评审，不通过回退重做（最多 max_critique_rounds 轮）
  respond  最终应答 + 终态判定（escalated/failed 不被"应答成功"覆盖）
```

**跟着跑**：

```bash
python scripts/demo.py        # 一次看全：
                               # ② 产品问答与拒答  ⑨ HITL 告警闭环  ⑩ 决策轨迹
                               # ⑰⑱ 网关三板斧 + 成本台账 + 会话回放
                               # ⑳–㉓ 语音：凭证/配置/文本模拟一轮/内容安全/评测
```

---

## 二、业务流程流（覆盖项目八，全系统最重要的主线）

线索从录入到成交的每一步，都是**同一套五步套路**。用种子里的真实线索（走到 `invited` 的那条）跟着走一遍，胜过读十遍图。

### 2.1 全流程图

```
投流留资 / 人工录入
  │  POST /flow/leads（幂等）—— src/flow_ops/api.py
  ▼
┌───────────── 每个写操作都走这五步（workflow.py 文件头）─────────────┐
│ ① 幂等键 make_idempotency_key（业务语义，非随机 UUID）              │
│ ② 分布式锁 LockGuard.hold（拿不到快速失败 lock_busy，不排队）        │
│ ③ 状态机校验 validate_transition（跳阶段直接拒绝）                  │
│ ④ 执行业务动作（+ CRM 同步，失败只告警不回滚）                       │
│ ⑤ 写审计 AuditLogger.log（ok / blocked / failed / idempotent_hit）  │
└──────────────────────────────────────────────────────────────────┘
  │  src/flow_ops/workflow.py::FlowEngine
  │  src/flow_ops/idempotency.py::make_idempotency_key / ::LockGuard
  │  src/flow_ops/leads.py::validate_transition / ::ALLOWED_TRANSITIONS
  │  src/flow_ops/audit.py::AuditLogger
  ▼
new → assigned → first_called → wechat_added → nurturing → invited
     → arrived → proposal_ready → fee_estimated → signed
                    ↘ lost（任一非终态可流失，终态冻结）
  │  阶段顺序与"已经到更后阶段"判断：src/flow_ops/workflow.py::_STAGE_ORDER
  ▼
【定时兜底】SopScheduler 常驻扫描（lifespan 启动，装配层只建不启）
  │  src/flow_ops/scheduler.py::SopScheduler::sweep_once
  │  ├─ asyncio.to_thread(check_sop)：同步扫描不占事件循环
  │  ├─ 抢 flow:sop:sweep 短租约：多副本同一时刻只有一个实例在扫
  │  └─ check_sop → workflow.py::FlowEngine::sweep_sop
  │        ├─ build_sop_rules + overdue_minutes（判据是确定性代码）
  │        ├─ 2×SLA 宽限期：已有预警不重复轰炸（find_recent_alert）
  │        ├─ insert_alert + audit.log(sop.alert)
  │        └─ notifier 通知主管（Webhook 失败只记日志，绝不拖垮扫描）
  │              src/flow_ops/notifier.py::build_notifier
  ▼
到店（confirm_arrival，幂等）→ human_stage=RECEPTION（Agent 禁区开始）
  │
  ▼
到店前一页纸 ReadinessBuilder.build（档案 + 方案初稿 + 费用预估）
  │  src/flow_ops/readiness.py::ReadinessBuilder
  │  ├─ 方案初稿：plan_provider 注入（项目二），不 import 业务包
  │  └─ 费用预估：fees.py::FeeEstimator（等额本息，全程 Decimal）
  │  → 自动推进 arrived → proposal_ready（"准备"语义，不是成交动作）
  ▼
成交类动作（send_contract / confirm_fee）—— 必须人批
  │  request_high_risk → ApprovalGate.request（pending）
  │      src/flow_ops/approval.py::ApprovalGate
  │  decide_approval  → 主管批准 / 驳回 / 过期（过期只能作废，绝不自动放行）
  │  execute_approved → assert_approved() 先行 → CRM 发送 → mark_executed → 审计
  │      src/flow_ops/workflow.py::FlowEngine::execute_approved
  │      ⚠️ 顺序是硬约束：先 assert 再动 CRM。反了会出现
  │         "状态已执行但通知没发出去"——审批单会说谎
  ▼
客户随时自助查进度（脱敏）
     POST /flow/progress → ProgressService.query
     src/flow_ops/progress.py::ProgressService
     双重身份校验（lead_id + 手机号后 4 位）+ 服务端脱敏
     （姓名留首字、无征信明细、new 阶段不对外）
```

### 2.2 三个必须记住的点

1. **HTTP 请求和定时任务走同一套逻辑**：`POST /flow/sop/sweep` 手动扫 与 `SopScheduler` 自动扫，最终都到 `FlowEngine::sweep_sop`。所以"客户连点三次"和"定时器跑三遍"结果完全一致。
2. **幂等命中不是失败**：审计里 `idempotent_hit` 是**成功拦截**，且命中后**重新读库**返回当前状态——客户重复提交时想知道"我的线索到哪了"，不是"你第一次收到时它是新的"。
3. **人工节点 Agent 只打标记**：`request_handoff` 只写 `human_stage`，绝不代替人做节点内动作（`workflow.py::FlowEngine::request_handoff`）。

**跟着跑**：

```bash
python scripts/demo.py        # ㉔ SOP 扫描  ㉕ 到店准备  ㉖ HITL 发合同  ㉗ 评测准入

# 或者自己走一遍 HTTP 全链路（先起服务 + 灌种子）：
curl -X POST localhost:8000/flow/leads -H 'Content-Type: application/json' -d '{...}'   # 录入
curl -X POST localhost:8000/flow/leads/{lead_id}/advance -d '{"to_stage":"first_called"}' # 推进
curl localhost:8000/flow/sop/alerts                                                   # 看预警
# 交互式调试：http://localhost:8000/docs（Swagger UI，静态资源已本地化）
```

---

## 三、数据中台流（覆盖项目四：AI 用的数据从哪来、脏数据怎么被挡住）

这条流解释一件事：**其他项目的语料与知识，是怎么经过"人审"才进来的。**

```
微信记录 / JSONL 语料
  │  src/data_platform/source.py::WeChatSqliteSource（只读打开）/ ::JsonlSource
  │  src/data_platform/source.py::load_sessions（wxid→姓名、群消息发送者还原）
  ▼
DataPlatformService.ingest ─── src/data_platform/service.py::ingest
  │
  ├─ ConversationBuilder.build：5 分钟窗切块 + 同角色合并 + 跨块去重
  │     src/data_platform/chunker.py::ConversationBuilder
  ├─ ConversationFilter：9 级分类 + 质量分（全规则，不靠模型）
  │     src/data_platform/filters.py::ConversationFilter
  │     噪声直接丢；keep（normal/high_value/sensitive）才进暂存区
  ├─ MaskingEngine：三层幂等脱敏（身份证先于银行卡的优先级 + verify()）
  │     src/data_platform/masking.py::MaskingEngine
  │     全角→半角归一化只作用于匹配层，1:1 等长映射保证幂等不被破坏
  ├─ 所有块全落库（含被丢弃的）：漏斗与丢弃率要能现算，
  │     也留住"为什么这条被丢"的证据（category_reason）
  └─ ReviewGate.submit → 暂存区（pending）
        src/data_platform/staging.py::ReviewGate
  ▼
人工审核（唯一入口，三条边）── src/data_platform/service.py::review
  │  approve / reject / edit
  │  ⚠️ edit 即重置并重跑脱敏——改了内容不重跑脱敏 = 脱敏白做
  │  ⚠️ 工具层 data.review 是 risk_level=high：批准即决定"哪些数据能进训练集"
  ▼
导出（只导 approved 且 dataset_eligible）── src/data_platform/exporter.py::DatasetExporter
  │  sharegpt / alpaca / openai / rag_csv 四格式
  │  导出前再脱敏一次 + verify() 断言零残留，否则整批拒绝
  │  导出 key = 内容哈希：同输入同输出，幂等复用已有产物
  ▼
知识构建 ── src/data_platform/knowledge.py::KnowledgeBuilder
  │  默认增量（按 build_watermarks 上次水位，只处理新批准条目）
  │  LLM 只做知识提炼，失败规则兜底并标 extractor 来源
  ▼
供给下游 ── src/data_platform/service.py::feed_conversations
     只给 approved 且 dataset_eligible：
       敏感对话（投诉/维权）可审但永不进训练集——
       把客户投诉包装成营销案例是事故，不是瑕疵
```

**关键分离**：`keep`（敏感可审）与 `dataset_eligible`（可进训练集）是两个字段，必须在存储层就分开——这是"中台可信"的地基（`src/data_platform/domain.py` 的四层模型里定义）。

**跟着跑**：

```bash
python scripts/demo.py        # ㉘ 一段看全：漏斗 → 9 级分类分布 → 人工审批 → 导出 → 知识 → 检索
```

---

## 四、素材与激活流（覆盖项目七）

```
结构模板（blueprints.json：钩子→痛点→方案→CTA）
  │  src/marketing/generator.py::MaterialGenerator::generate
  ▼
_build_one：_from_llm（真实模型）/ _from_template（离线模板填充）
  │  离线也走模板而不是让 echo 自由发挥：
  │  素材是对外的，宁可稳定可控，不能编造事实
  ▼
assemble() ─── 素材唯一出口，先审后发无法绕过
  │  src/marketing/generator.py::MaterialGenerator::assemble
  ├─ ③ ComplianceEngine.check_and_fix：规则保召回 + LLM 保精度
  │     src/marketing/compliance.py::ComplianceEngine
  │     能自动改的（承诺性/绝对化用语）改写留痕；改不动的（手机号/证件号）直接拦截
  ├─ ④ QualityScorer.score：渠道语感合规性 + 质量分
  │     src/marketing/quality.py::QualityScorer
  └─ 定状态：approved / sanitized / rejected
  ▼
成单案例包装（只有成交对话能入库）
  │  src/marketing/cases.py::CaseBuilder::build_from_conversation
  │  语料来自数据中台 feed_conversations（已审 + eligible）
  ▼
未成单客户激活（对症匹配 + 四道防骚扰闸门）
  │  src/marketing/activation.py::ActivationEngine::plan
  │  沉睡阈值 / 静默期 / 触达上限 / 避开深夜周末
  │  触达计数来自真实流水，不是离线快照
  ▼
素材发给客户（高风险 + 非幂等 → 平台 HITL）
  │  Agent 路由：src/marketing/agent.py::build_marketing_agent
  │  → 工具 platform.send_external_message（risk_level=high, idempotent=False）
  │     src/agent_platform/tools/builtin.py
  │  → execute 节点 interrupt() 挂起 → 审批中心待办 → 人工批准 → resume
  │  → 超时策略只有 abort / escalate，绝不自动放行
  ▼
效果归因（最朴素的漏斗，没有数据就不编结论）
     src/marketing/attribution.py::AttributionEngine::report
     impression → consult → lead → deal，按渠道 × 素材聚合
```

**跟着跑**：

```bash
python scripts/demo.py        # ⑪ 渠道语感  ⑫ 先审后发（含隐私拦截） ⑬ 案例脱敏
                               # ⑭ 激活防骚扰  ⑮ 归因  ⑯ HITL 发送闭环
```

---

## 五、四条流的交汇点

| 交汇点 | 谁流向谁 | 机制（不是约定，是代码） |
|---|---|---|
| 数据中台 → 素材（项目七） | 已审对话 → 案例语料 | `feed_conversations()` + 装配层 `_make_conversation_provider` 注入（`src/app_context.py`） |
| 数据中台 → 知识（项目二/三） | 导出 RAG CSV / JSONL → 知识库与题库上游 | `DatasetExporter` 产物 |
| 产品助手 → 流程（项目八） | 方案初稿 → 到店一页纸 | `ReadinessBuilder(plan_provider=...)` 装配层注入 |
| 语音/陪练 → 网关 | 所有 LLM 调用 | `LLMGateway` 实现 `LLMProvider`，业务看到的就是"一个模型" |
| 流程/素材 → 平台 | 高风险动作 | `interrupt()` + `ApprovalGate`，HITL 是编排层能力不是业务自觉 |
| 一切写操作 → 审计 | 四态留痕 | `AuditLogger` / `audit_log` 表 |

**读法建议**：业务流程流是心脏，数据中台流是上游，AI 对话流是门面，素材激活流是增长腿。
新人追数据流，就从"一条线索"开始追（流二），遇到方案/档案岔路去流一、流三。

---

## 六、这份文档怎么保持不过期

1. 所有代码位置都是 `文件::符号` 格式，跑 `python scripts/onboarding_check.py --map docs/11-data-flow.md` 可验证；符号被改名/删除会立刻报错。
2. "跟着跑"的段号对应 `scripts/demo.py` 的 `hr("① …")` 编号，demo 改结构时同步更新段号。
3. 新增模块/端点时，先想清楚它属于哪条流、在哪个总闸上插业务，再写代码——写不明白通常说明边界设计有问题。
