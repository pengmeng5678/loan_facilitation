# 项目六 · 大模型推理网关与 Agent 可观测评测平台

这个包解决的是**上线之后才暴露**的三类问题：

1. **成本失控** —— 有人把阈值一改、或者某个 Agent 把历史全量塞进上下文，账单翻十倍；
2. **质量看不见** —— "这次改 Prompt 到底变好了还是变坏了"只能靠感觉；
3. **出了事查不出** —— 客户投诉"AI 答错了"，但没有轨迹能回答"当时走了哪条路、用了哪个模型"。

> 与项目七的关系：**项目六是基础设施，不是业务 Agent**。它把自己包装成一个
> `LLMProvider` 交给平台使用，业务代码只看到"一个更便宜的 LLM"。
> 它也可以整个摘掉（`GATEWAY_ENABLED=false`），业务不受影响。

---

## 一、模块清单

```
src/gateway/
├── domain.py        # 领域模型：RouteDecision / CostRecord / CacheEntry /
│                    #   EvalCase / CaseResult / EvalReport / GateDecision
├── errors.py        # 网关自有异常（不放进 agent_platform，保持可摘除）
├── router.py        # 复杂度路由 + 降级链
├── cache.py         # 前缀缓存（精确）+ 语义缓存（阈值 0.94 + TTL）
├── trim.py          # 上下文裁剪：去重 / 工具输出摘要 / 历史分层压缩
├── ratelimit.py     # 令牌桶限流 + 按作用域配额
├── keys.py          # 多密钥轮转 + 失败冷却
├── gateway.py       # LLMGateway：把上面这些串成一次完整调用
├── observe.py       # span 级追踪 + 成本台账 + 会话回放
├── store.py         # SQLite 持久化（成本 / 评测报告 / 基线）
├── evaluation/
│   ├── dataset.py   # 内置 Golden Set（10 条 / 5 个场景），支持挂 jsonl 扩充
│   ├── judge.py     # 规则判分（红线一票否决）+ LLM-as-Judge + 双盲校准
│   └── runner.py    # 跑评测 + 与基线比 → CI 准入结论
├── service.py       # 门面：route_preview / demo_generate / cost_report /
│                    #   replay / run_eval / gate_eval / calibrate
├── agent.py         # 装配到平台的 6 个能力函数（route / eval / report）
└── api.py           # /gateway/*
```

---

## 二、成本治理三板斧

### ① 复杂度路由：能省的省，不能省的别省

```
复杂度得分 = 长度信号 + 推理信号（分析/对比/方案/逐条…）+ 结构信号
            └─ < 0.35 → small ｜ < 0.70 → medium ｜ else → large
```

叠加**任务档位保底**：`material`（素材要对外发）/ `activation`（要约到店）/
`policy_qa`（口径错了要担责）/ `judge` 一律不允许落到 small —— 这些任务省下来的钱
远不够赔一次错误回答。

**降级链只能往下退，绝不往上顶**（`router.fallback_chain`）：

| 起始档 | 降级链 |
|---|---|
| large | large → medium → small |
| medium | medium → small |
| small | small（没有后备） |

为什么 small 档不配后备：往上顶会瞬间打爆成本，而且失败原因通常是"上游挂了/配额用尽"，
不是"模型太弱"。要升档必须由调用方**显式**指定 tier，这样"成本超预期"永远能追溯到一次明确的决定。

### ② 缓存：省钱最猛，也最危险

| 缓存 | 命中条件 | 风险 | 适用 |
|---|---|---|---|
| 前缀缓存 | prompt 规范化后完全一致 | 无（同样输入必然同样输出） | 全部场景 |
| 语义缓存 | 余弦相似度 ≥ **0.94** 且未过 TTL | **高**：把 A 产品的利率答成 B 产品的 | 素材 / 陪练等允许近义复用的场景 |

阈值必须"宁漏不错"，且**允许按任务关闭**：`policy_qa`、`judge` 这类确定性要求高的场景
直接 `cache="off"` —— 政策会变、答错代价高，省这点钱不值当。

### ③ 上下文裁剪：不让成本随轮次线性上涨

`trim_messages()` 三招，且是**保守**的（不超预算就原样返回，绝不动最近几轮）：

1. **去重**：连续重复内容只留一份；
2. **工具输出摘要化**：`tool` 角色的长输出通常是中间态，只留头部；
3. **历史分层压缩**：system 指令全留、最近 N 轮全留，更早的压成一条摘要。

> ⚠️ 踩过的坑：摘要的预算最初按"4 字符 ≈ 1 token"（英文经验）算，
> 中文实际约 1 字符 ≈ 2 token，结果 12 轮对话"压缩"完 token 反而多了 38。
> 现在 `_digest()` 直接按 **token** 收敛，硬保证"摘要严格小于原文"。

### ④ 限流与配额

- **令牌桶限流**按 model 维度隔离（一个模型被打爆不影响其它模型）；
- **按作用域配额**（业务线 / 任务），超配额时先**降级到 small 保服务** ——
  业务断了损失更大；**只有降无可降（已经在 small）时才 fail-closed** 抛
  `GatewayQuotaExceededError`，把"排队重试还是拒答"的决定权交回调用方。

### ⑤ 流式与 provider 级熔断

`stream_complete()` 是网关对 `LLMProvider` 流式能力的实现，语音链路直接受益。
它和 `complete()` 有三处**刻意的差异**：

| 维度 | `complete()` | `stream_complete()` |
|---|---|---|
| 缓存 | 前缀/语义缓存全查 | **不查**（半截答案命中没有意义，还会掩盖首字延迟） |
| 降级窗口 | 全程可降级 | **只到首个 delta**：已经念出去的半句话不能换人重来 |
| 中途失败 | 逐档重试 | 不换档、不重试：已生成部分收口返回，记 `stream_interrupted` |

首字前的失败仍然可以换下一档；一旦模型开口，网关锁定该 provider，保证客户听到的是
一个模型连贯的一段话，而不是两段拼接。

**熔断按 `provider.name` 记账**（`CircuitBreaker`，阈值/冷却复用平台配置
`CIRCUIT_FAILURE_THRESHOLD` / `CIRCUIT_RESET_SECONDS`）：

- small 档挂了不会连坐 medium/large —— 否则一个上游故障会把整条降级链一起熔断；
- 熔断打开时该档直接跳过并写入 `errors`，调用方能看到 `CircuitOpen(provider)` 而不是笼统的"全挂"；
- 状态通过 `GET /gateway/stats` 的 `breaker` 字段暴露，`reset_counters()` 会一并重置。

> 取舍说明：中途失败选择"收口"而不是抛错，是为了避免一条 SSE 直接 500；
> 代价是可能截断在半句话上，因此成本台账会把它标成 `ok=false`，排障时应优先看这一类记录。

---

## 三、可观测：从"花了多少"到"为什么花这么多"

`CostRecord` 是成本治理的最小粒度，落库字段包含：
`run_id / thread_id / scope / task / tier / model / prompt_tokens / completion_tokens /
cache_status / trim_saved / fallbacks / latency_ms / ok / error / cost / baseline_cost / saved`。

由此得到的三个口径（`cost_report()`）：

- **总量**：实际花费 vs 不治理的基线花费 → `saved_percent`；
- **按档位 / 按任务 / 按作用域**：钱花在哪个环节一眼可见；
- **每会话单价**（`per_session()`）：面试里最能体现"真做过生产系统"的口径 ——
  它能把"这个月成本涨了"定位到具体某类会话。

`replay(thread_id)` 返回会话内的事件流（route / cache_hit / trims / fallback / error），
用来回答"这通对话到底发生了什么"。**回放只读，不改变任何状态**。

---

## 四、评测与 CI 准入门禁

### 判分顺序（很重要）

```
① 合规红线（禁止词）   命中 → 0 分，一票否决，不给任何翻盘机会
② 结构化断言          如 expect_tier=small：断言的是"行为有没有回退"，不是"答得好不好"
③ 拒答正确性          该拒答不拒答 = 疑似幻觉
④ 关键词命中          all 模式按完成度给部分分；any 模式命中即满分
⑤ LLM-as-Judge        仅对"措辞是否得体"补充打分，且必须配双盲校准
```

关于 ④ 的模式差异：`any` 模式的多个关键词通常是**同一事实的不同写法**
（`600000` / `600,000`），按命中率算分会让完全正确的回答只拿 0.5 —— 分数就失真了。
所以 `any` 只给 0 / 1，`all` 才给完成度。

### 准入判据（`EvalRunner.compare`）

| 判据 | 处理 |
|---|---|
| 整体加权得分退化超阈值 | 拦（阈值可配，默认 0.02） |
| **单条用例从"过"变"挂"** | **无论阈值多宽都拦** —— 功能回归不能被平均分稀释 |
| 命中合规红线 | 直接拦，不看总分 |

### 基线顺序（最容易写错的地方）

```python
baseline = runner.baseline_from_db(store, scene)   # ① 先取基线
current  = runner.run(cases, fn, scene=scene)      # ② 再跑本次
decision = runner.compare(current, baseline)
```

反过来的话，本次结果已经落库，"基线"就变成了自己 —— gate 永远通过。
这是这类系统最隐蔽也最致命的 bug，所以有一条专门的测试锁住它。

### 评测目标是注入的（解耦证明）

```python
# app_context.py（装配层）
gateway_service.targets = {
    "policy_qa": _qa,        # → product.answer_question
    "refusal":   _qa,
    "compliance":_qa,
    "routing":   _routing,   # → gateway.route_preview（断言档位）
    "material":  _material,  # → marketing.generate_materials（断言素材结构）
}
```

`gateway` 包里没有任何一行 `import product_assistant / marketing`，
并且有机器化校验测试（`test_gateway_does_not_import_business_packages`）防止后人破坏这条边界。

---

## 五、接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/gateway/stats` | 网关卡口运行态（调用数 / 缓存命中率 / 各档位分布 / 节省比例） |
| GET | `/gateway/scenes` | 评测场景与用例数 |
| POST | `/gateway/route/preview` | 路由预览：这次请求会走哪档、为什么 |
| POST | `/gateway/demo` | 走一次真实网关调用（用于压测成本治理效果） |
| GET | `/gateway/costs` | 成本报告（总量 / 按档位 / 按任务 / 每会话单价） |
| GET | `/gateway/replay/{thread_id}` | 会话决策轨迹回放 |
| POST | `/gateway/eval` | 跑一轮评测 |
| POST | `/gateway/eval/gate` | 跑评测 + 与基线比 → 能否发布 |
| GET | `/gateway/eval/runs` | 历史评测运行记录（基线的来源） |
| POST | `/gateway/calibrate` | Judge 与人工双盲一致性校准 |

平台上还注册了 6 个工具供 Agent 调用：
`gateway.route_preview` / `cost_report` / `replay` / `stats` / `run_eval` / `gate_eval`。

---

## 六、配置项

```ini
GATEWAY_ENABLED=false                       # 关掉后业务直接走原始 LLM，不受影响（默认关）
GATEWAY_SQLITE_PATH=.data/gateway.sqlite

# 分档与路由
GATEWAY_MODEL_SMALL=qwen-turbo
GATEWAY_MODEL_MEDIUM=qwen-plus
GATEWAY_MODEL_LARGE=qwen-max
GATEWAY_DEFAULT_TIER=medium
GATEWAY_TIER_THRESHOLD_MEDIUM=0.35
GATEWAY_TIER_THRESHOLD_LARGE=0.70
GATEWAY_BASELINE_TIER=large                 # 成本基线（"不治理"的对照），用于算节省比例

# 价格表（¥/1K tokens，输入/输出）—— 演示估值，生产按实际账单校准
GATEWAY_PRICE_SMALL_IN=0.0008   GATEWAY_PRICE_SMALL_OUT=0.0020
GATEWAY_PRICE_MEDIUM_IN=0.0040  GATEWAY_PRICE_MEDIUM_OUT=0.0120
GATEWAY_PRICE_LARGE_IN=0.0200   GATEWAY_PRICE_LARGE_OUT=0.0600

# 缓存
GATEWAY_CACHE_TTL_SECONDS=900
GATEWAY_PREFIX_CACHE_MAX_ENTRIES=512
GATEWAY_SEMANTIC_CACHE_MAX_ENTRIES=512
GATEWAY_SEMANTIC_CACHE_THRESHOLD=0.94

# 限流 / 配额 / 密钥
GATEWAY_RATE_LIMIT_PER_MINUTE=600
GATEWAY_RATE_LIMIT_BURST=120
GATEWAY_QUOTA_TOKENS_PER_SCOPE=2000000
GATEWAY_API_KEYS=                            # 逗号分隔多密钥；留空则用 LLM_API_KEY
GATEWAY_KEY_COOLDOWN_SECONDS=60

# 上下文裁剪
GATEWAY_MAX_CONTEXT_TOKENS=6000
GATEWAY_KEEP_RECENT_TURNS=6
GATEWAY_SUMMARIZE_CHARS=200

# 评测闭环
GATEWAY_EVAL_MAX_REGRESSION=0.02            # CI 准入允许的退化幅度
GATEWAY_EVAL_GOLDEN_PATH=                   # 外部 Golden Set（jsonl），留空用内置
```

> ⚠️ 阈值与权重都是**实测校准**出来的（离线 local-hash embedding + 内置语料）。
> 换真实 Embedding 后 `GATEWAY_SEMANTIC_CACHE_THRESHOLD` 必须重新探测 ——
> 语义缓存的阈值错了不是"少省点钱"，而是"把错误答案发出去"。

---

## 七、测试（57 个用例）

```
tests/test_gateway.py
```

覆盖：路由（轻任务走小档 / 长文走大档 / 任务保底 / 降级链单调不升档 / 强制档位）、
缓存（键稳定性 / TTL 过期 / 容量淘汰 / 语义阈值 / 按任务策略 / 命中记账 cost=0）、
裁剪（去重 / 工具摘要 / 分层 / system 必留 / 摘要严格小于原文）、
限流与配额（令牌桶补充 / 多租户隔离 / **配额耗尽 fail-closed**）、
密钥池（轮转 / 冷却 / 空池）、降级链（向下退成功 / 全链失败抛错并记账）、
成本台账（节省口径 / 按任务聚合 / 每会话单价）、观测（回放 / 预览不改变状态）、
评测（红线一票否决 / 拒答 / 结构化断言 / 双盲校准 / 异常转失败用例 / CI 准入 /
合规硬拦 / 容忍阈值只作用于整体分 / 新失败用例必拦 / 基线顺序 / 落库复用）、
以及架构约束的机器化校验。
