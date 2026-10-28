# 自检与验收：2 天目标的验收依据

> **怎么用**：题目必须先做后翻答案（答案在折叠块里，先盖住自己答）。
> Day 1 末做前 12 题，**答对 10 题以上才进 Day 2**；Day 2 末做后 8 题 + 5 个练习，最后过验收单。
> 涉及"代码在哪"的题，答案用 `文件::符号` 写进一个 txt，然后：
>
> ```bash
> python scripts/onboarding_check.py --answer my_answers.txt
> ```
>
> 它会告诉你每个位置对不对、错了的话符号真正在哪——**这比 Mentor 人工翻代码快，也比"感觉对了"可靠**。

---

## 一、Day 1 自检题（业务与链路，12 题）

> 考点都在 Day 1 读过的内容里：README、`docs/10-code-map.md`、`docs/11-data-flow.md`、Web 工作台实操。
> 答不出来的回去重读对应小节，不要猜。

### Q1. 线索从 `invited` 能不能直接跳到 `signed`？在哪段代码被拦？

<details>
<summary>答案</summary>

不能。`src/flow_ops/leads.py::ALLOWED_TRANSITIONS` 是白名单流转表（`dict[LeadStage, frozenset[LeadStage]]`），`invited` 的合法目标里没有 `signed`；`validate_transition` 校验失败会抛 `FlowError`，错误信息列出"允许的目标"。要允许必须先经过 `arrived → proposal_ready → fee_estimated`——这不是麻烦，是**成交前的人工节点不能被跳过**。

</details>

### Q2. `stage_entered_at` 和 `created_at` 有什么区别？SOP 判据用哪个？

<details>
<summary>答案</summary>

`created_at` 是线索创建时间，一生不变；`stage_entered_at` 是**进入当前阶段**的时间，每次推进都刷新。SOP 漏跟进判据用 `stage_entered_at`（见 `overdue_minutes(stage_entered_at=...)`）——"进到 first_called 之后 30 分钟没人首呼"才算漏，用 `created_at` 会把老线索全部误判为逾期。

</details>

### Q3. 首呼 SLA 默认多少分钟？在哪个配置项改？

<details>
<summary>答案</summary>

30 分钟（加微 2 小时 / 跟进 24 小时）。改 `src/agent_platform/config.py` 的 `Settings::flow_first_call_sla_minutes` 等配置项（或 `.env` 的 `FLOW_SOP_*`），规则由 `src/flow_ops/leads.py::build_sop_rules` 从配置生成——**判据不在代码里写死，也不在前端**。

</details>

### Q4. 为什么房产抵押贷的额度**不能**乘月收入？相关常量叫什么？

<details>
<summary>答案</summary>

房产抵押的 `quota_multiple=0.7` 是**抵押率**（对房产评估价），不是收入倍数。漏了门控会算出"月收入 3 万 × 0.7 = 2.1 万"这种荒谬额度。门控常量是 `src/product_assistant/domain/matching.py::_INCOME_QUOTA_BASES`：只有基数落在"月收入/公积金基数/社保基数/个税/工资/打卡"这些词上才允许乘。

</details>

### Q5. 页面上显示 `rep_01`，想让新人看到"张晓东"，该改哪个文件？

<details>
<summary>答案</summary>

`web/src/hooks.ts` 的 `ownerLabel` 翻译表。**只有这一层做翻译**，页面不许自己 if-else 拼字符串——否则同一个 `rep_01` 会在三个页面出现三种译法（这是 Web 工作台两条前端纪律之一）。

</details>

### Q6. 审计四态是哪四个？`idempotent_hit` 算成功还是失败？

<details>
<summary>答案</summary>

`ok` / `blocked` / `failed` / `idempotent_hit`。`idempotent_hit` 是**成功拦截**——重复提交被幂等挡住、没有产生第二条记录，这是系统在正确工作，不是故障。审计落在 `src/flow_ops/store.py` 的 `audit_log` 表。

</details>

### Q7. 幂等键为什么不能是随机 UUID？正确的生成函数在哪？

<details>
<summary>答案</summary>

随机键每次请求都不同，"重复点击"和"网络重试"会生成两个不同键，两条都执行了——等于没防。正确函数是 `src/flow_ops/idempotency.py::make_idempotency_key`，按业务语义拼接（如 `intake:客户:渠道:负责人`），且**前后端必须同源**（`web/src/api.ts`）。

</details>

### Q8. 「到店前一页纸」由哪个类生成？它从项目二拿什么？

<details>
<summary>答案</summary>

`src/flow_ops/readiness.py::ReadinessBuilder`。从项目二拿**方案初稿**（通过装配层注入的 `plan_provider`，不是 import 业务包）；另有档案、费用预估（`fees.py::FeeEstimator`）和谈话要点。生成后自动推进 `arrived → proposal_ready`。

</details>

### Q9. 网关的缓存有哪两种？命中判据一样吗？

<details>
<summary>答案</summary>

前缀（精确）缓存与语义缓存，都在 `src/gateway/cache.py::CacheLayer`。判据完全不同：精确缓存要求 prompt 前缀一致；语义缓存按 embedding 相似度阈值（可 `SemanticCache.calibrate()` 探测校准）。且**按任务决定策略**：`policy_qa`/`compliance`/`eval` 一律 `off`——利率答错、合规漏判、评测被缓存污染，都是事故。

</details>

### Q10. `gateway` 包里为什么不能出现"线索"这个词？

<details>
<summary>答案</summary>

因为基础设施不许反向依赖业务（七条设计原则第 5 条）。网关只认识 message 和 token；它一旦知道什么是"线索"，就会慢慢长成"没人敢动的业务逻辑坟场"。这条边界有机器化测试守着（`test_gateway_does_not_import_business_packages`），违反会直接红。

</details>

### Q11. 数据中台的"审核"为什么必须人点，不能批量通过？

<details>
<summary>答案</summary>

因为审核闸门是这个中台存在的意义本身：批准 = 决定"哪些数据能进训练集"，而敏感对话（投诉/维权）即使批准也**永不进训练集**（`keep` 与 `dataset_eligible` 分离）。批量通过 = 没有闸门，脏数据/隐私直接进下游。三条边（approve/reject/edit）、编辑即重置重跑脱敏，见 `src/data_platform/staging.py::ReviewGate`。

</details>

### Q12. 前端页面里能不能写"如果阶段是 invited 就显示倒计时"这种判断？为什么？

<details>
<summary>答案</summary>

不能。判据一旦进了前端就有两份实现：后端改了 SOP，前端还按老规则显示，业务员看到的是错的。正确位置是后端 `src/flow_ops/leads.py::build_sop_rules` 算好，前端只渲染（`src/flow_ops/workflow.py::_STAGE_ORDER` 给顺序）。前端唯一的自由是"怎么显示得好看"。

</details>

---

## 二、Day 2 自检题（源码级，8 题）

> 每题都对应 10-code-map 里的一行。允许用 `--probe`，但**先自己说，再验证**。

### Q13. `execute_approved` 里 CRM 调用和 `mark_executed` 的顺序是什么？反了会出什么事故？

<details>
<summary>答案</summary>

顺序是 `assert_approved()` → CRM 发送 → `mark_executed()` → 审计。反了会出现"状态显示已执行，但合同其实没发出去"——审批单和审计会说谎，且客户侧毫不知情。代码：`src/flow_ops/workflow.py::FlowEngine::execute_approved`，注释里明确写了这是硬约束。

</details>

### Q14. Redis 锁连不上时，系统返回什么？为什么不能降级用 SQLite？

<details>
<summary>答案</summary>

返回 503「未做任何修改，重试安全」（fail-closed）。多 worker 下降级成 SQLite 锁，两个副本会**同时拿到同一把锁**，重复发通知、重复推进状态——跨进程互斥已经失效，还给人"锁在工作"的错觉，比明确失败危险得多。见 `src/flow_ops/locking.py`。

</details>

### Q15. 语音一轮里，客户说"资料你帮我包装一下"会发生什么？哪段代码决定的？

<details>
<summary>答案</summary>

整轮被拒绝并转人工：`Censor.check_user_text` 判定"要求造假"类高风险诉求，直接返回兜底话术 + `refused=True` + `needs_human=True`，**不调模型、不检索**。见 `src/voice_service/dialog.py::stream_turn` 的第 ① 步。

</details>

### Q16. 工具 `data.review` 为什么 `risk_level=high`？批准一条敏感对话后，它能进训练集吗？

<details>
<summary>答案</summary>

因为批准即决定"哪些数据可用于训练"，是对数据资产的高风险动作，所以必须过 HITL（`src/data_platform/service.py` 的 `register_tools`）。**不能**：敏感类（sensitive）条目即使 approved，`dataset_eligible` 仍为 False，导出与 `feed_conversations` 都会排除它。

</details>

### Q17. 一条素材从生成到下发，经过哪几个强制闸门？哪个是唯一出口？

<details>
<summary>答案</summary>

`assemble()` 是唯一出口（`src/marketing/generator.py::MaterialGenerator::assemble`），内部固定两闸：`ComplianceEngine.check_and_fix`（先审后发，改不动的拦截）+ `QualityScorer.score`。到"发给客户"还有第三道：平台 `platform.send_external_message` 是 high risk + 非幂等，execute 节点自动 `interrupt()` 等人工批准。

</details>

### Q18. SOP 调度器多副本部署时，同一时刻几个实例在扫？靠什么保证？

<details>
<summary>答案</summary>

只有一个。每轮扫描前先抢 `flow:sop:sweep` 短租约（`src/flow_ops/scheduler.py::SopScheduler::sweep_once`），抢不到就跳过本轮。加上 sweep 本身幂等 + 2×SLA 宽限期，双保险保证主管不会被轰炸。

</details>

### Q19. 网关的 `_TASK_CACHE_DEFAULT` 里哪些任务是 `cache=off`？为什么评测（eval）也必须 off？

<details>
<summary>答案</summary>

`policy_qa`（利率/额度）、`compliance`（合规审核）、`eval`（评测）三类。评测 off 是因为：**缓存命中会让"本次评测"变成"上次的答案"**，基线对比失真，CI 准入门禁被悄悄击穿——gate 永远通过比 gate 永远失败更危险。

</details>

### Q20. 对一个已批准的 HITL 任务调 `resume()`，execute 节点会再次 `interrupt()` 吗？为什么？

<details>
<summary>答案</summary>

不会。恢复重放时该节点发现审批单已有结论（approved），直接采用、不再挂起（`src/agent_platform/graph/nodes.py::make_execute_node` 的分支）。否则每次 resume 都重新 interrupt 同一个动作，形成"批了也执行不了"的死循环。前提是 `approval_key` 按 `thread:step:tool` 确定性生成——用随机 ID 就会建出重复待办。

</details>

---

## 三、5 个递进练习（难度递增，都有验证方式）

| # | 练习 | 涉及层 | 验证方式 |
|---|---|---|---|
| 1 | 给状态机加一条流转规则（如允许 `nurturing → invited` 重复邀约） | 领域层 | `pytest tests/test_flow_ops.py -k transition` 全绿，且你补了新用例 |
| 2 | 调整一个 SOP 阈值，重灌种子，观察预警条数变化 | 服务层 + 配置 | 改 `config.py` 的 SLA 配置 → `python scripts/seed_data.py` → `python scripts/demo.py` 的 ㉔ 段，预警数量符合预期，165/25 分钟两条仍在 |
| 3 | 给某个包加一个只读工具并注册 | 平台层 | `/ready` 返回的工具数 +1（demo ① 段也能看到） |
| 4 | 加一个只读 API 端点，前端页面调用它 | 前后端联调 | `cd web && npx tsc --noEmit` 0 错 + 页面显示正确 |
| 5 | 完整走一遍"改需求 → 改代码 → 补测试 → 更新文档" | 全链路 | 693 个测试仍全绿 + `python scripts/onboarding_check.py --map docs/10-code-map.md` 通过（改了代码位置就要同步文档） |

**练习 1 必须做**：它会逼你同时碰领域层、状态机校验、测试三处，是理解"改动影响面"最快的方式。
**练习 5 必须做**：它验的不是技术，是习惯——文档与代码同源，是这个仓库的底线。

---

## 四、验收单（Day 2 末，Mentor 逐项签字）

| # | 验收项 | 通过标准 |
|---|---|---|
| 1 | **能讲** | 不看文档，5 分钟口述一条线索从录入到成交：每步经过哪个包、数据落在哪张表、哪几步必须人批 |
| 2 | **能定位** | 随机抽 5 个"我要改 X"（从 10-code-map 表 A 挑），每个 5 分钟内找到文件和符号；可用 `--probe` 自证 |
| 3 | **懂边界** | 说清 3 个"看似合理的错误改法"为什么错、正确位置在哪（10-code-map 第四节） |
| 4 | **能改对** | 完成一个不破坏现有测试的小需求；693 个测试全绿；没有违反七条设计原则 |
| 5 | **会验证** | 能用 `--probe` 自查代码位置；能用测试验证自己的改动；知道文档锚点失效时改文档而不是绕过 |

**五项全过 = 2 天目标达成**：买到了"地图 + 指南针"，接下来 30 天按 `docs/00-onboarding.md` 的深入路线走。

---

## 五、附：给 `--answer` 模式的答案文件示例

```text
# 我的 Day1 答案（# 开头是注释，一行可以写多个锚点）
Q1 src/flow_ops/leads.py::ALLOWED_TRANSITIONS
Q7 src/flow_ops/idempotency.py::make_idempotency_key
Q9 src/gateway/cache.py::CacheLayer
# Q13 顺序题不用写锚点，写：assert_approved → CRM → mark_executed
```

跑 `python scripts/onboarding_check.py --answer my_answers.txt`，输出会逐条标 `ok` / `FAIL`；
`FAIL` 会顺手告诉你符号真正在哪（如"它其实在 `leads.py:51`"）。
