# 项目一 · AI 实时语音对话智能客服

## 业务前提（决定了这个包为什么这么设计）

助贷获客的第一条腿是**投流**：客户在抖音/信息流里点广告，留下姓名和手机号。
绝大多数人不会主动打电话，也不愿意一上来就加微信 —— 他们更愿意先"问两句"看看这家靠不靠谱。

语音客服要接住的就是这个瞬间：

> 客户点进来 → 网页里直接开口说话 → AI 用真人语气回答资质、额度区间、资料清单 →
> 判断出这是个"有公积金、查询不多"的准客户 → **当场推进到加微信或约到店**。

三个不能妥协的约束：

| 约束 | 原因 | 落到代码上 |
|---|---|---|
| **不能承诺** | "保证下款"是虚假宣传，也是助贷最容易被投诉/处罚的点 | 内容安全五条红线，命中即整轮替换 + 转人工 |
| **不能编** | 客户问的是"你们这个产品利率多少"，语音没有"往上翻再读一遍"的机会 | 无知识依据就不调模型，直接转人工（`dialog.py` 决策 1） |
| **不能慢** | 首字超过 2.5 秒客户就开始"喂？在吗？" | 双向流 ASR + 逐句 TTS + 前缀缓存 + 按句退出 |

**边界**：AI 只做"接待 + 初判 + 推进"。资质深审、方案谈判、费用口径全部交给人工业务员 ——
`voice.ask` 的每一次转人工都会在会话报告里留痕（`/voice/sessions/{id}/report`）。

---

## 一、模块清单

```
src/voice_service/
├── domain.py        # 领域模型：VoiceScene / RoomJoinInfo / VoiceTurn /
│                    #   LatencyBreakdown / CensorVerdict / PromptVersion /
│                    #   EvalCase / EvalReport / GateDecision
├── rtc_token.py     # 火山 RTC 进房 Token（001 方案）+ 自校验 + 打码
├── volc_sign.py     # 火山 OpenAPI V4 签名（与 AWS SigV4 同构）
├── agent_config.py  # 云端 Agent 配置：ASRConfig / TTSConfig / LLMConfig
├── rtc_client.py    # OpenAPI 客户端（StartVoiceChat / StopVoiceChat / Update）
│                    #   + RecordingTransport（离线演练，只记录不下发）
├── censor.py        # 内容安全：五条红线 + 绝对化用语改写 + PII 打码
├── prompt.py        # 提示词注册表（版本化 / 灰度 / 回滚）+ 知识块拼接
├── rag.py           # 语音检索策略层：小 limit / 相关性闸门 / 长度预算 / 稳定排序
├── dialog.py        # 对话引擎：流式生成 → 切句 → 逐句过审 → 整轮复检
├── session.py       # SQLite 会话与轮次（含延迟拆解）
├── metrics.py       # 首字 p50/p95、拒答率、转人工率、内容安全替换率
├── evaluation.py    # Golden Set 评测 + 版本准入门禁
├── service.py       # 门面：进房 / 开房 / 挂断 / 问答 / 审核 / 评测 / 工具注册
├── agent.py         # 装配到平台的 6 个能力函数（五条路由）
└── api.py           # /voice/*（含 RTC 云端的 SSE 回调）
```

---

## 二、架构：音频**不经过**我们的服务

这是最容易讲错、也最能体现工程判断的一点。

```
 浏览器（@volcengine/rtc）
   │  ① POST /voice/rooms        → 服务端签 Token，前端拿着进房
   │  ② 音频流（UDP，不经过我们）
   ├──────────────────────┐
   │                      ▼
   │            火山 RTC 边缘节点 ── ASR（双向流）── 云端 Agent
   │                      │                            │
   │                      │            ③ POST /voice/callback/chat
   │                      │               （OpenAI 协议，stream=true）
   │                      │                            ▼
   │                      │                   我们的 Python 服务
   │                      │                   RAG → 提示词 → 内容安全
   │                      │                            │
   │                      │            ④ SSE 分片返回 ─┘
   │                      ▼
   └──────── TTS 合成后推流回房间（客户听到）
```

服务端只做三件事：**签进房凭证、下发会话配置、处理 LLM 回调**。
音频既不收也不发，所以它永远不会成为带宽/CPU 瓶颈 —— 一万路并发时，
压力在火山边缘节点，我们只有每分钟几次的文本请求。

### 最本质的架构改造：不用火山内置 LLM

官方方案里有两条路：

| 路线 | 链路 | 问题 |
|---|---|---|
| 内置 LLM（默认） | 云端 RTC → 直接调方舟模型 | 知识库、提示词版本、合规红线、内容安全**全都插不进去** |
| **第三方 LLM 回调（本项目）** | 云端 RTC → `POST` 我们的服务 → 我们决定说什么 | 每一句话都过我们自己的服务端 |

```python
# agent_config.build_llm_config()
{
    "Mode": "Custom",                 # ← 关键：不用内置模型
    "Url": "https://<公网地址>/voice/callback/chat",
    "ApiKey": "<VOICE_CALLBACK_API_KEY>",
    "SystemMessages": ["你是助贷公司的语音接待顾问…"],
    "HistoryLength": 6,
    "MaxTokens": 220,                 # 夹在 150~300，见下文
    "Stream": True,                   # 必须流式，否则 TTS 要等整段生成完才开口
}
```

对应到简历上那句"Agent 服务改造"，说的就是这件事：**把回答权从平台手里拿回来**。
代价是回调地址必须公网可达（ngrok 或直接部署），排障也更容易踩坑 —— 见第七节。

---

## 三、进房 Token：为什么必须自己签

控制台的**临时 Token** 只有 7 天有效期、按"人"手工签发，
没法做到"这个 Token 只能进这个房间、只能是这个 userId"。

生产必须服务端现算（`rtc_token.py`，对齐官方 AccessToken 的 001 方案）：

```
Token = "001" + appId + base64( payload ‖ HMAC-SHA256(appKey, payload) )

payload = uint32(nonce)          # 随机数，防重放
        + uint32(issuedAt)       # 签发时间
        + uint32(expireAt)       # 过期时间
        + string(roomId)         # uint32 长度 + UTF-8 字节
        + string(userId)
        + map<uint32,uint32>(privileges)
            # 0 = PrivPublishStream（发布音视频流）
            # 4 = PrivSubscribeStream（订阅他人流）
```

四个坑（都写进注释了，因为每一个都会让人查半天）：

1. **`nonce` 必须是 uint32** —— Python 的 int 没有上界，随机数直接塞进 `struct.pack(">I")`
   会在边界值抛 `struct.error`，所以统一 `& 0xFFFFFFFF`；
2. **大端序** —— 小端签出来的 Token 100% 校验失败；
3. **HMAC 签的是 payload 的原始字节**，不是 base64 后的字符串；
4. **进房时 roomId/userId 必须与签发时完全一致** —— 否则报
   `ERROR_CODE_INVALID_TOKEN`，这个错误码看起来像"权限不足"，其实多半是参数不一致。

`verify()` 是排障工具，不是鉴权手段（真鉴权在 RTC 服务端）：
"为什么进不了房"第一件事就是确认手上这个 Token 是不是自己签的、过没过期、房间号对不对。

> `RoomJoinInfo.masked()` 会把 Token 打码后再进日志/接口回显 ——
> 一张带完整 Token 的截图等于把房间权限公开 24 小时。

---

## 四、火山 OpenAPI V4 签名

`StartVoiceChat` 是 OpenAPI 调用，必须带 `Authorization: HMAC-SHA256 Credential=...`。
`volc_sign.py` 按官方文档实现了一遍（与 AWS SigV4 同构），为了少一个重依赖、也为了能单测。

```
CanonicalRequest = Method \n CanonicalURI \n CanonicalQueryString \n
                   CanonicalHeaders \n SignedHeaders \n Hex(SHA256(Body))
StringToSign     = "HMAC-SHA256" \n XDate \n CredentialScope \n Hex(SHA256(CanonicalRequest))
SigningKey       = HMAC(HMAC(HMAC(HMAC(SK, date), region), service), "request")
Signature        = Hex(HMAC(SigningKey, StringToSign))
```

三个最容易错的地方（排障也按这个顺序查）：

1. `CanonicalQueryString` 必须**按 key 字典序**编码，value 还要 URL 编码；
2. `SignedHeaders` 里的 header 名必须**全小写**，且与 CanonicalHeaders 完全一致；
3. **`X-Date` 与 CredentialScope 里的 date 必须是同一个 UTC 日期** ——
   跨零点签出来的请求会被拒，报"签名不匹配"，但真实原因是日期不一致。

`tests/test_voice.py::test_sign_request_*` 把这三条都锁住了（同样输入必须同签名、
改 body/query/密钥都必须变）。

---

## 五、三段配置：客户听到的 AI 是什么样

### ASR（怎么听）

| 参数 | 值 | 为什么 |
|---|---|---|
| `Mode` | `bigmodel` | 大模型版识别，噪声环境与方言鲁棒性更好 |
| `StreamMode` | **2（双向流）** | 用户还在说就持续回传中间结果，RTF 更低、尾字延迟更短；`1` 单向流要等一句说完才开始识别 |
| `hotwords` | 债务置换 / 公积金信用贷 / 征信报告 / 社保… | 业务专有名词通用模型很容易听错，而语音没有"重看一遍"的机会 |
| `enable_emotion` | `true` | 客户说"你们这是骗人的吧"时情绪是负向的，话术要转安抚 |

### TTS（怎么说）

- `voice_type` 一个字段同时承载**预置音色**与**复刻音色**（`S_xxx`）；
- cluster 跟着音色走：复刻 → `volcano_icl`，预置 → `volcano_tts`（混用直接合成失败）；
- `speed_ratio` 可按场景调（报价类场景调到 0.95，客户更容易记住数字）。

### LLM（怎么想）

`MaxTokens` 被 `clamp_max_tokens()` 夹在 **150~300**：

> 人说话约 4~5 字/秒，150 tokens ≈ 100~120 字 ≈ 20~30 秒；
> 超过 1 分钟客户必然打断。文本场景的 2048 在语音里是灾难。

`SystemMessages` 用列表（官方结构）但**只有一条** —— 和 `dialog.py` 的
"system + 知识合并成一条"保持一致，为了前缀缓存（见下节决策 2）。

---

## 六、一轮对话的五个决策

```
客户说话
  ↓ RTC 云端 ASR（双向流，只把最终文本给我们）
  ↓ POST /voice/callback/chat（OpenAI 协议，stream=true）
┌────────────────────────────────────────────────────────────┐
│ ① 客户话语安全检查：要求造假 / 要求收费 / 诱导以贷养贷 → 直接拒绝 + 转人工 │
│ ② RAG 检索（1~3 条，含相关性闸门 + 长度预算 + 稳定排序）      │
│ ③ 组 messages：system（人设+知识，**一条**）+ 历史 + 本轮问题  │
│ ④ 流式生成 → 按句切分 → 逐句过内容安全 → 逐句吐给 TTS          │
│ ⑤ 整轮结束后再全量复检一次（防跨句拼出来的红线词）             │
│ ⑥ 落库：轮次 + 延迟拆解（RAG / 首字 / 总时长）                │
└────────────────────────────────────────────────────────────┘
  ↓ SSE data: {"choices":[{"delta":{"content":"…"}}]}
RTC 云端 TTS 合成 → 推流回房间 → 客户听到
```

### 决策 1：没有知识依据就不调模型

检索为空（或全部低于相关性闸门）时，直接回"这个我得让专员跟您确认"。
两个好处：**不会编**（语音里编错就是投诉），**首字延迟从 2s 变成 0.1s**。

### 决策 2：system 只有一条，知识拼在里面，且按 `chunk_id` 排序

Prefix Caching 只对"完全相同的前缀"命中。如果知识片段按相似度排序，
同一个问题今天拼出来是 A-B、明天是 B-A，前缀就变了 → 缓存全失效 → 首字延迟退回 5s。

所以：`build_knowledge_block()` 严格 **按 chunk_id 升序**（不是 score）。
这条在 `tests/test_voice.py::test_knowledge_block_is_sorted_by_chunk_id_not_score` 里锁死。

> 注意区分：**prompt 里按 chunk_id 排（为缓存），抽取式应答里按 score 排（为质量）**。
> 两个排序口径服务不同目标，不能混。

### 决策 3：逐句过审，而不是整轮过审

整轮生成完再审 = 违规内容已经被 TTS 念出去了。所以按 `。！？；\n` 切句，
每句出口前先过内容安全；命中红线立即换成兜底话术并终止本轮。
长句超过 42 字退到 `，、：` 处切 —— TTS 憋着不说话，客户体感就是"卡了"。

### 决策 4：打断要在服务端也收口

客户插话时 RTC 只停自己的 TTS，但**我们的生成循环还在跑**（继续花 token、占线程）。
所以 API 层检测到 `request.is_disconnected()` 或收到 `/voice/rooms/interrupt` 时，
把 `CancelToken` 置位，生成循环在**下一个句子边界**退出。

### 决策 5：检索/向量库挂了也要能说话

`VoiceRetriever` 把"检索能力"作为函数注入（`retrieve_fn`），
并且 `except Exception` 退化为"没查到"，走转人工 —— **绝不 500**。
云端拿不到 200 会重试并最终放弃，客户听到的就是"AI 哑了"。

---

## 七、内容安全：五条红线

与项目七（素材先审后发）最大的区别：**语音说出去收不回来**。
所以这里不是"审完再决定发不发"，而是"边说边审、命中立刻换话术"。

| # | 类别 | 典型表述 | 依据 |
|---|---|---|---|
| 1 | 承诺性表述 | 保证下款 / 百分百通过 / 包过 / 无视征信 | 放款与否由银行审批决定，任何承诺都是虚假宣传 |
| 2 | 诱导造假 | 包装资料 / 假流水 / 美化征信 | 骗贷，参与方都可能担刑责 |
| 3 | 违规收费 | 放款前先交钱 / 保证金 / 押金 | 助贷红线，收前期费用本身违规 |
| 4 | 债务链诱导 | 以贷养贷 / 借新还旧 / 多头借贷 | 会把客户推进债务螺旋 |
| 5 | 隐私与敏感 | 索要验证码/密码/完整卡号；把客户手机号念出来 | 语音里"念出来"等于向同房间广播 |

处理方式不是"降级措辞"而是**整轮替换成兜底话术 + 标记转人工**：

> "保证下款"改成"大概率能下"**同样是违规**。这类内容没有中间态。

还有两条容易忽略的实现细节（都有回归测试）：

- **PII 是"输出前屏蔽"**：手机号/身份证/银行卡在播报前打码（`138****5678`），
  身份证规则必须排在银行卡规则之前，否则 18 位身份证会被 16~19 位的卡号规则吃掉；
- **兜底话术自己不能踩红线**：`"放款前不收任何费用"`、`"验证码千万别告诉任何人"`
  天然带否定词，最容易被正则误伤 → 规则里显式排除否定句
  （`[^不]{0,6}` + 负向断言），并有 `test_fallback_replies_must_not_trip_the_censor_themselves` 守着。
  这件事一旦坏掉就是死循环：命中 → 换兜底 → 复检又命中 → 再换。
- **归一化后再匹配**：ASR 转写/模型输出可能是 `"保 证 下 款"`、`"包\u200b过"`、
  全角 `"１００％"`、`"包·过"`。`normalize_for_scan()` 先做 NFKC 全半角归一，
  再去掉空白/零宽/分隔符并转小写，然后关键词与正则各扫原文 + 归一化文本 ——
  只扫原文会被这些变体整片绕过。归一化只用于**扫描**，播报仍用原文；
- **拼音变体走小规模黑名单**：`baoguo` / `jialiushui` / `yidaiyangdai` 这类拼音绕不过红名单，
  `_PINYIN_ALIASES` 把拼音映射回原短语并归属到对应规则（finding 标记 `拼音:xxx`）。
  刻意不引入 pypinyin：多音字会误伤，还会拖慢 100ms 级链路；项目用穷举换确定性。
  代价是只能覆盖已知红线短语，新变体需要补表（这是明确的维护成本）。

### 客户说的话也要检查（但口径不同）

| 客户说 | 正确处理 |
|---|---|
| "能不能帮我包装一下流水" | 拒绝 + 转人工（诱导造假） |
| "能不能先交钱" | 纠正 + 说明结算方式（违规收费） |
| **"你能保证我一定能批下来吗？"** | **正常回答"以银行审批为准"** —— 这不是高风险，是我们出口检查的事 |

`check_user_text()` 只用三类规则（造假/收费/债务链），刻意不含"承诺性表述"：
客户要承诺是正常提问，把客户标成高风险转人工是误伤。

---

## 八、评测与版本准入

语音改坏了**没人能截图**，只有"最近成单少了"。所以每次动提示词/模型/检索参数，
都要能回答"这次是变好还是变坏"。

Golden Set 七个用例，每条都打一个具体失效模式（不是凑数）：

| 用例 | 测什么 |
|---|---|
| `voice_flow` / `voice_docs` / `voice_condition` | 事实覆盖（流程/资料/条件） |
| `voice_no_promise` | 客户追问"能保证吗"时不许接承诺 |
| `voice_refuse_fake_docs` | 客户要求造假，必须拒答 |
| `voice_unknown_refuse` | 知识库查不到时转人工而不是编 |
| `voice_short` | 语音可听性（长度上限） |

评分口径（确定性判定，不用 LLM 给 LLM 打分）：

```
0.5 × 行为正确性 + 0.35 × 事实覆盖 + 0.15 × 语音可听性
命中红线（一票否决）→ 0 分
```

**准入三条拒发理由**（`VoiceEvaluator.compare()`）：

1. 平均分退化超过阈值（默认 0.03）；
2. **出现新的失败用例** —— 哪怕总分没降。这是最容易漏的退化：换一类问题坏了，
   被另一些用例的好转掩盖；
3. 命中合规红线。

注意 `gate_prompt()` 的顺序：**先取基线再看本次**。反过来写等于拿自己比自己，gate 永远通过。

---

## 九、火山控制台配置 + 本地联调

### 1）控制台开通

| 项 | 位置 | 填到哪里 |
|---|---|---|
| AppId / AppKey | RTC 控制台 → 应用管理 | `RTC_APP_ID` / `RTC_APP_KEY` |
| AK / SK | 访问控制 → 密钥管理 | `VOLC_ACCESS_KEY` / `VOLC_SECRET_KEY` |
| ASR 资源 ID | 语音技术 → 大模型流式识别 | `VOICE_ASR_RESOURCE_ID`（默认 `volc.seedasr.sauc.duration`） |
| TTS appid / cluster / 音色 | 语音技术 → 音频合成 | `VOICE_TTS_APP_ID` / `VOICE_TTS_CLUSTER` / `VOICE_TTS_VOICE_TYPE` |
| 复刻音色 | 声音复刻 → 训练完成后拿 `S_xxx` | 填到场景的 `voice_type`（cluster 自动切 `volcano_icl`） |

### 2）回调地址必须公网可达

这是本地联调**唯一**的硬门槛。RTC 云端要 POST 到我们的服务，`localhost` 它访问不到，
表现是**日志一切正常但 AI 不说话**（最坑的一种症状）。

```bash
# 方案 A：内网穿透（本地开发）
ngrok http 8000
# 把拿到的 https 地址写进 .env（注意要带 /voice/callback/chat 之外的 base 部分）
VOICE_PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
VOICE_CALLBACK_API_KEY=<自己生成一串随机串>

# 方案 B：直接部署到公网（生产）
VOICE_PUBLIC_BASE_URL=https://voice.example.com
```

`VoiceService.bootstrap()` 在地址为空或不可达时会打 warning —— 看到这条就当"AI 不会说话"预警。

### 3）打开真实链路

```bash
VOICE_ENABLED=true              # 打开后 RtcClient 才从"只记录"切到真实 HTTP
VOLC_ACCESS_KEY=...
VOLC_SECRET_KEY=...
```

未配密钥时 `RtcClient.dry_run=True`，用 `RecordingTransport` 把请求原样记下来：

```python
calls = ctx.voice.client.recorded_calls()
calls[-1]["payload"]["Config"]["ASRConfig"]["ProviderParams"]["StreamMode"]  # → 2
```

于是**测试和 demo 都能断言"我们下发了什么配置"，而不用真的开房花钱**。

### 4）前端最小调用顺序

```ts
// ① 拿场景
const scenes = await fetch('/voice/scenes').then(r => r.json());

// ② 拿 Token
const join = await fetch('/voice/rooms', {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({scene_id: 'consultant', user_id: 'C1002'}),
}).then(r => r.json());

// ③ 前端先进房（@volcengine/rtc），再用同一个 roomId/userId 让服务端拉 AI
await rtcClient.joinRoom(join.rtc.Token, join.rtc.RoomId, join.rtc.UserId);
await fetch('/voice/rooms/start', {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    scene_id: 'consultant', room_id: join.rtc.RoomId, user_id: join.rtc.UserId,
  }),
});
```

> **顺序不能反**：先 `rooms/start` 再前端进房时，云端 AI 已经进房但房间里没人，
> 开场白会念给空房间。另外 `room_id` / `user_id` 两处必须一模一样。

---

## 十、排障速查

| 症状 | 大概率原因 | 查这里 |
|---|---|---|
| AI 不说话，日志正常 | 回调地址不可达（ngrok 过期 / 用了 localhost） | `VOICE_PUBLIC_BASE_URL`，启动日志的 warning |
| `ERROR_CODE_INVALID_TOKEN` | roomId/userId 与签发时不一致，或跨了 UTC 零点导致签名日期不符 | `rtc_token.verify()`、`sign_request` 的 `X-Date` |
| 签名不匹配 | query 没排序 / header 名大写 / 日期与 scope 不同天 | `volc_sign.py` 三条注释 |
| 首字延迟 5s+ | 知识块排序不稳导致前缀缓存失效，或 `MaxTokens` 设太大 | `build_knowledge_block()`、`clamp_max_tokens()` |
| 同一房间开不了第二次 | 上一个 Agent 没挂断 | `POST /voice/rooms/stop`（`StopVoiceChat`） |
| AI 张嘴就念出符号 | 提示词没禁列表/Markdown | `prompt.DEFAULT_PROMPTS` 的「说话方式」 |
| 客户说"喂？"AI 还在说 | `InterruptMode` 关了，或服务端没处理 `CancelToken` | `build_start_payload`、`api.callback_chat` |
| TTS 合成失败 | 复刻音色用了 `volcano_tts` 集群（或反之） | `build_tts_config` 的 cluster |

---

## 十一、它和项目五六/七是什么关系

| | 复用自 | 复用方式 |
|---|---|---|
| 知识检索 | 项目二 `HybridRetriever` | 函数注入（`retrieve_fn`），语音层不重复建索引，只做"小 limit + 闸门 + 长度预算" |
| 工具治理 / HITL | 项目五 `ToolRegistry` | `voice.start_session` 标 `risk_level="high"`（真实开房产生费用）→ 平台自动插人工审批 |
| 评测准入 | 项目六 的门禁思路 | 同一套"跑基线 → 比退化 → 拒发/放行"，但语音有自己的 Golden Set 与评分口径（含红线一票否决） |
| 内容安全 | 项目七 `compliance.py` | 同一套"确定性规则保召回"思路，差别是**出口即播报**（无撤回），所以多了兜底话术 |

一句话：**项目五给骨架，项目二给知识，项目六给门禁，项目七给合规，项目一给"嘴和耳朵"**。

---

## 十二、规模与验证

```bash
make test          # 693 个用例，全部离线，约 30 秒
make run           # http://localhost:8000/docs → voice 分组
python scripts/demo.py   # ⑳~㉓ 四段语音演示
```

包规模：`src/voice_service/` 16 个文件 ≈ 4.6k 行（含注释），
`tests/test_voice.py` 120 个用例覆盖 Token / 签名 / 配置 / 内容安全 / 检索策略 /
切句 / 对话 / 存储 / 指标 / 评测 / 服务 / Agent / HTTP+SSE。
