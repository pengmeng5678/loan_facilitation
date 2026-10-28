# API 手册

启动后访问 `http://localhost:8000/docs` 可在 Swagger UI 中直接调试。

> Swagger UI 的 JS/CSS **已本地化**（`src/agent_platform/api/static/swagger-ui/`，随代码发货），
> 不依赖 `cdn.jsdelivr.net` —— 国内网络无外网也能正常打开。`/redoc` 已关闭（同理由外网加载）。
> 原始 OpenAPI 规范在 `/openapi.json`，可用 Postman / 代码生成器直接消费。

**通用约定**
- 请求/响应均为 JSON（`Content-Type: application/json`）
- 错误结构：`{"error": "<异常类名>", "detail": "<说明>"}`
- 所有 `as_of` 参数格式为 `YYYY-MM-DD`，缺省为今天
- **鉴权**：配置 `API_AUTH_ENABLED=true` 后，除下列白名单外所有路径都要凭证
  （`X-API-Key: <key>` 或 `Authorization: Bearer <key>`，任选其一）；
  没带/带错都返回固定文案的 401 —— 不告诉调用方是"没带"还是"带错了"：

| 白名单路径 | 为什么放行 |
|---|---|
| `/health` `/ready` `/` | 探针要 key 等于没有探针；`/ready` 响应体也只回答"好/没好"（内部异常详情只在服务端日志） |
| `/metrics` | 抓取端带 key 很别扭；生产按来源 IP 收紧（反代层） |
| `/docs` `/openapi.json` `/static/*` | 交互式文档与静态资源；生产通常在反代层（Nginx/APISIX）再收紧 |
| `/voice/callback/*` | 有独立更强的回调鉴权 `VOICE_CALLBACK_API_KEY`（生产必填） |

---

## 一、运维

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针（K8s liveness） |
| GET | `/ready` | 就绪探针，返回已装配的 Agent / 工具 / 数据统计 |
| GET | `/` | 服务信息与路由前缀（含前端挂载点 `web`） |
| GET | `/metrics` | Prometheus 指标（`text/plain; version=0.0.4`，无需 Key） |
| GET | `/ui/*` | Web 工作台静态产物（`web/` 构建后存在；dev 模式用 5173） |

```bash
curl localhost:8000/ready
# {"status":"ready","agents":["product_assistant","coaching","marketing","voice_cs","gateway","flow_ops"],"tools":56,...}

curl localhost:8000/metrics
# # HELP app_info 应用信息（版本/环境/装配形态）
# # TYPE app_info gauge
# app_info{version="1.0.0",env="local",...} 1.0
# # TYPE flow_leads gauge
# flow_leads{stage="assigned"} 1
# ...
```

`/metrics` 指标族：`app_info`（版本/环境）、`runtime`（进程运行时长）、
`gateway_*`（调用/成本/缓存命中/限流，配了网关才有数）、`flow_*`（线索各阶段数/
幂等命中/锁等待/审批）、`scheduler_*`（SOP 扫描轮次与预警数）。
**采集失败也返回 200** —— 监控接口不能成为故障源；抓取端按 IP 收紧请在反代层做。

---

## 二、项目五 · 平台（`/platform`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/platform/agents` | 列出已装配的 Agent 及装配信息（是否含 planner/critic、工具数） |
| GET | `/platform/agents/{name}` | 单个 Agent 的装配详情 |
| POST | `/platform/agents/{name}/run` | **运行一个 Agent** |
| POST | `/platform/runs/{thread_id}/resume` | **HITL 审批后恢复执行** |
| GET | `/platform/runs/{thread_id}` | 查询会话当前状态 |
| GET | `/platform/tools` | 工具生态清单（含 `risk_level` / `idempotent`） |
| GET | `/platform/metrics` | 指标快照（计数器 + 延迟直方图 + 熔断器状态） |
| GET | `/platform/approvals` | 待审批列表 |
| POST | `/platform/approvals/{request_id}/decide` | 按请求 ID 审批 |

### 运行 Agent

```bash
curl -X POST localhost:8000/platform/agents/product_assistant/run \
  -H 'Content-Type: application/json' \
  -d '{
    "input": "客户 C1001 适合做什么产品？",
    "context": {"as_of": "2026-01-01"},
    "thread_id": "demo-001"
  }'
```

响应（截断）：

```json
{
  "run_id": "run_xxx",
  "thread_id": "demo-001",
  "agent": "product_assistant",
  "status": "completed",
  "route": "match",
  "steps": 2,
  "tokens_used": 693,
  "output": {
    "route": "match",
    "match": {"matched": true, "primary": {"product_name": "公积金尊享贷（高额度）", "pass_probability": 0.82}},
    "summary": "★ 主力方案：..."
  },
  "tool_calls": [{"tool": "product.match", "ok": true, "latency_ms": 1.2}],
  "trajectory": [{"name": "route", "kind": "node", "duration_ms": 0.01}]
}
```

`status` 取值：

| 值 | 含义 |
|---|---|
| `completed` | 正常完成 |
| `interrupted` | **被 HITL 挂起**，需调用 resume |
| `escalated` | 触发预算熔断，已转人工 |
| `failed` | 执行异常（`error` 字段有原因） |

### HITL 审批闭环

```bash
# ① 触发一个高风险动作（质检发现违规 → 告警主管）
curl -X POST localhost:8000/platform/agents/coaching/run \
  -H 'Content-Type: application/json' \
  -d '{"input":"对 CONV-0002 做合规质检并告警主管",
       "context":{"conversation_id":"CONV-0002","supervisor":"张主管"}}'
# → status = "interrupted"，pending_approval 里带 tool / arguments / risk_level / reason

# ② 查看待办
curl localhost:8000/platform/approvals

# ③ 批准并恢复
curl -X POST localhost:8000/platform/runs/<thread_id>/resume \
  -H 'Content-Type: application/json' \
  -d '{"approved": true, "decided_by": "张主管", "comment": "已核实"}'
# → status = "completed"，approvals 里留下完整审批留痕
```

---

## 三、项目二 · 产品知识助手（`/product`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/product/qa` | 产品政策问答（带引用，无依据拒答） |
| POST | `/product/match` | 结构化方案匹配 |
| POST | `/product/proposal` | 生成方案书（无匹配返回 409） |
| GET | `/product/products` | 有效产品目录（自动过滤过期政策） |
| GET | `/product/products/{code}` | 产品详情 |
| GET | `/product/products/{code}/graph` | 产品图谱关系（资质/用途/替代品） |
| GET | `/product/customers` | 客户档案列表 |
| GET | `/product/customers/{id}` | 客户档案详情（含负债比） |
| GET | `/product/stats` | 知识库统计 |

### 政策问答

```bash
curl -X POST localhost:8000/product/qa -H 'Content-Type: application/json' \
  -d '{"question":"公积金信用贷的利率和准入条件是什么？"}'
```

```json
{
  "answer": "根据现行政策条款，……",
  "citations": ["[BANK-A-GJJ-001-ELIG-01] · 产品 BANK-A-GJJ-001 · 第 3.1 条 · 生效 2024-06-01"],
  "refused": false,
  "verified": true,
  "verify_issues": [],
  "related_products": ["BANK-A-GJJ-001", "BANK-H-GJJ-002"],
  "hits": [{"score": 0.73, "bm25": 31.38, "dense": 0.275, "rerank": 0.467}]
}
```

`verified=false` 表示答案里的数字/产品编号/条款号在引用原文中找不到，
服务会按"无依据"拒答（`refused=true`），并把不一致项放进 `verify_issues`；
`citations` 只保留**实际被答案引用**的块，而不是所有召回块。

无政策依据时：

```json
{"answer": "抱歉，我未检索到可支撑该问题的现行政策条款……", "refused": true,
 "reason": "无政策依据（检索结果为空或低于相关度阈值）", "citations": []}
```

### 方案匹配

```bash
curl -X POST localhost:8000/product/match -H 'Content-Type: application/json' \
  -d '{"customer_id":"C1001"}'
```

```json
{
  "matched": true,
  "primary": {
    "product_code": "BANK-H-GJJ-002",
    "product_name": "公积金尊享贷（高额度）",
    "score": 0.912,
    "pass_probability": 0.82,
    "estimated_amount": 300000,
    "rate_display": "2.98%–3.90%（以银行审批为准）",
    "reasons": ["资质匹配：公积金满足该产品准入", "征信查询 3 次 ≤ 上限 6 次（余量充足）"],
    "risks": ["暂无明显风险项，仍需以银行实际审批为准"],
    "margin_detail": {"征信查询余量": 0.5, "负债比余量": 0.43}
  },
  "alternates": [{"product_name": "公积金信用贷（标准版）"}],
  "rejected_summary": [
    {"product_name": "社保信用贷", "reason": "用途不匹配（产品支持 资金周转/装修，客户需求 债务置换）"}
  ],
  "weights": {"pass_probability": 0.6, "rate": 0.25, "amount": 0.15},
  "summary": "★ 主力方案：……"
}
```

无匹配时 `matched=false` + `handoff_reason`（**绝不硬配**）。

---

## 四、项目三 · 话术陪练与考核（`/coaching`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/coaching/personas` | 客户画像列表 |
| POST | `/coaching/sessions` | 开启陪练会话 |
| POST | `/coaching/sessions/{id}/reply` | 学员发言，模拟客户回应 |
| POST | `/coaching/sessions/{id}/finish` | 结束并出评分卡 |
| GET | `/coaching/sessions/{id}/replay` | 复盘回放 |
| GET | `/coaching/sessions` | 会话列表 |
| POST | `/coaching/qc` | 质检（传 `conversation_id` 查单个，留空查全部） |
| POST | `/coaching/qc/alert` | 对高风险对话告警主管（**走 HITL，返回 interrupted**） |
| POST | `/coaching/mining` | 高转化话术挖掘 |
| POST | `/coaching/calibrate` | Judge 与人工双盲一致性校准 |
| GET | `/coaching/stats` | 服务统计 |

### 完整陪练流程

```bash
# ① 开启会话
curl -X POST localhost:8000/coaching/sessions -H 'Content-Type: application/json' \
  -d '{"persona_id":"P-PRICE","trainee":"学员小陈"}'
# → {"session_id":"cs_xxx","opening":"我已经问过好几家了，你们先说说利率和费用？"}

# ② 逐轮对话（模拟客户会抵抗）
curl -X POST localhost:8000/coaching/sessions/cs_xxx/reply \
  -H 'Content-Type: application/json' \
  -d '{"text":"李总您好，我先帮您做资质初审：您有营业执照和房产，近半年征信查询多少次？"}'
# → {"customer_reply":"有的，我有营业执照、房产、车产，月收入大概 45,000……","resistant":false}

# ③ 结束并评分
curl -X POST localhost:8000/coaching/sessions/cs_xxx/finish
```

评分卡（节选）：

```json
{
  "total_score": 83.8,
  "passed": true,
  "vetoed": false,
  "dimensions": [
    {"dimension": "合规红线", "score": 1.0, "weight": 0.35, "passed": true},
    {"dimension": "需求挖掘", "score": 0.75, "weight": 0.25, "passed": true, "notes": "覆盖 3/4 个关键槽位，缺失：用途"}
  ],
  "violations": [],
  "weak_points": ["需求挖掘：覆盖 3/4 个关键槽位，缺失：用途"],
  "recommendations": ["练习'资质-征信-用途-额度'四连问……"],
  "judge_source": "rules"
}
```

出现违规承诺时：`vetoed=true`、`passed=false`、`total_score ≤ 59`、`violations` 列出违规词与原文。

### 全量质检

```bash
curl -X POST localhost:8000/coaching/qc -H 'Content-Type: application/json' -d '{}'
```

```json
{
  "count": 4,
  "needs_alert": 1,
  "average_score": 57.5,
  "reports": [{
    "conversation_id": "CONV-0002",
    "score": 0.0,
    "needs_alert": true,
    "checked_rules": 4,
    "findings": [{
      "rule_id": "QC-001", "category": "违规承诺", "severity": "high",
      "excerpt": "李总您好，您需要的150万经营周转我们可以做，成功率很高，包过！",
      "detail": "出现违规表述『包过』（承诺包过）",
      "suggestion": "删除承诺性表述，改为'预估区间 + 以银行审批为准'"
    }]
  }]
}
```

### 话术挖掘与校准

```bash
curl -X POST localhost:8000/coaching/mining
# → {"scanned":4,"mined":[{"text":"...","score":0.9,"stage":"资质问询","reason":"...","customer_next_reaction":"行，那我周六过去"}]}

curl -X POST localhost:8000/coaching/calibrate -H 'Content-Type: application/json' \
  -d '{"judge_scores":[82,75,90,66],"human_scores":[80,78,88,70],"tolerance":10}'
# → {"samples":4,"agreement_rate":1.0,"mean_bias":-0.75,"bias_direction":"无明显偏差"}
```

---

## 五、项目七 · 素材推广与客户激活（`/marketing`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/marketing/channels` | 各渠道语感规格（字数区间 / 标签数 / 钩子风格） |
| POST | `/marketing/materials` | 批量生成素材（生成即过合规与质量分） |
| GET | `/marketing/materials` | 素材库列表（可按状态过滤） |
| POST | `/marketing/cases` | 成单案例脱敏包装（只有成交对话可入库） |
| POST | `/marketing/activation/plan` | 生成未成单客户激活计划（含防骚扰过滤） |
| GET | `/marketing/activation/tasks` | 激活任务列表（按优先级排序） |
| POST | `/marketing/activation/send` | 发送素材给客户（**走 HITL，返回 interrupted**） |
| POST | `/marketing/audit` | 合规审核（先审后发；业务员自己写的话术也能审） |
| GET | `/marketing/audits` | 合规审核流水（可供监管审计） |
| POST | `/marketing/attribute` | 素材效果归因（渠道线索占比 / 最佳素材） |
| POST | `/marketing/touch` | 上报一次素材触达（曝光/咨询/留资/成单） |
| GET | `/marketing/stats` | 服务统计 |

### 生成素材（渠道语感是硬约束）

```bash
curl -X POST localhost:8000/marketing/materials -H 'Content-Type: application/json' \
  -d '{"channel":"douyin","theme":"征信解读","count":1}'
```

```json
{
  "count": 1,
  "channel": "douyin",
  "rejected": 0,
  "needs_human": 0,
  "materials": [{
    "draft_id": "md_44a87ec5bc",
    "material_id": "mat_md_44a87ec5bc",
    "channel": "douyin",
    "theme": "征信解读",
    "title": "征信解读｜征信解读·抖音口播",
    "body": "【0-3秒】很多人问：查了十几次征信，为什么反而办不下来？\n【痛点】硬查询次数是银行直接看的指标...\n【方案】遇到这种情况，第一件事不是继续申贷，而是停下来...",
    "hashtags": ["征信解读", "征信知识", "贷款攻略"],
    "cta": "不确定自己征信能不能做的，点头像问我，先判断方向再动手。",
    "status": "approved",
    "quality_score": 0.94,
    "compliance": {"passed": true, "findings": [], "checked_rules": 6, "needs_human": false}
  }],
  "texts": ["【抖音｜征信解读】征信解读｜征信解读·抖音口播\n..."]
}
```

`status` 三种取值对应"先审后发"的三种结局：

| status | 含义 | 能否发布 |
|---|---|---|
| `approved` | 一次过审 | 可以 |
| `sanitized` | 命中但已自动改写成合规表达（改动留痕） | 可以 |
| `rejected` | 命中且不可改写（如含客户手机号） | **不可以**，必须人工处理后重提 |

### 合规审核（先审后发）

```bash
curl -X POST localhost:8000/marketing/audit -H 'Content-Type: application/json' \
  -d '{"text":"黑户也能做！百分百下款，保证下款不看征信。"}'
```

```json
{
  "passed": true,
  "needs_human": false,
  "sanitized": true,
  "max_severity": "high",
  "sanitized_text": "资质较弱亦有方案做！以银行审批结果为准，以银行审批结果为准综合评估资质。",
  "findings": [
    {"rule_id": "PROMISE-01", "severity": "high", "matched": "保证下款", "category": "承诺性话术", "source": "rules"},
    {"rule_id": "PROMISE-01", "severity": "high", "matched": "百分百",   "category": "承诺性话术", "source": "rules"},
    {"rule_id": "BYPASS-01",  "severity": "high", "matched": "不看征信", "category": "规避审查",   "source": "rules"},
    {"rule_id": "BYPASS-01",  "severity": "high", "matched": "黑户也能", "category": "规避审查",   "source": "rules"}
  ]
}
```

注意 `findings` 是**首次命中 + 复检命中的并集**：改写成功所以 `passed=true`，
但"拦下并改写了什么"仍然留在报告里 —— 审计要能回答"审核到底做了什么"。

含客户隐私时无法自动改写，只能拦截转人工：

```bash
curl -X POST localhost:8000/marketing/audit -H 'Content-Type: application/json' \
  -d '{"text":"客户手机号 13900001111，先加微信聊。"}'
# → {"passed":false,"needs_human":true,"sanitized":false,"sanitized_text":"","max_severity":"high",
#    "findings":[{"rule_id":"PRIVACY-01","severity":"high","matched":"13900001111","category":"敏感信息"}]}
```

### 激活计划与发送（HITL）

```bash
curl -X POST localhost:8000/marketing/activation/plan -H 'Content-Type: application/json' -d '{"limit":10}'
```

```json
{
  "plan_id": "plan_5b39c516ff",
  "generated": 6,
  "tasks": [{
    "task_id": "act_714cff7290",
    "customer_id": "C1005",
    "name_masked": "陈*女",
    "dormant_days": 35,
    "trigger": "我这种资质担心办不下来",
    "concern": "征信",
    "channel": "douyin",
    "material_title": "征信解读｜征信解读·抖音口播",
    "suggested_send_at": "2026-10-19 10:00",
    "priority": 0.8,
    "reason": "沉睡 35 天；犹豫点「我这种资质担心办不下来」→ 用「征信解读」主题素材对症触达（同渠道无对症素材，按素材原渠道发布）",
    "status": "pending"
  }],
  "skipped": [
    {"customer_id": "C1007", "name_masked": "孙*女", "reason": "未达沉睡阈值（2<7 天）"},
    {"customer_id": "C1008", "name_masked": "周*生", "reason": "处于静默期（1<3 天）"}
  ]
}
```

发送走编排层 → 高风险非幂等动作 → 自动挂起等审批：

```bash
curl -X POST localhost:8000/marketing/activation/send -H 'Content-Type: application/json' \
  -d '{"customer_id":"C1002"}'
# → {"status":"interrupted",
#    "pending_approval":{"type":"approval_required","request_id":"apr_xxx",
#      "tool":"platform.send_external_message",
#      "arguments":{"target":"C1002","content":"...素材正文 + CTA（不含状态/质量分等内部信息）..."},
#      "risk_level":"high","reason":"高风险动作（risk=high，非幂等=True），需人工确认后执行"}}
# 审批：POST /platform/runs/{thread_id}/resume  {"approved":true,"decided_by":"业务员小陈"}
```

### 效果归因

```bash
curl -X POST localhost:8000/marketing/attribute -H 'Content-Type: application/json' -d '{}'
```

```json
{
  "total_materials": 30,
  "total_touches": 12611,
  "by_channel": {
    "moments":     {"impression": 2410.0, "consult": 243.0, "lead": 89.0, "deal": 14.0, "touches": 2756.0},
    "douyin":      {"impression": 5810.0, "consult": 362.0, "lead": 93.0, "deal": 10.0, "touches": 6275.0},
    "xiaohongshu": {"impression": 3230.0, "consult": 271.0, "lead": 74.0, "deal":  5.0, "touches": 3580.0}
  },
  "lead_share": {"moments": 0.348, "douyin": 0.363, "xiaohongshu": 0.289},
  "conversion_rate": 0.113,
  "top_materials": [{"title": "征信解读｜征信解读·抖音口播", "channel": "douyin", "touches": 2039, "leads": 41, "deals": 5}],
  "conclusions": [
    "抖音带来的留资最多（93 条，占 36%）",
    "表现最好的素材是「征信解读｜征信解读·抖音口播」（抖音，留资 41 条），建议同主题加量产出",
    "素材触达 → 成单转化率 11.3%（以留资为分母）"
  ]
}
```

> 数值随演示数据与运行次数变化，字段结构固定。
> 注意聚合字段是单数 `lead` / `deal` —— `to_text()` 里曾把它写成复数，
> 结果报表显示"留资 0"而结论写着"留资最多"，是最典型的口径事故。

---

## 六、项目六 · 推理网关与评测（`/gateway`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/gateway/stats` | 网关运行态快照（调用数 / 命中率 / 各档位分布 / 节省比例 / provider 熔断状态 `breaker`） |
| GET | `/gateway/scenes` | 评测场景与用例概况 |
| POST | `/gateway/route/preview` | 路由预览（会走哪个档位、为什么） |
| POST | `/gateway/demo` | 走一次真实网关调用（演示成本治理效果） |
| GET | `/gateway/costs` | 成本报告（含每会话单价） |
| GET | `/gateway/replay/{thread_id}` | 按会话回放决策轨迹 |
| POST | `/gateway/eval` | 跑一轮 Golden Set 评测 |
| POST | `/gateway/eval/gate` | 评测 + 基线比对（CI 准入结论） |
| GET | `/gateway/eval/runs` | 历史评测运行记录 |
| POST | `/gateway/calibrate` | Judge 与人工专家双盲一致性校准 |

### 路由预览

```bash
curl -X POST localhost:8000/gateway/route/preview -H 'Content-Type: application/json' \
  -d '{"text":"在吗","task":""}'
# → {"tier":"small","model":"qwen-turbo","complexity":0.2,
#    "reason":"复杂度 0.200 （<0.35→small / <0.7→medium / else→large）",
#    "fallback_chain":["qwen-turbo"]}

curl -X POST localhost:8000/gateway/route/preview -H 'Content-Type: application/json' \
  -d '{"text":"写一条朋友圈素材","task":"material"}'
# → {"tier":"small","model":"qwen-turbo","complexity":0.2001,
#    "reason":"复杂度 0.200 （<0.35→small / <0.7→medium / else→large）",
#    "signals":{"size":0.0,"long_document":0.0,"reasoning_markers":[],"task_prior":0.2,...},
#    "forced":false,"fallback_chain":["qwen-turbo"]}

curl -X POST localhost:8000/gateway/route/preview -H 'Content-Type: application/json' \
  -d '{"text":"写一条朋友圈素材","task":"material"}'
# → {"tier":"medium","model":"qwen-plus","complexity":0.3004,
#    "reason":"复杂度 0.300 （...）；任务 material 有档位保底 medium，已上调",
#    "fallback_chain":["qwen-plus","qwen-turbo"]}
```

### 成本治理效果

```bash
curl -X POST localhost:8000/gateway/demo -H 'Content-Type: application/json' \
  -d '{"prompt":"写一条朋友圈素材","task":"material","thread_id":"demo-1"}'
# 第一次 → {"tier":"medium","model":"qwen-plus","provider_model":"echo-offline",
#           "cost":0.000384,"baseline_cost":0.00192,"saved_percent":80.0,"cache_status":"miss"}
# 第二次 → {"tier":"medium","cost":0.0,"baseline_cost":0.00192,
#           "saved_percent":100.0,"cache_status":"hit_prefix"}
```

`model` 是**档位模型名**（与台账/路由同一口径），`provider_model` 是供应商自报的模型名。
两者分开上报，避免"缓存命中时显示成另一个模型"这种看起来像 bug 的现象。

```bash
curl localhost:8000/gateway/costs
# → {"total":{"calls":2,"cost":0.000384,"baseline_cost":0.00384,"saved_percent":90.0},
#    "by_tier":{"medium":{"calls":2,"cost":0.000384,"tokens":96,...}},
#    "by_task":{"material":{...}},
#    "per_session":{"demo-1":{"calls":2,"cost":0.000384,"tokens":96}},
#    "cache_hit_rate":0.5}
```

（离线 echo 模式的单价是"演示估值"，真实金额以 `GATEWAY_PRICE_*` 配置与实际账单为准。）

### 评测与 CI 准入

```bash
curl -X POST localhost:8000/gateway/eval -H 'Content-Type: application/json' \
  -d '{"scene":"all"}'
# → {"scene":"all","total":10,"passed":10,"failed":0,"score":1.0,"case_results":[...]}

curl -X POST localhost:8000/gateway/eval/gate -H 'Content-Type: application/json' \
  -d '{"scene":"all","max_regression":0.02}'
# → {"current":{"eval_id":"eval_b8770c753e","score":1.0,"passed":10,"total":10},
#    "baseline":{"eval_id":"eval_c374f87feb","score":1.0},
#    "gate":{"allowed":true,"reasons":["未发现退化，允许发布"],"delta":0.0,"regressions":[]}}
# 首跑（无基线）→ {"gate":{"allowed":true,"reasons":["无基线（首次评测），默认放行并以此作为新基线"]}}
```

准入结论的三条拒发理由（任一命中即 `allowed=false`）：

| 理由 | 说明 |
|---|---|
| `整体加权通过率退化 ...` | 退化幅度超过 `max_regression` |
| `新失败用例：['xxx']` | 单条用例从"过"变"挂" —— **无论阈值多宽都拦** |
| `命中合规红线，直接拦截：['xxx']` | 不看总分，直接拦 |

### 双盲校准

```bash
curl -X POST localhost:8000/gateway/calibrate -H 'Content-Type: application/json' \
  -d '{"judge_scores":[82,75,90,66],"human_scores":[80,78,88,70],"tolerance":10}'
# → {"samples":4,"agreement_rate":1.0,"mean_abs_diff":2.75,"max_abs_diff":4.0,"tolerance":10.0,"passed":true}
```

---

## 七、项目一 · 语音客服（`/voice`）

这个前缀下有**三类使用者**，接口按这个分：

| 使用者 | 接口 |
|---|---|
| 前端（React + `@volcengine/rtc`） | `GET /scenes`、`POST /rooms`、`POST /rooms/start`、`POST /rooms/stop`、`POST /rooms/interrupt` |
| **火山 RTC 云端**（第三方 LLM 回调） | `POST /callback/chat`（OpenAI 协议，`stream=true` 返回 SSE） |
| 内部调试 / 运营 | `POST /ask`、`POST /audit`、`GET /sessions*`、`GET /metrics`、`GET /prompts`、`POST /eval` |

### 拿进房凭证

```bash
curl -X POST localhost:8000/voice/rooms \
  -H 'Content-Type: application/json' \
  -d '{"scene_id":"consultant","user_id":"C1002"}'
# → {"rtc":{"AppId":"...","RoomId":"room_38f9607e","UserId":"C1002",
#           "Token":"001...","BotUserId":"Huoshan01","ExpireAt":1792326217,"DryRun":true},
#    "scene_id":"consultant","dry_run":true}
```

> 这里返回**完整 Token**（客户端要拿它进房），其他任何地方（日志/会话详情/开房回显）
> 都只出现打码版本。`DryRun=true` 表示未配密钥，这个 Token 进不了真实房间。

**调用顺序不能反**：前端先 `joinRoom`，再 `POST /rooms/start` 让服务端拉 AI 进房。
反过来的话云端 AI 会对着空房间念开场白。

### 拉起云端 AI（StartVoiceChat）

```bash
curl -X POST localhost:8000/voice/rooms/start \
  -H 'Content-Type: application/json' \
  -d '{"scene_id":"consultant","room_id":"room_38f9607e","user_id":"C1002"}'
# → {"session":{"session_id":"vs_xxx","status":"running",...},
#    "rtc":{"action":"StartVoiceChat","ok":true,"dry_run":true,"payload":{...已脱敏...}},
#    "join":{"Token":"001dry-run...（137 字符，已脱敏）",...}}
```

`rtc.payload` 就是我们实际下发的配置（ASR 双向流 / TTS 音色 / LLM 回调地址），
dry-run 时也完整可见 —— 这是排查"AI 不说话"的第一手材料。

### RTC 云端回调（第三方 LLM）

```bash
curl -N -X POST localhost:8000/voice/callback/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Room-Id: room_38f9607e' \
  -H 'Authorization: Bearer <VOICE_CALLBACK_API_KEY>' \
  -d '{"model":"doubao-seed-1-6-flash","stream":true,
       "messages":[{"role":"user","content":"公积金信用贷需要哪些资料？"}]}'
```

```
data: {"id":"chatcmpl-voice","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"政策上写的是："},"finish_reason":null}]}

data: {"...","delta":{"content":"办理 公积金信用贷（标准版） 需提供以下资料：身份证、…"}}

data: {"...","delta":{},"finish_reason":"stop"}
data: [DONE]
```

三个硬约束：
- **必须流式**：非流式时 TTS 要等整段生成完才开口，首字延迟 3~5s；
- **必须校验鉴权**：配了 `VOICE_CALLBACK_API_KEY` 就校验 `Authorization`，否则这个公网
  地址等于一个免费的大模型代理（未配时放行并打警告，只用于本地演练）；
- **客户端断开即取消**：`request.is_disconnected()` 为真时置位 `CancelToken`，
  服务端生成循环在下一个句边界退出（RTC 停 TTS 只是它那一侧停了，我们这边还在烧 token）。

### 文本模拟一轮（不花 RTC 的钱）

```bash
curl -X POST localhost:8000/voice/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"公积金信用贷的办理流程是什么？","scene_id":"consultant"}'
# → {"answer_text":"政策上写的是：…办理流程：提交资料 → 银行预审 → 线下面签 → 放款到账。…",
#    "refused":false,"needs_human":false,"citations":["…"],"prompt_version":"consultant@v1",
#    "latency":{"rag_ms":1.2,"llm_ttft_ms":0.0,"total_ms":1.1,"chunks":3}}
```

查不到依据时 `refused=true`、`needs_human=true` —— **不调模型，直接转人工**。

### 内容安全自查

```bash
curl -X POST localhost:8000/voice/audit \
  -H 'Content-Type: application/json' \
  -d '{"text":"放心，我们包过，百分百下款"}'
# → {"passed":false,"needs_human":true,"blocked_at_runtime":true,"max_severity":"high",
#    "safe_reply":"能不能批、批多少，都以银行最终审批为准…",
#    "findings":[{"rule_id":"PROMISE-01","category":"承诺性表述","severity":"high","matched":"百分百"},…],
#    "text":"内容安全：拦截｜最高等级 high｜命中 2 条｜需人工 True\n  · [PROMISE-01/high] …"}
```

### 评测与提示词版本

```bash
curl -X POST localhost:8000/voice/eval -H 'Content-Type: application/json' \
  -d '{"prompt_key":"consultant"}'
# → {"prompt_key":"consultant","version":"v1","total":7,"passed":7,"score":1.0,"cases":[...]}

curl -X POST localhost:8000/voice/eval/gate -H 'Content-Type: application/json' \
  -d '{"prompt_key":"consultant"}'
# → {"current":{...},"baseline":null,"gate":{"allowed":true,"reasons":["无基线（首次评测），默认放行并以此作为新基线"]}}

# 新增候选版本（默认不生效）→ 跑评测 → 再切 active
curl -X POST localhost:8000/voice/prompts -H 'Content-Type: application/json' \
  -d '{"key":"consultant","system_prompt":"你是顾问 {{knowledge}} {{extra}}","max_tokens":180}'
curl -X POST localhost:8000/voice/prompts/consultant/activate \
  -H 'Content-Type: application/json' -d '{"version":"v2"}'
```

> `POST /voice/prompts` 会校验 `{{knowledge}}` 占位符：缺了它模型拿不到知识，
> 会静默退化成闲聊 —— 这种"看起来能跑但一定答错"的配置必须挡在入口。

### 指标

```bash
curl localhost:8000/voice/metrics
# → {"turns":12,"refused":2,"handoff":2,"slow_ttft":0,
#    "refuse_rate":0.167,"handoff_rate":0.167,
#    "latency_ms":{"ttft_p50":820.0,"ttft_p95":1900.0,"rag_p50":35.0,"total_p50":2600.0,"total_p95":4100.0}}
```

首字超过 2500ms 会单独计入 `slow_ttft` —— 这是语音体验的分水岭（客户会开口打断）。

---

## 八、项目八 · 流程数字员工（`/flow`）

线索全生命周期：`new → assigned → first_called → wechat_added → nurturing → invited → arrived → proposal_ready → fee_estimated → signed / lost`。
**不允许跳阶段**（每步 SOP 都是留痕点）；`reception / negotiation / fee_confirmation` 是人工节点，Agent 进不去。
### 端点总览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/flow/leads` | 录入线索（幂等，支持 `Idempotency-Key` 头） |
| GET | `/flow/leads` | 线索列表（可按阶段/业务员过滤） |
| GET | `/flow/leads/{lead_id}` | 线索详情（含流转记录与邀约） |
| POST | `/flow/leads/{lead_id}/advance` | 推进阶段（人工操作，非法流转返回 409） |
| POST | `/flow/leads/{lead_id}/invite` | 邀约到店（幂等） |
| POST | `/flow/leads/{lead_id}/arrived` | 到店确认（幂等，自动挂起人工接待节点） |
| POST | `/flow/leads/{lead_id}/readiness` | 到店前准备（档案 + 方案初稿 + 费用预估一页纸） |
| POST | `/flow/leads/{lead_id}/fee` | 费用预估（等额本息 + 服务费分档） |
| POST | `/flow/leads/{lead_id}/handoff` | 转人工节点 |
| GET | `/flow/sop/alerts` | SOP 预警列表（主管待办） |
| POST | `/flow/sop/sweep` | 手动触发 SOP 扫描 |
| POST | `/flow/sop/alerts/{alert_id}/ack` | 认领预警（认领即静音） |
| GET | `/flow/approvals` | 审批列表 |
| POST | `/flow/leads/{lead_id}/request-contract` | 申请发送合同（HITL 高风险） |
| POST | `/flow/leads/{lead_id}/confirm-fee` | 申请确认费用（HITL 高风险） |
| POST | `/flow/approvals/{approval_id}/decide` | 审批决策 |
| POST | `/flow/approvals/{approval_id}/execute` | 执行已审批动作 |
| POST | `/flow/progress` | 客户自助查进度（双重身份校验 + 脱敏） |
| GET | `/flow/audit` | 审计留痕查询 |
| GET | `/flow/metrics` | 流程指标（漏跟进率 / 幂等命中 / 待审批 / 准备耗时） |
| GET | `/flow/stats` | 服务统计 |
| POST | `/flow/eval` | 跑流程 Golden Set（11 条内置用例） |
| POST | `/flow/eval/gate` | 准入门禁（先取基线再跑本次） |

### 录入线索（幂等）

```bash
curl -X POST localhost:8000/flow/leads \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: intake-C1002-20260901" \
  -d '{"customer_id":"C1002","customer_name":"测试客户","phone_masked":"138****5678","source":"douyin","owner":"rep_01"}'
# 同一 Idempotency-Key 重复提交不会建第二条线索（返回当前状态，不是首次快照）
```

### 推进阶段（跳步被拒）

```bash
curl -X POST localhost:8000/flow/leads/{lead_id}/advance \
  -H "Content-Type: application/json" -d '{"to_stage":"first_called","reason":"已完成首呼"}'
# invited → assigned 这种跳阶段返回 409 {"detail":"非法流转：invited → assigned"}
```

### SOP 扫描与预警

```bash
curl -X POST localhost:8000/flow/sop/sweep
# → {"new_alerts":1,"alerts":[{"lead_id":"...","action":"first_call","overdue_minutes":12.5,...}]}
curl -X POST localhost:8000/flow/sop/alerts/{alert_id}/ack?by=mgr_01
# 认领后 2×SLA 内不再重复轰炸同一线索同一动作
```

### 到店前准备（一页纸）

```bash
curl -X POST localhost:8000/flow/leads/{lead_id}/readiness
# → 客户档案（资质/风险点）+ 方案初稿（调用项目二 match_customer）
#   + 费用预估 + 谈话要点 + 准备就绪告警
```

### 高风险动作必须先审批

```bash
# 1) 申请（此时不会真的发合同）
curl -X POST localhost:8000/flow/leads/{lead_id}/request-contract \
  -H "Content-Type: application/json" -d '{"reason":"客户已确认方案"}'
# → {"approval_id":"...","status":"pending",...}

# 2) 审批
curl -X POST localhost:8000/flow/approvals/{approval_id}/decide \
  -H "Content-Type: application/json" -d '{"approver":"mgr_01","approved":true,"comment":"合规已核"}'

# 3) 执行（未审批直接执行返回 409 approval_required）
curl -X POST localhost:8000/flow/approvals/{approval_id}/execute
```

> 过期（超过 `APPROVAL_TIMEOUT_SECONDS`）的审批单只能转 `expired`，
> **绝不会自动批准** —— 这是写死在 `approval.py` 的铁律。

### 客户自助查进度（脱敏）

```bash
curl -X POST localhost:8000/flow/progress \
  -H "Content-Type: application/json" -d '{"lead_id":"...","phone_last4":"5678"}'
# 手机号后 4 位错误 → 403（不提示是哪一位错，防枚举）
# 返回内容服务端脱敏：姓名留首字、无完整手机号、无征信明细、new 阶段不进时间线
```

---

## 九、项目四 · 对话数据中台（`/data`）

先说三条使用约定：

- **抽取是幂等的**：`msg_id` / `block_id` 由内容派生，重跑不会产生重复待审条目；
- **导出只导 approved**，且导出前会再脱敏一次并校验零残留；同输入同输出（复用已有文件）；
- 错误码刻意区分：**409** 状态冲突（非法流转/未审批就导出）、**503** 数据源不可用、**422** 参数错。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/data/rules` | 脱敏规则、9 级分类口径、三层脱敏说明 |
| GET | `/data/stats` | 全量统计（原始/块/暂存/导出/知识） |
| GET | `/data/pipeline` | 漏斗视图（含丢弃率与分类分布） |
| POST | `/data/ingest` | 抽取 + 清洗；`{"dry_run": true}` 只出报告不落库 |
| GET | `/data/staging?status=pending&limit=20` | 待审/已审队列 |
| GET | `/data/staging/{id}` | 单条详情 + 审核流水 |
| POST | `/data/staging/{id}/review` | `{"action":"approve|reject|edit","reviewer":"...","comment":"...","messages":[...]}` |
| GET | `/data/audits?staging_id=` | 审核流水 |
| POST | `/data/export` | `{"format":"sharegpt|alpaca|openai|rag_csv"}` |
| GET | `/data/exports` | 导出记录（含 `content_hash` / `watermark`） |
| POST | `/data/knowledge/build` | 构建向量切片 + 结构化文章 |
| GET | `/data/knowledge/search?q=公积金&top_k=3` | 知识检索（向量优先，失败降级关键词） |
| GET | `/data/knowledge/articles` | 知识文章（含 `extractor=llm|rule`） |
| GET | `/data/feed` | 预览供给项目三/七的语料（只含 approved 且非敏感） |

```bash
# 1) 先看规则与漏斗
curl localhost:8000/data/rules
curl localhost:8000/data/pipeline

# 2) 试跑一轮清洗（不落库）
curl -X POST localhost:8000/data/ingest \
  -H 'Content-Type: application/json' -d '{"dry_run": true}'
# → {"raw_messages":33,"sessions":7,"blocks_total":7,"blocks_kept":3,"blocks_dropped":4,...}

# 3) 正式抽取 + 审核
curl -X POST localhost:8000/data/ingest -H 'Content-Type: application/json' -d '{"dry_run": false}'
curl -X POST localhost:8000/data/staging/stg_xxx/review \
  -H 'Content-Type: application/json' -d '{"action":"approve","reviewer":"主管A"}'

# 4) 导出（重复执行会复用同一份产物）
curl -X POST localhost:8000/data/export \
  -H 'Content-Type: application/json' -d '{"format":"sharegpt"}'
# → {"rows":2,"content_hash":"...","watermark":"...","reused":false}

# 5) 知识产物与检索
curl -X POST localhost:8000/data/knowledge/build -H 'Content-Type: application/json' -d '{}'
curl "localhost:8000/data/knowledge/search?q=公积金&top_k=2"
```

---

## 十、用 Python 直接调用（不走 HTTP）

```python
from app_context import build_app_context

ctx = build_app_context()

# 项目二
answer = ctx.product.answer_question("公积金信用贷的利率是多少？")
match = ctx.product.match_customer("C1001")
proposal = ctx.product.generate_proposal("C1001")

# 项目三
session = ctx.coaching.start_session("P-PRICE", "学员小陈")
ctx.coaching.reply(session.session_id, "我先帮您做资质初审……")
card = ctx.coaching.finish_session(session.session_id)
reports = ctx.coaching.qc_all()

# 项目七
drafts = ctx.marketing.generate_materials("douyin", theme="征信解读", count=1)
plan = ctx.marketing.plan_activation()          # 含防骚扰过滤
verdict = ctx.marketing.audit("百分百下款，不看征信。")
report = ctx.marketing.attribute()

# 项目一
join = ctx.voice.join_info(scene_id="consultant", user_id="C1002")   # 进房凭证
started = ctx.voice.start_agent(scene_id="consultant", user_id="C1002")
turn = ctx.voice.ask("公积金信用贷的办理流程是什么？")                # 文本模拟一轮
audit = ctx.voice.audit("我保证您能下款")                             # 出口内容安全
eval_report = ctx.voice.evaluate(prompt_key="consultant")
gate = ctx.voice.gate_prompt(prompt_key="consultant")                 # 版本准入
ctx.voice.stop_agent(started["session"]["session_id"])

# 项目六
route = ctx.gateway_service.route_preview("在吗")
costs = ctx.gateway_service.cost_report()
eval_report = ctx.gateway_service.run_eval("all")
gate = ctx.gateway_service.gate_eval("all")     # CI 准入结论

# 项目八
lead = ctx.flow_ops.intake_lead(customer_id="C1002", customer_name="测试客户",
                                phone_masked="138****5678", source="douyin")
ctx.flow_ops.advance(lead["lead_id"], "assigned")          # 非法跳步抛 FlowError
sweep = ctx.flow_ops.check_sop()                            # SOP 扫描（逾期预警）
pack = ctx.flow_ops.prepare_arrival(lead["lead_id"])        # 到店前一页纸
fee = ctx.flow_ops.estimate_fee(lead["lead_id"], loan_amount=500_000)  # 50 万走 2% 档
req = ctx.flow_ops.request_high_risk("send_contract", lead_id=lead["lead_id"])  # 建审批单
snapshot = ctx.flow_ops.metrics_snapshot()                  # 漏跟进率等指标
flow_gate = ctx.flow_ops.gate()                             # 行为评测准入

# 项目四：数据中台（抽取 → 审核 → 导出 → 知识）
report = ctx.data_platform.ingest()                          # 幂等：重跑不产生重复
pending = ctx.data_platform.list_staging(status="pending")
ctx.data_platform.review(pending[0].staging_id, "approve", reviewer="主管A")
manifest = ctx.data_platform.export("sharegpt")              # 只导 approved
ctx.data_platform.build_knowledge()
hits = ctx.data_platform.search_knowledge("公积金 置换", top_k=2)
feed = ctx.data_platform.feed_conversations()                # 供给项目七的案例语料

# 项目五：直接跑编排
run = ctx.runtime.invoke("product_assistant", "给 C1001 出方案书", context={})
print(run.status, run.output, run.trajectory)
```
