# 项目八：助贷业务流程数字员工

> 对应简历项目八 ｜ MCP 工具化 / 流程状态机 / HITL / 幂等可靠性 ｜ `2025.06 – 2026.09`
>
> **一句话定位**：把「咨询 → 邀约 → 方案生成 → 费用评估」自动化，
> **接待、谈判、费用确认保留人工** —— Agent 只做准备与建议，不做承诺与成交。

---

## 一、业务前提：为什么"提醒工具"不够

客户从线索到签约要经过一串**内部流程动作**：分配 → 首呼 → 加微 → 跟进 → 邀约 →
到店 → 方案准备 → 费用评估。这些动作时效性强、步骤多、**靠人盯**：

| 痛点 | 业务代价 |
|---|---|
| 漏跟进（首呼忘做、加微没加，靠自觉） | 漏跟进率 15%，线索直接浪费 |
| 到店前临时攒资料、手工做方案 | 准备 40 分钟，客户到店干等 |
| 服务费率 / 放款成本靠手算 | 效率低 + 口径易错 |
| 客户反复打电话问进度 | 业务员大量时间耗在重复答疑 |
| 谁在什么时候做了什么说不清 | 出问题难追责 |

传统 ERP/工作流只能"提醒"，不能"准备"——它不会在客户到店前自动汇总档案、
预生成方案、算好费用。**Agent + MCP 工具化对接真实系统，才能让流程从
"提醒人做"升级为"替人做准备"**；同时 HITL 把高风险动作留在人手里。

---

## 二、整体架构

```
┌──────────────────────────────────────────────────────────────┐
│ Agent 流程状态机（FlowEngine，LangGraph 之外的本项目编排）      │
│   new → assigned → first_called → wechat_added → nurturing    │
│        → invited → arrived → proposal_ready → fee_estimated   │
│        → signed                                              │
│   ├── SOP 漏做检测（时限规则表）→ 预警主管                     │
│   └── 高风险动作 → ApprovalGate（HITL）                       │
└───────┬──────────────────────────────┬───────────────────────┘
        ▼                              ▼
┌───────────────────────┐    ┌──────────────────────────────┐
│ ★ 到店前自动准备        │    │ ★ 人机边界                    │
│  汇总客户档案           │    │  人工节点：接待/谈判/费用确认   │
│  预生成方案初稿(项目二)  │    │  高风险动作人工审批后执行       │
│  费用预估(确定性公式)    │    │  全程审计日志可追溯            │
└───────┬───────────────┘    └──────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────┐
│ ★ MCP 工具层：CRM / 企业微信 / 进件系统（OpenAPI → ToolSpec）   │
└───────┬──────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────┐
│ ★ 可靠性：幂等键 + 分布式锁（SQLite 实现，接口同 Redis）        │
│           自助查进度（企微/小程序，脱敏视图）                   │
│           监控指标（漏跟进率 / 准备时长 / 幂等拦截 / HITL）      │
└──────────────────────────────────────────────────────────────┘
```

**与项目五的关系**：本项目把自己注册成平台上的第 6 个 Agent（`flow_ops`），
编排、状态持久化、HITL 检查点、可观测全部复用平台能力。
高风险工具**双重保护**：平台层 risk=high 自动挂起 + 本服务 ApprovalGate
（后者覆盖定时任务等不经过 Agent 的路径）。

---

## 三、模块清单

`src/flow_ops/` 18 个文件 ≈ 5.0k 行（含注释）：

| 文件 | 职责 |
|---|---|
| `domain.py` | 领域模型：Lead / StageTransition / Appointment / SopRule / ReadinessPack / FeeEstimate / ApprovalRequest / AuditEntry / ProgressSnapshot / EvalCase |
| `leads.py` | **状态机**（流转表 + 合法性校验）+ **SOP 规则表**（从配置构造）+ 逾期计算 |
| `workflow.py` | **FlowEngine**：数字员工本体，每个写操作的固定套路 = 幂等键 → 锁 → 状态机 → 审计 |
| `store.py` | SQLite 八张表：leads / transitions / appointments / sop_alerts / approvals / audit_log / idempotency_keys / locks |
| `idempotency.py` | 幂等键（业务语义生成）+ 分布式锁（owner 校验 + TTL） |
| `approval.py` | **ApprovalGate**：审批生命周期，`assert_approved` 是执行前唯一通行证 |
| `fees.py` | 费用试算：等额本息 + 分档服务费率（确定性代码，可单测可复现） |
| `readiness.py` | 到店前准备包：档案亮点/风险 + 方案初稿（provider 注入）+ 费用预估 + 谈资 |
| `progress.py` | 自助查询：手机号后 4 位校验 + 脱敏视图 + 内部节点过滤 |
| `audit.py` | 审计留痕：谁/何时/对什么/做了什么/结果/关联审批 |
| `crm.py` | CRM/企微 OpenAPI 抽象（Protocol + Recording 离线实现） |
| `notifier.py` | 通知渠道：LogNotifier（默认）/ WebhookNotifier（企微/钉钉 markdown），失败只记日志 |
| `scheduler.py` | **SopScheduler**：常驻扫描循环（asyncio + to_thread），单轮失败自愈 |
| `metrics.py` | 漏跟进率 / 阶段分布 / 幂等拦截 / HITL 待审 / 准备时长 p95 |
| `evaluation.py` | 流程行为 Golden Set（11 条）+ CI 准入三条拒发理由 |
| `service.py` | 门面：六组能力 + 14 个工具注册 + 评测 |
| `agent.py` | 平台 Agent：8 条路由 + 细粒度作用域裁剪 |
| `api.py` | HTTP 接口（`/flow/*`） |

---

## 四、核心实现逐条

### ① 流程自动化：状态机 + SOP 兜底

状态机的流转表在 `leads.py`，**不允许跳阶段**——每一步 SOP 都是留痕点：

```python
ALLOWED_TRANSITIONS = {
    LeadStage.NEW:       frozenset({ASSIGNED, LOST}),
    LeadStage.ASSIGNED:  frozenset({FIRST_CALLED, LOST}),
    ...
    LeadStage.SIGNED:    frozenset(),   # 终态不可再流转，历史不允许被改写
}
```

非法流转在**代码层**拒绝（不是 UI 上不给你按钮——接口层永远有人直接调）。
SOP 判据是纯确定性计算：

```
逾期分钟数 = max((now - stage_entered_at) / 60 - sla_minutes, 0)
```

`leads.py::build_sop_rules()` 从配置构造规则表（首呼 30 分钟、加微 2 小时、
跟进 24 小时、到店确认 4 小时），换运营策略不用改代码。

**防预警疲劳**：同一线索同一动作在 2×SLA 内只报一次。
未认领的靠"还在主管待办列表里"持续提醒；认领的给处理时间。
否则主管认领越多被轰炸越频繁，很快就学会忽略预警——预警就白做了。

### ② 到店前自动准备：40 分钟 → 5 分钟

`ReadinessBuilder.build()` 生成"一页纸"，三个组成部分各有来源：

| 组成 | 来源 | 说明 |
|---|---|---|
| 档案亮点/风险 | 本项目 ProfileSummary | 资质六项（公积金/社保/房/车/保单/执照）+ 征信四项（逾期/查询/负债/网贷） |
| 方案初稿 | **项目二**（provider 注入） | 本项目不重新实现匹配，只负责"在对的时机把它准备好" |
| 费用预估 | 本项目 `fees.py` | 等额本息 + 分档服务费，确定性公式 |

**对项目二是调用关系，不是重新实现**（面试问边界就这么答）。
provider 异常时降级为"转人工"并写进 warnings——准备流程不能因为
下游挂了就整个崩掉。

### ③ 人机边界：ApprovalGate

三条铁律，全部有测试守着：

1. **没有 approved 记录就没有执行**。`assert_approved()` 是执行前唯一通行证，
   pending / rejected / expired 一律拒绝；
2. **过期只能 expired，绝不自动批准**。"没人批"的正确结果是"这事没发生"；
3. **人工节点是 Agent 的禁区**。`reception / negotiation / fee_confirmation`
   三个阶段 Agent 只能"标记待人工 + 准备材料"。

平台层的 HITL（Agent 编排路径）+ 本服务的 ApprovalGate（定时任务/事件路径）
双重覆盖——因为 SOP 扫描、到店提醒这些动作根本不经过 Agent。

### ④ 可靠性：幂等 + 锁 + 自助查询

**幂等**：每个写操作的键按业务语义生成（`lead_id + action + 关键参数`），
参数先排序再哈希，换个传参顺序绕不过去。命中缓存时**重新读库返回当前状态**——
客户重复提交留资表单时想知道"我的线索到哪了"，不是"你第一次提交时它是新的"。

**锁**：SQLite 实现，接口与 Redis 版一致（`hold` / `release`）。
owner 校验（A 不能释放 B 的锁）+ TTL（持锁者崩了不死锁）。
快速失败而非阻塞等待：写操作都是几十毫秒的短临界区，
"让调用方带同一个幂等键重试"比"抱着锁等"更可控。

**自助查询**：线索 ID + 手机号后 4 位双重定位；只返回阶段与时间线，
不给身份证、完整手机号、征信明细；`new`（未分配）不进客户时间线——
客户不需要知道内部待办。身份校验失败**不说哪一位错了**（防枚举）。

### ⑤ 常驻调度与主动通知：让预警"找到人"

预警只落库，等于没预警 —— 漏跟进的平均发现时间是"客户问起来的时候"。
两个模块把"被动查询"变成"主动触达"：

**SopScheduler（`scheduler.py`）**——常驻扫描，随 FastAPI lifespan 启停：

```
lifespan 启动 → build_scheduler(settings)（FLOW_SCHEDULER_ENABLED=false 时返回 None）
            → start() → [立即扫一轮] → sleep(interval) → [扫一轮] → …
lifespan 关闭 → stop()（cancel + 等待当前轮退出）
```

四条约束：① sweep 是同步的，用 `asyncio.to_thread` 丢线程池，**不阻塞事件循环**；
② 单轮扫描抛异常只记日志返回空，**下一轮照常**（定时任务炸一次就静默死掉最危险）；
③ 多副本同跑安全（sweep 幂等 + 2×SLA 宽限期，不会重复预警/重复通知）；
④ 默认关闭，生产/compose 显式打开（`FLOW_SCHEDULER_ENABLED=true`，默认 5 分钟一轮）。

**Notifier（`notifier.py`）**——Protocol + 两个实现，按配置选路：

| 渠道 | 何时用 | 行为 |
|---|---|---|
| `LogNotifier` | 默认（不配 webhook） | 写结构化日志，零外部依赖 |
| `WebhookNotifier` | 配了 `FLOW_NOTIFY_WEBHOOK_URL` | 企微/钉钉机器人 markdown；3s 超时；**失败绝不抛** |

三个通知点：SOP 扫描出新预警（**一轮一个 digest**，不逐条轰炸）、
高风险审批建单（**即通知**，审批时效直接决定动作能不能当天完成）、
启动时清理过期审批（这些事"没人批"，需要人重新发起）。
接飞书/短信只需实现同一个 `notify`，`FlowOpsService` 一行不用改。

---

## 五、费用估算口径

```
月供    = P × r × (1+r)^n / ((1+r)^n - 1)      r = 年化/12，P = 放款金额，n = 期数
服务费  = 放款金额 × 服务费率（分档：< 30 万 3%，≥ 30 万 2%）
到手金额 = 放款金额 - 服务费
```

**为什么用确定性代码而不是让模型算**：费用是钱。模型算错一位小数就是事故，
而且错了查不出来（同输入两次可能两个结果）。封闭公式代码算 3 微秒出结果、
可单测、可复现。零利率时退化为 `P / n`（银行贴息活动也能试算）。

**免责声明是模型字段的一部分**，不是可选 UI 文案——任何对外发送的预估必须带口径。

> ⚠️ **上线前必须与财务核对**：`flow_service_fee_rate` / `flow_fee_rate_large_loan` /
> `flow_default_annual_rate` 当前是行业常见区间的默认值（3% / 2% / 7.2%），
> 属【待填】项，未与公司真实费率核对前不得对外。

---

## 六、评测与准入

`evaluation.py` 的 11 条 Golden Set，**全部是布尔判定**（流程系统的正确性
没有"部分正确"）：

| 用例 | 锁定的故障模式 |
|---|---|
| `flow_sop_overdue` / `flow_sop_on_time` | 该告警没告警 = 漏跟进；不该告警乱告 = 预警疲劳 |
| `flow_illegal_transition` | 跳阶段 = SOP 可被绕过 |
| `flow_idempotent_intake` / `flow_invite_idempotent` | 重复提交产生重复数据 |
| `flow_fee_formula` / `flow_fee_tier` | 钱算错 / 费率档位口径漂移 |
| `flow_hitl_blocks` / `flow_hitl_executes_after_approval` | 没批就执行 / 批了也执行不了 |
| `flow_progress_desensitized` | 自助查询泄露隐私 |
| `flow_human_stage_forbidden` | Agent 在谈判节点替人做准备 |

准入三条拒发理由（CI 里跑，`POST /flow/eval/gate`）：
① 通过率相对基线退化超阈值；② 任一基线通过的用例本次失败；
③ 出现基线里没有的新失败用例（防止删用例凑通过率）。

**基线必须先取再跑**——反过来的话"基线"就是自己，gate 永远通过。

---

## 七、HTTP 接口

前缀 `/flow`（23 个端点，`/docs` 可交互调试）：

```
POST /flow/leads                     录入线索（Idempotency-Key 头）
GET  /flow/leads                     线索列表（stage / owner 过滤）
GET  /flow/leads/{id}                详情（含流转与邀约）
POST /flow/leads/{id}/advance        推进阶段（人工操作）
POST /flow/leads/{id}/invite         邀约到店（幂等）
POST /flow/leads/{id}/arrived        到店确认（幂等）
POST /flow/leads/{id}/readiness      到店前准备（一页纸）
POST /flow/leads/{id}/fee            费用预估
POST /flow/leads/{id}/handoff        转人工节点
GET  /flow/sop/alerts                SOP 预警（主管待办）
POST /flow/sop/sweep                 手动触发扫描
POST /flow/sop/alerts/{id}/ack       认领预警
GET  /flow/approvals                 审批列表
POST /flow/leads/{id}/request-contract  申请发合同（HITL）
POST /flow/leads/{id}/confirm-fee       申请确认费用（HITL）
POST /flow/approvals/{id}/decide     审批决策
POST /flow/approvals/{id}/execute    执行已审批动作
POST /flow/progress                  客户自助查进度（脱敏）
GET  /flow/audit                     审计留痕
GET  /flow/metrics                   流程指标
GET  /flow/stats                     服务统计
POST /flow/eval                      Golden Set
POST /flow/eval/gate                 准入门禁
```

---

## 八、与其他项目的咬合

| 本项目用到 | 来自 | 关系 |
|---|---|---|
| 方案初稿 | 项目二 product_assistant | 调用（provider 注入），项目二不知道本项目存在 |
| 客户档案 | 项目二客户档案 | 数据（装配层适配成 ProfileSummary） |
| 编排 / HITL / 观测 | 项目五 agent_platform | 复用，本项目只写能力函数 |
| 成本记账 | 项目六 gateway | 框架调用走网关（本项目判据是确定性代码，极少调模型） |
| 客户对话历史 | 项目一 voice_service | 可注入（`conversation_provider` 同款模式） |

---

## 九、生产部署要点

1. **CRM / 企微对接**：把 `RecordingCrmClient` 换成真实 OpenAPI 实现
   （`crm.py` 的 Protocol 就是契约），流程代码一行不改。
   建议先写本地再同步上游（outbox 模式），外部系统不可用是常态不是异常。
2. **多 worker 必须换 Redis 锁 + PG 存根**（副本数 > 1 时是硬要求，不是优化项）：
   - `FLOW_LOCK_BACKEND=redis` + `REDIS_URL=...`：SQLite 锁只在本进程内互斥，
     多副本下两条请求同时改同一条线索会**双双拿到锁**。Redis 版三件套：
     `SET NX PX`（互斥+租约）/ Lua 比 owner 再 DEL（原子释放）/ Lua 比 owner 再
     PEXPIRE（续约）。
   - `CHECKPOINTER_BACKEND=postgres` + `POSTGRES_DSN=...`：HITL 审批在副本 A 挂起、
     resume 落到副本 B 时，sqlite 存根取不到状态 —— "审批通过了但流程没继续"。
   - **两者都不静默降级**：Redis 挂了 → 503「未做任何修改，重试安全」；
     PG 配错 → 启动失败并给排查指引。
   - 验收：`GET /flow/stats` 的 `lock_backend` 应为 `RedisLockBackend`。
3. **定时扫描**：`FLOW_SCHEDULER_ENABLED=true` 起内置调度器（默认 5 分钟一轮，
   随 lifespan 启停）；不想起常驻进程也可以关掉它，改用 cron 打
   `POST /flow/sop/sweep`（幂等，多副本/ cron 重跑都安全）。
4. **通知渠道**：`FLOW_NOTIFY_WEBHOOK_URL` 填企微/钉钉机器人地址即升级为
   IM 通知；留空只写日志。**通知失败只记日志，绝不拖垮扫描**。
5. **口径核对**：费率与利率默认值必须与财务核对后才能对外（见第五节）。
6. **监控**：`GET /metrics`（Prometheus 0.0.4，无需 Key）直接抓，
   `flow_*` 族含各阶段线索数/幂等命中/锁等待/审批，`scheduler_*` 族含扫描轮次。
   多 worker 下还应盯"审批挂起→恢复"的跨副本延迟（PG 存根命中率）。

---

## 十、排障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `illegal_transition` | 跳阶段或终态流转 | 按状态机合法路径推进；确实要跳必须人工在 CRM 先补记录 |
| `lock_busy` | 同一线索并发操作 | 带同一个幂等键重试（结果一致，不会重复执行） |
| `lock_unavailable`（503） | Redis 连接失败（fail-closed） | 检查 Redis 可达性；本次未做任何修改，重试安全。**不会**降级成 SQLite 锁 |
| `approval_required` | 高风险动作未获批准/已过期 | 走审批流；过期重新申请（过期不自动批准） |
| `human_stage` | Agent 试图在人工节点执行 | 设计如此；让业务员处理，Agent 只准备材料 |
| 进度查询 403 | 手机号后 4 位不对 | 确认客户手机号；错误信息刻意不提示哪一位错 |
| 预警反复出现 | 超过 2×SLA 仍未处理 | 这是升级机制在工作，认领或推进线索 |
| 审批通过但流程没继续（多 worker） | Checkpointer 还是 sqlite/memory，resume 落到别的副本 | `CHECKPOINTER_BACKEND=postgres` 并配 DSN；启动日志会打印存根类型 |

---

## 十一、高频问答

**Q：为什么不全自动，要留人工？**
助贷强监管 + 成交依赖信任，承诺和费用必须由人确认；AI 越界承诺 = 合规事故。
数字员工的价值不是替人做所有事，而是把人从准备工作和重复答疑里解放出来，
让人专注在只有人能做的谈判和成交上。

**Q：幂等怎么实现的？**
每个写操作带业务幂等键（线索 ID + 动作 + 关键参数，排序后哈希），
SQLite/Redis 存储首次结果；并发重复用分布式锁挡（owner + TTL）。
命中缓存在审计里记 `idempotent_hit`，"拦了多少次重复"成为可观测数字。

**Q：SOP 漏跟进怎么检测？**
状态机每个节点有超时与前置依赖（规则表在 `leads.py`，时限显式可配），
未按时流转即生成预警给主管。判据是确定性代码，不靠模型判断——
可解释、可复现、可单测。

**Q：和项目二的边界？**
项目二负责"怎么配方案"，本项目负责"在到店前把方案准备好"，是调用关系。
provider 注入让两个服务不互相 import，项目二发版不影响本项目。

**Q：客户自助查询会不会暴露隐私？**
只展示进件进度状态，不展示敏感字段；双重身份校验（线索 ID + 手机号后 4 位）；
脱敏在服务端做（前端想多显示也拿不到数据）；校验失败不提示哪一位错（防枚举）。

**Q：SOP 预警做出来了，怎么保证有人看到？**
两层：**调度**让扫描不需要人触发（`SopScheduler` 常驻循环，`to_thread` 不阻塞
事件循环，单轮失败自愈，多副本同跑靠幂等 + 宽限期保证不重复）；
**通知**让预警主动找人（`Notifier` 协议 + Log/Webhook 两实现，预警 digest、
审批建单即通知）。设计上最关键的一条：**通知失败绝不拖垮业务流程** ——
它是"找人"的手段，不是流程的一部分，所以 webhook 挂了只记日志。
