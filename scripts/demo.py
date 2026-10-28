"""端到端演示脚本 —— 一次跑通六大服务的核心链路。

    python scripts/demo.py

演示内容：
    项目五 编排平台：Agent 装配、作用域裁剪、HITL 中断恢复、预算熔断
    项目二 产品助手：政策问答（可溯源 / 拒答）、结构化方案匹配、方案书生成
    项目三 话术陪练：模拟客户对抗、四维评分（含一票否决）、全量质检、话术挖掘
    项目七 素材推广：三渠道语感适配、先审后发、脱敏案例、客户激活、效果归因
    项目六 推理网关：复杂度路由、精确缓存、成本台账、会话回放、评测与 CI 准入
    项目一 语音客服：进房凭证、云端配置下发、文本模拟一轮、内容安全、评测准入
    项目八 流程数字员工：SOP 漏跟进预警、到店前一页纸、HITL 发合同、自助查进度、评测准入

不需要任何 API Key（默认 LLM_PROVIDER=echo 离线确定性模式）。
语音部分默认走 dry-run（RecordingTransport）：只记录下发了什么，不真的开房花钱。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json  # noqa: E402

from agent_platform.config import get_settings  # noqa: E402
from agent_platform.llm.base import Message  # noqa: E402
from app_context import build_app_context  # noqa: E402
from gateway.trim import trim_messages  # noqa: E402


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def main() -> None:
    settings = get_settings()
    required = [
        settings.policy_abs_dir / "products.json",
        settings.marketing_abs_dir / "blueprints.json",
    ]
    if not all(p.exists() for p in required):
        print("⚠️  未找到演示数据，正在自动生成……")
        import runpy

        runpy.run_path(str(Path(__file__).parent / "seed_data.py"), run_name="__main__")

    ctx = build_app_context()
    summary = ctx.summary()

    hr("① 平台装配总览（项目五）")
    print(f"  LLM 供应商        : {summary['llm_provider']}（离线确定性，无需密钥）")
    print(f"  向量化            : {summary['embedding_provider']}")
    print(f"  状态持久化         : {summary['checkpointer']}")
    print(f"  已装配 Agent      : {', '.join(summary['agents'])}")
    print(f"  工具生态          : {summary['tools']} 个")
    print(f"  产品知识库         : {summary['product']}")
    print("\n  各 Agent 装配信息：")
    for info in ctx.runtime.registry.describe_all():
        print(f"    · {info['name']}｜planner={info['has_planner']} critic={info['has_critic']}"
              f"｜工具 {info['tool_count']} 个")

    # ------------------------------------------------------------------ #
    hr("② 项目二：产品政策问答（可溯源 / 无依据拒答）")
    q = "测试公积金信用贷的利率是多少？准入条件是什么？"
    print(f"  业务员问：{q}")
    answer = ctx.product.answer_question(q)
    print(f"  是否拒答：{answer.refused}")
    print("  回答：")
    for line in answer.answer.splitlines():
        print(f"    {line}")
    print(f"  引用溯源：{answer.citations}")
    print(f"  关联产品（图谱）：{answer.related_products}")

    print("\n  再问一个无政策依据的问题：")
    q2 = "帮我推荐一只基金，明天会涨吗？"
    print(f"  业务员问：{q2}")
    ans2 = ctx.product.answer_question(q2)
    print(f"  是否拒答：{ans2.refused}  ← 合规底线：没有依据就不回答")
    print(f"  理由：{ans2.reason}")

    # ------------------------------------------------------------------ #
    hr("③ 项目二：结构化方案匹配（资质 × 征信 × 需求 → 产品组合）")
    customer = "C1001"
    print(f"  客户 {customer}：{json.dumps(ctx.product.profiles.get(customer).model_dump(mode='json'), ensure_ascii=False)[:150]}…")
    match = ctx.product.match_customer(customer)
    print(f"\n  匹配结果：matched={match.matched}")
    print("  " + match.to_text().replace("\n", "\n  "))
    print(f"\n  被硬性条件淘汰的产品数：{len(match.rejected_summary)}")
    for r in match.rejected_summary[:4]:
        print(f"    ✗ {r['product_name']}：{r['reason']}")

    hr("④ 项目二：方案书生成（分钟级、带合规免责）")
    proposal = ctx.product.generate_proposal(customer)
    print(proposal.to_markdown())

    # ------------------------------------------------------------------ #
    hr("⑤ 项目三：话术陪练（比价型客户，会抵抗不放水）")
    session = ctx.coaching.start_session("P-PRICE", "学员小陈")
    print(f"  客户开场：{session.messages[-1].text}\n")
    lines = [
        "李总您好，我是助贷顾问小陈。您需要 150 万经营周转，我先帮您做资质初审："
        "您有营业执照和房产，近半年征信查询大概多少次？",
        "我理解您关心费用，服务费按放款金额 3 个点、放款后才收；"
        "利率预估 3.85% 到 5.6%，以银行审批为准，我不做承诺。",
        "李总，周六上午 10 点您方便过来一趟吗？我当面给您算额度。",
    ]
    for said in lines:
        print(f"  学员：{said}")
        reply = ctx.coaching.reply(session.session_id, said)
        print(f"  客户：{reply['customer_reply']}\n")

    card = ctx.coaching.finish_session(session.session_id)
    print(card.to_text())

    hr("⑥ 项目三：违规话术 → 合规红线一票否决")
    bad = ctx.coaching.start_session("P-VAGUE", "学员小周")
    ctx.coaching.reply(bad.session_id, "您放心，我们包过，不看征信，保证下款！")
    bad_card = ctx.coaching.finish_session(bad.session_id)
    print(f"  总分 {bad_card.total_score}｜通过={bad_card.passed}｜一票否决={bad_card.vetoed}")
    print(f"  检出违规词：{[v.keyword for v in bad_card.violations]}")
    print(f"  改进建议：{bad_card.recommendations[:2]}")

    hr("⑦ 项目三：全量会话质检（覆盖率 100%）")
    reports = ctx.coaching.qc_all()
    for r in reports:
        flag = "⚠️ 需告警" if r.needs_alert else "正常"
        print(f"  {r.conversation_id}｜业务员 {r.agent}｜{r.score:.0f} 分｜"
              f"发现 {len(r.findings)} 条｜{flag}")
    worst = max(reports, key=lambda r: len(r.findings))
    print("\n  问题最多的会话详情：")
    print("  " + worst.to_text().replace("\n", "\n  "))

    hr("⑧ 项目三：高转化话术挖掘（回流题库）")
    mining = ctx.coaching.mine()
    print(f"  扫描对话 {mining.scanned} 段，产出有效话术 {len(mining.mined)} 条：")
    for p in mining.mined[:6]:
        print(f"    [{p.score:.2f}] {p.stage}｜{p.text[:46]}…")

    # ------------------------------------------------------------------ #
    hr("⑨ 项目五：HITL 人工审批闭环（告警主管是高风险动作）")
    run = ctx.runtime.invoke(
        "coaching",
        "对 CONV-0002 做合规质检并告警主管",
        context={"conversation_id": "CONV-0002", "supervisor": "张主管"},
    )
    print(f"  第一次调用状态：{run.status}")
    print(f"  工具调用：{[t['tool'] for t in run.tool_calls]}")
    if run.status == "interrupted":
        print("  待审批内容：")
        print("   " + json.dumps(run.pending_approval, ensure_ascii=False, indent=2).replace("\n", "\n   "))
        pending = ctx.runtime.approvals.pending()
        print(f"  审批中心待办：{len(pending)} 条")

        resumed = ctx.runtime.resume(
            run.thread_id, {"approved": True, "decided_by": "张主管", "comment": "已核实"}
        )
        print(f"\n  审批通过后状态：{resumed.status}")
        print(f"  告警已发送：{resumed.output.get('alert_sent')}")
        print(f"  审批留痕：{json.dumps(resumed.approvals, ensure_ascii=False)}")

    hr("⑩ 平台观测：指标与决策轨迹")
    metrics = ctx.runtime.metrics_snapshot()
    print("  计数器：")
    for k, v in list(metrics["counters"].items())[:10]:
        print(f"    {k} = {int(v)}")
    print("\n  直方图（延迟分布）：")
    for k, v in list(metrics["histograms"].items())[:6]:
        print(f"    {k} → count={int(v['count'])} avg={v['avg']}ms p95={v['p95']}ms")

    print("\n  最近一次运行的决策轨迹（span 级）：")
    for span in run.trajectory[:10]:
        print(f"    {span['name']:<10} kind={span['kind']:<5} {span['duration_ms']:>8.2f}ms  status={span['status']}")

    # ------------------------------------------------------------------ #
    hr("⑪ 项目七：素材生成 × 渠道语感适配（朋友圈 / 抖音 / 小红书）")
    print("  业务背景：公司没有内容自运营账号，获客第二条腿是业务员在自己账号发素材 ——")
    print("           所以素材必须“拿来就能发”，且语感要匹配渠道。")
    print("\n  渠道规格（字数上限 / 标签数 / 是否口播）：")
    for spec in ctx.marketing.channel_specs():
        print(f"    {json.dumps(spec, ensure_ascii=False)}")

    for channel, theme in (("moments", "债务置换"), ("douyin", "征信解读")):
        print(f"\n  ── 生成 {channel} / {theme} ──")
        # persist=False：演示只展示生成效果，不写库（否则每次跑演示素材数都会累积）
        for draft in ctx.marketing.generate_materials(channel, theme=theme, count=1, persist=False):
            print("  " + draft.to_text().replace("\n", "\n  "))

    # ------------------------------------------------------------------ #
    hr("⑫ 项目七：合规先审后发（能自动改的顺手改掉，改不动的拦截转人工）")
    bad_copy = "黑户也能做！百分百下款，保证下款不看征信，月供压力大想置换的私信我。"
    print(f"  业务员写的文案：{bad_copy}")
    verdict = ctx.marketing.audit(bad_copy)
    print("  " + verdict.to_text().replace("\n", "\n  "))
    print(f"\n  → 复查结论：可否发布 {verdict.passed}｜是否需要人工复核 {verdict.needs_human}")
    if verdict.sanitized_text:
        print(f"  → 自动改写后：{verdict.sanitized_text}")

    leaky = "客户张先生手机号 13900001111，征信有点花，先加微信慢慢聊。"
    print(f"\n  再看一条含客户隐私的私信：{leaky}")
    leak_report = ctx.marketing.audit(leaky)
    print("  " + leak_report.to_text().replace("\n", "\n  "))
    print(
        f"  → 可否发布 {leak_report.passed}｜需人工复核 {leak_report.needs_human}"
        "  ← 敏感信息无法自动改写，只能拦截 + 转人工，绝不“改一下就当没事”"
    )

    # ------------------------------------------------------------------ #
    hr("⑬ 项目七：成单案例脱敏包装（客户证言式素材）")
    case_drafts = ctx.marketing.pack_cases(channel="moments")
    print(f"  可包装案例 {len(case_drafts)} 条（只有成交对话能入库）")
    for draft in case_drafts[:1]:
        print("  " + draft.to_text().replace("\n", "\n  "))

    # ------------------------------------------------------------------ #
    hr("⑭ 项目七：未成单客户激活（对症匹配 + 防骚扰）")
    plan = ctx.marketing.plan_activation()
    print("  " + plan.to_text().replace("\n", "\n  "))

    # ------------------------------------------------------------------ #
    hr("⑮ 项目七：素材 → 触达 → 留资 → 成单 归因")
    attribution = ctx.marketing.attribute()
    print("  " + attribution.to_text().replace("\n", "\n  "))

    # ------------------------------------------------------------------ #
    hr("⑯ 项目七：HITL —— 把素材发给客户（高风险 + 非幂等，必须人工确认）")
    print("  设计要点：对外发送是“发出去就收不回”的动作，所以平台自动挂起等审批；")
    print("           超时策略是 abort/escalate，绝不自动放行。")
    send_run = ctx.runtime.invoke(
        "marketing",
        "激活这批沉睡客户，把素材发给他",
        context={"customer_id": "C1002"},
    )
    print(f"\n  第一次调用状态：{send_run.status}")
    print(f"  工具调用：{[t['tool'] for t in send_run.tool_calls]}")
    if send_run.status == "interrupted":
        print("  待审批内容（客户只会收到素材正文 + CTA，不含状态/质量分等内部信息）：")
        print("   " + json.dumps(send_run.pending_approval, ensure_ascii=False, indent=2).replace("\n", "\n   "))
        resumed_send = ctx.runtime.resume(
            send_run.thread_id, {"approved": True, "decided_by": "业务员小陈", "comment": "内容已核对"}
        )
        print(f"\n  审批通过后状态：{resumed_send.status}")
        print(f"  发送结果：{json.dumps(resumed_send.output, ensure_ascii=False)[:200]}")

    # ------------------------------------------------------------------ #
    hr("⑰ 项目六：成本治理三板斧（复杂度路由 / 缓存 / 上下文裁剪）")
    print("  ① 复杂度路由：轻任务走小模型，重任务才放大模型（省的是真金白银，不是感觉）")
    previews = [
        ("在吗", ""),
        ("帮我写一条朋友圈素材，主题是债务置换", "material"),
        ("请阅读以下材料并逐条分析风险，给出三套方案对比：" + "材料内容" * 200, "proposal"),
    ]
    for text, task in previews:
        route = ctx.gateway_service.route_preview(text, task=task)
        print(
            f"    [{task or '（默认）':<8}] 复杂度 {route['complexity']:.2f} → "
            f"{route['tier']} 档（{route['model']}）｜降级链 {route['fallback_chain']}"
        )
        print(f"               路由理由：{route['reason']}")

    print("\n  ② 缓存：同一个问题问两次，第二次直接命中，成本归零")
    first = ctx.gateway_service.demo_generate("写一条朋友圈素材", task="material", thread_id="demo-gw-1")
    second = ctx.gateway_service.demo_generate("写一条朋友圈素材", task="material", thread_id="demo-gw-1")
    for idx, item in enumerate((first, second), start=1):
        print(
            f"    第 {idx} 次：model={item['model']} tier={item['tier']} "
            f"cost=¥{item['cost']:.6f}（原始 ¥{item['baseline_cost']:.6f}）"
            f" 省 {item['saved_percent']:.0f}%｜缓存 {item['cache_status']}"
        )

    print("\n  ③ 上下文裁剪：去重 + 工具输出摘要化 + 历史分层压缩")
    trim_result = trim_messages(
        [Message(role="system", content="你是助贷业务助手，回答必须可溯源。")]
        + [Message(role="user", content=f"第 {i} 轮：客户在问费用怎么算" + "补充说明" * 30) for i in range(10)]
        + [Message(role="tool", name="query_policy", content=json.dumps({"rows": [{"k": "v"} for _ in range(80)]}, ensure_ascii=False))],
        max_tokens=400,
        keep_recent_turns=4,
    )
    print(f"    token：{trim_result.original_tokens} → {trim_result.final_tokens}"
          f"（省 {trim_result.saved_percent}%）")
    for action in trim_result.actions:
        print(f"    · {action}")

    # ------------------------------------------------------------------ #
    hr("⑱ 项目六：可观测（成本台账 / 每会话单价 / 会话回放）")
    cost = ctx.gateway_service.cost_report()
    total = cost["total"]
    print(f"  总调用 {total['calls']} 次｜实际 ¥{total['cost']:.6f}｜"
          f"不治理的基线 ¥{total['baseline_cost']:.6f}｜省 {total['saved_percent']}%")
    print(f"  缓存命中率：{cost['cache_hit_rate']:.1%}")
    print("  按档位：")
    for tier, agg in cost["by_tier"].items():
        print(f"    {tier:<7} 调用 {agg['calls']} 次｜¥{agg['cost']:.6f}")
    print("  每会话单价（最能体现成本是否可解释）：")
    for session, agg in list(cost["per_session"].items())[:5]:
        print(f"    {session:<16} ¥{agg['cost']:.6f}｜tokens {agg['tokens']}")

    replay = ctx.gateway_service.replay("demo-gw-1")
    print(f"\n  会话 demo-gw-1 回放：{replay['count']} 个事件")
    for event in replay["events"][:8]:
        print(f"    · {json.dumps(event, ensure_ascii=False)[:120]}")

    # ------------------------------------------------------------------ #
    hr("⑲ 项目六：评测 + CI 准入门禁（改 Prompt 必须过门槛，不靠感觉）")
    scenes_info = ctx.gateway_service.list_scenes()
    print(f"  评测场景：{scenes_info['scenes']}｜用例 {scenes_info['case_count']} 条")
    print(f"  已注入的评测目标：{scenes_info['available_targets']}")
    print("  （注意：网关包不 import 任何业务代码 —— 目标由装配层注入，见 app_context）")

    print("\n  跑一轮全场景评测：")
    evaluation = ctx.gateway_service.run_eval("all")
    print("  " + evaluation["report_text"].replace("\n", "\n  "))

    print("\n  与基线比对（CI 准入）：")
    gate = ctx.gateway_service.gate_eval("all")
    print(f"    本次得分 {gate['current']['score']:.4f}｜基线 "
          f"{gate['baseline']['score'] if gate['baseline'] else '（首跑）'}")
    print(f"    准入结论 allowed={gate['gate']['allowed']}｜delta={gate['gate']['delta']}")
    for reason in gate["gate"]["reasons"]:
        print(f"    · {reason}")

    print("\n  判官双盲校准（LLM-as-Judge 有偏，必须用人工抽检校准）：")
    print("   " + json.dumps(
        ctx.gateway_service.calibrate([8, 9, 7, 10, 6], [9, 8, 8, 9, 7]), ensure_ascii=False
    ))

    # ------------------------------------------------------------------ #
    hr("⑳ 项目一：语音客服 —— 进房凭证 + 下发云端配置")
    voice = ctx.voice
    scenes = voice.scene_list()
    print(f"  可用场景 {len(scenes)} 个：")
    for scene in scenes:
        print(f"    · {scene.scene_id:11s} {scene.name}｜上限 {scene.max_tokens} tokens"
              f"｜检索 {scene.knowledge_limit} 条")

    join = voice.join_info(scene_id="consultant", user_id="C1002")
    print("\n  签发进房凭证（客户端 joinRoom 直接用）：")
    print(f"    AppId={join.app_id}  RoomId={join.room_id}  UserId={join.user_id}")
    print(f"    Token={join.token[:28]}…（{len(join.token)} 字符，下面只在日志里打码）")
    verdict = voice._token_builder.verify(join.token)
    print(f"    自校验：valid={verdict.valid} room={verdict.room_id} user={verdict.user_id}")
    print(f"    对外输出（已脱敏）：{join.masked()['Token']}")

    started = voice.start_agent(scene_id="consultant", room_id=join.room_id, user_id="C1002")
    session_id = started["session"]["session_id"]
    print(f"\n  拉起云端 AI：session={session_id}  rtc_ok={started['rtc']['ok']}"
          f"  dry_run={started['rtc']['dry_run']}  ← 未配密钥时只记录不下发")
    print("  实际下发的配置（这段配置决定客户听到的 AI 是什么样）：")
    payload = voice.client.recorded_calls()[-1]["payload"]
    asr = payload["Config"]["ASRConfig"]["ProviderParams"]
    tts = payload["Config"]["TTSConfig"]["ProviderParams"]
    llm = payload["Config"]["LLMConfig"]
    print(f"    ASR  Mode={asr['Mode']} StreamMode={asr['StreamMode']}（2=双向流，尾字延迟更短）")
    print(f"         热词={json.loads(asr['VolcanoASRParameters']).get('hotwords', [])[:4]}…")
    print(f"    TTS  集群={tts['app']['cluster']} 音色={tts['audio']['voice_type'] or '（用默认/复刻音色）'}")
    print(f"    LLM  Mode={llm['Mode']}（第三方回调 → 我们自己的服务，RAG/提示词/内容安全才插得进来）")
    print(f"         Url={llm['Url'] or '（未配置 VOICE_PUBLIC_BASE_URL —— 真实开房前必须配公网地址）'}")
    print(f"         Stream={llm['Stream']}  MaxTokens={llm['MaxTokens']}  HistoryLength={llm['HistoryLength']}")
    print(f"         开场白={payload['AgentConfig']['WelcomeMessage']}")

    # ------------------------------------------------------------------ #
    hr("㉑ 项目一：文本模拟一轮（走真实 RAG + 提示词 + 内容安全，不花 RTC 的钱）")
    for question in [
        "公积金信用贷的办理流程是什么？",
        "公积金信用贷需要准备哪些资料？",
    ]:
        turn = voice.ask(question)
        print(f"  客户：{question}")
        print(f"  AI  ：{turn.answer_text}")
        print(f"        {turn.latency.as_row()}｜引用 {len(turn.citations)} 条｜"
              f"拒答={turn.refused} 转人工={turn.needs_human}")
        print()

    print("  查不到依据的问题 —— 不调模型，直接转人工（首字更快，也不会编）：")
    unknown = voice.ask("你们公司老板叫什么名字？")
    print(f"    AI：{unknown.answer_text}")
    print(f"    拒答={unknown.refused} 转人工={unknown.needs_human}")

    # ------------------------------------------------------------------ #
    hr("㉒ 项目一：内容安全（语音说出去收不回来，所以边说边审）")
    for text in [
        "我保证您能下款，百分百通过",
        "资料我帮您包装一下就行了",
        "您的手机号是 13812345678，我核对一下",
        "我们是本地最专业的机构",
    ]:
        report = voice.audit(text)
        blocked = report["passed"] is False
        print(f"  「{text}」→ {'拦截' if blocked else '通过（已改写）'}｜等级 {report['max_severity']}")
        if report["findings"]:
            hits = ", ".join(f"{f['rule_id']}({f['matched']})" for f in report["findings"])
            print(f"      命中：{hits}")
        if blocked:
            # 命中红线时这句不会被念出去，实际播报的是兜底话术
            print(f"      实际播报（兜底话术）：{report['safe_reply']}")
        else:
            print(f"      改写后播报：{report['sanitized_text']}")

    # ------------------------------------------------------------------ #
    hr("㉓ 项目一：语音 Golden Set 评测 + 提示词版本准入")
    report = voice.evaluate(prompt_key="consultant")
    print("  " + report.to_text().replace("\n", "\n  "))
    gate = voice.gate_prompt(prompt_key="consultant")
    print(f"\n  准入门禁：allowed={gate['gate']['allowed']}｜本次 {gate['current']['score']:.3f}"
          f"｜基线 {gate['baseline']['score'] if gate['baseline'] else '（首跑即基线）'}")
    for reason in gate["gate"]["reasons"]:
        print(f"    · {reason}")

    print("\n  语音链路指标（首字延迟是体感的分水岭，必须可观测）：")
    metrics = voice.metrics.snapshot()
    print(f"    轮次 {metrics['turns']}｜拒答率 {metrics['refuse_rate']:.1%}"
          f"｜转人工率 {metrics['handoff_rate']:.1%}")
    print(f"    首字延迟 p50={metrics['latency_ms']['ttft_p50']:.0f}ms"
          f" p95={metrics['latency_ms']['ttft_p95']:.0f}ms"
          f"｜总时长 p95={metrics['latency_ms']['total_p95']:.0f}ms")

    print(f"\n  挂断：{voice.stop_agent(session_id)['stopped']}")

    # ================================================================== #
    # 项目八：助贷业务流程数字员工
    # ================================================================== #
    hr("㉔ 项目八：SOP 扫描（漏跟进 = 系统兜底，不靠业务员自觉）")
    flow = ctx.flow_ops
    swept = flow.check_sop()
    print(f"  扫描范围：{flow.metrics_snapshot()['leads']['active']} 条活跃线索"
          f"（SLA：首呼 30 分钟 / 加微 2 小时 / 跟进 24 小时）")
    print(f"  本次新增预警：{swept['count']} 条｜未处理预警总数：{swept['open_total']} 条")
    for alert in swept["alerts"]:
        print(f"    · {alert['customer_name']}（{alert['owner']}）"
              f"{alert['action']} 已超时 {alert['overdue_minutes']} 分钟 → 已通知主管")
    if swept["count"] == 0 and swept["open_total"]:
        print("    （逾期线索此前已预警过，宽限期内不重复轰炸主管 ——"
          "认领 = 有人在处理，再立刻报一次只会训练大家忽略预警）")

    hr("㉕ 项目八：到店前自动准备（档案 + 方案初稿 + 费用预估 = 一页纸）")
    invited = [item for item in flow.leads() if item["stage"] == "invited"]
    if invited:
        lead = invited[0]
        for st in ("arrived",):
            flow.advance(lead["lead_id"], st, actor="demo", reason="客户到店")
        pack = flow.prepare_arrival(lead["lead_id"])
        print(f"  客户：{lead['customer_name']}（{lead['customer_id']}）"
              f"｜准备耗时 {pack['prepared_in_ms']:.1f}ms（原来手工 40 分钟）")
        print(f"  档案亮点：{'、'.join(pack['highlights']) or '（档案信息有限）'}")
        print(f"  风险提示：{'、'.join(pack['risks']) or '无明显风险'}")
        if pack["plan_draft"]:
            plan = pack["plan_draft"]
            print(f"  方案初稿（复用项目二）：{plan['product_name'] or plan['handoff_reason']}")
            print(f"    {plan['summary'][:120]}")
        if pack["fee_estimate"]:
            fee = pack["fee_estimate"]
            print(f"  费用预估：服务费 {fee['service_fee']:,.0f} 元（{fee['service_fee_rate']:.2%}）"
                  f"｜月供 {fee['monthly_payment']:,.0f} 元｜到手 {fee['net_amount']:,.0f} 元")
            print(f"    {fee['basis']}｜{fee['disclaimer']}")
        for point in pack["talking_points"]:
            print(f"  谈资：{point}")
    else:
        print("  （没有 invited 状态的线索，跑一遍 seed_data.py 生成演示数据）")

    hr("㉖ 项目八：HITL —— 发合同必须人工点头（没批就发 = 合规事故）")
    target = invited[0] if invited else None
    if target:
        approval = flow.request_high_risk(
            "send_contract", lead_id=target["lead_id"],
            reason="客户到店后要求发送合同", payload={"lead_id": target["lead_id"]},
        )
        print(f"  申请：{approval['approval_id']} → {approval['status']}")
        try:
            flow.execute_approved(approval["approval_id"])
            print("  !!! 未审批竟然执行了（这是 bug）")
        except Exception as exc:  # noqa: BLE001
            print(f"  未审批执行 → 被拦截：{getattr(exc, 'code', exc)}")
        decided = flow.decide_approval(
            approval["approval_id"], approver="mgr_01", approved=True, comment="材料已核对"
        )
        print(f"  主管审批：{decided['status']}（{decided['decided_by']}：{decided['decision_comment']}）")
        executed = flow.execute_approved(approval["approval_id"])
        print(f"  执行结果：ok={executed['ok']}（CRM 调用 {len(flow.engine.crm.calls)} 次，全程留痕）")
        # 客户侧：自助查进度
        snap = flow.progress_query(target["lead_id"], "1234" if "137" in target["phone_masked"] else "5678")
        print(f"\n  客户自助查进度（脱敏）：{snap['stage_label']}｜{snap['next_step']}")
        print(f"    姓名展示：{snap['customer_name']}（不出现完整手机号与征信明细）")
    else:
        print("  （没有可用的线索）")

    hr("㉗ 项目八：流程 Golden Set 评测 + 准入门禁")
    report = flow.evaluate()
    passed = sum(1 for r in report["results"] if r["passed"])
    print(f"  通过 {passed}/{report['total']}（均分 {report['avg_score']:.2f}）")
    for r in report["results"]:
        if not r["passed"]:
            print(f"    FAIL {r['case_id']}：{r['failures']}")
    gate = flow.gate()
    print(f"  准入门禁：allowed={gate['allowed']}")
    for reason in gate["reasons"]:
        print(f"    · {reason}")
    snap = flow.metrics_snapshot()
    print(f"\n  流程指标：活跃线索 {snap['leads']['active']}"
          f"｜漏跟进率 {snap['missed_followup_rate']:.1%}"
          f"｜幂等拦截 {snap['idempotent_hits']} 次"
          f"｜待审批 {snap['hitl']['pending']} 条")

    hr("㉘ 项目四：对话数据中台（抽取 → 脱敏 → 审核 → 导出 → 知识）")
    dp = ctx.data_platform
    pipe = dp.pipeline()
    print(f"  漏斗：原始消息 {pipe['raw_messages']} → 会话 {pipe['sessions']} → 对话块 {pipe['blocks']}"
          f"（噪声丢弃率 {pipe['drop_rate']:.0%}）→ 待审 {pipe['pending']}")
    print("  9 级分类分布：" + " / ".join(f"{k} {v}" for k, v in pipe["by_category"].items()))
    pending = dp.list_staging(status="pending", limit=3)
    for item in pending:
        dp.review(item.staging_id, "approve", reviewer="demo_lead", comment="演示审批通过")
    if pending:
        print(f"  人工审核：通过 {len(pending)} 条（敏感对话即使通过也不进训练集）")
    manifest = dp.export("sharegpt")
    reused = "（内容未变，复用已有产物）" if manifest.reused else ""
    print(f"  导出 ShareGPT：{manifest.rows} 行 / {manifest.bytes} 字节{reused}")
    knowledge = dp.build_knowledge()
    print(f"  {knowledge.to_text()}")
    for hit in dp.search_knowledge("公积金 置换", top_k=2):
        print(f"    检索命中｜{hit['scene']}｜score={hit['score']}｜{str(hit['text'])[:32]}…")
    print(f"  供给下游（项目七案例语料）：{len(dp.feed_conversations())} 条已审对话")

    hr("✅ 演示完成")
    print("  六个 Agent（产品助手 / 话术陪练 / 素材推广 / 语音客服 / 流程数字员工 / 推理网关）"
          "均可在无外部依赖下完整运行。")
    print("  启动 HTTP 服务：  make run   （然后访问 http://localhost:8000/docs）")
    print("  运行测试：        make test")


if __name__ == "__main__":
    main()
