"""模拟客户 Agent（角色扮演）。

关键设计（对应简历："模拟客户要会抵抗"）：
    如果客户有问必答、有求必应，陪练就练不出真本事。
    所以本模块的核心不是"生成一句话"，而是**识别业务员的话在干什么，并给出
    符合画像的对抗性反应** —— 会犹豫、会比价、会质疑、会隐瞒、会推脱。

两种实现：
    确定性（offline）  —— 用话术意图识别 + 画像规则生成回复，可复现、可测试
    LLM（生产）        —— 把画像与规则作为 system prompt，让模型自然表达
"""

from __future__ import annotations

import re

from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from coaching.domain import Persona, RoleplaySession

logger = get_logger(__name__)

# ---- 业务员话术的意图识别（确定性部分） ---------------------------------- #
_INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "ask_qualification": ("公积金", "社保", "房子", "房产", "车", "保单", "营业执照", "执照", "收入", "工资", "工作", "缴存"),
    "ask_credit": ("征信", "查询", "逾期", "负债", "网贷", "小贷", "贷款笔数", "信用卡"),
    "ask_purpose": ("用途", "做什么用", "资金用途", "用来", "需求", "额度需求", "借来"),
    "ask_amount": ("多少额度", "需要多少", "想贷多少", "金额"),
    "promise": ("包过", "保证", "一定批", "一定能", "百分百", "100%", "必过", "不看征信", "稳下", "肯定能"),
    "invite": (
        "到店", "面签", "来一趟", "过来", "线下", "上门", "约个时间", "方便过来", "到我们这",
        "来店", "店里", "面谈", "来我们", "到公司",
        # 提到具体星期几本身就是强邀约信号
        "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天",
    ),
    "price": ("服务费", "费用", "几个点", "收费", "手续费", "点位"),
    "compliance": ("以银行审批为准", "不承诺", "预估", "不保证", "无法保证"),
    "empathy": ("理解", "明白您", "我理解", "确实", "担心是正常的"),
}


def detect_intents(text: str) -> set[str]:
    """识别一句话里包含的业务意图（可多选）。"""
    hit: set[str] = set()
    for intent, keys in _INTENT_PATTERNS.items():
        if any(k in text for k in keys):
            hit.add(intent)
    return hit


class CustomerSimulator:
    """虚拟客户。"""

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm
        self._use_llm = llm is not None and getattr(llm, "name", "echo") != "echo"

    # ------------------------------------------------------------------ #
    # 开场
    # ------------------------------------------------------------------ #
    def opening(self, session: RoleplaySession) -> str:
        """客户的第一句话（按场景不同）。"""
        persona = session.persona
        scene = persona.scene

        if scene == "首触":
            return "你好，我在网上看到你们的广告，想问问贷款的事。"
        if scene == "邀约":
            return "我上次咨询过你们，方案我还在考虑，你先说说吧。"
        if scene == "异议处理":
            return "我已经问过好几家了，你们先说说你们的利率和费用是多少？"
        if scene == "征信解释":
            return "我征信可能有点问题，你们这边还能办吗？"
        return "你好，我想咨询一下贷款。"

    # ------------------------------------------------------------------ #
    # 回复
    # ------------------------------------------------------------------ #
    def reply(
        self,
        session: RoleplaySession,
        business_text: str,
        *,
        facts: dict | None = None,
    ) -> str:
        """对业务员的话给出符合画像的回复。"""
        if self._use_llm:
            try:
                return self._reply_llm(session, business_text, facts or {})
            except Exception as exc:  # noqa: BLE001 - 模型异常必须降级，不能中断陪练
                logger.warning("模拟客户 LLM 生成失败，降级为规则生成：%s", exc)
        return self._reply_rules(session, business_text, facts or {})

    # ------------------------------------------------------------------ #
    # 确定性实现
    # ------------------------------------------------------------------ #
    def _reply_rules(self, session: RoleplaySession, business_text: str, facts: dict) -> str:
        persona = session.persona
        intents = detect_intents(business_text)
        # 已发生的对话轮数越多，客户"抗性"越强（模拟真实拉锯）
        turn_index = len(session.customer_messages)

        # ---- 优先级 1：业务员做了违规承诺 → 客户质疑（这是训练重点）----
        if "promise" in intents and "compliance" not in intents:
            if persona.difficulty == "hard":
                return "你这么说我更不放心了 —— 别家也这么跟我保证过，最后也没批下来。你能不能给我一个实在的说法？"
            return "真的吗？你们不会是想先收钱吧？我看网上说这种承诺都不靠谱。"

        # ---- 优先级 2：问资质 → 按事实回答（信息模糊型会敷衍）----
        if "ask_qualification" in intents:
            if persona.persona_id == "P-VAGUE" and turn_index < 2:
                return "这些……反正就是普通上班的，具体数额我记不太清。"
            quals = facts.get("qualifications") or []
            income = facts.get("monthly_income")
            parts = []
            if quals:
                parts.append("、".join(quals))
            if income:
                parts.append(f"月收入大概 {income:,.0f}")
            if parts:
                return f"有的，我有{'，'.join(parts)}，这些应该够了吧？"
            return "我没公积金也没社保，就有个保单，能办吗？"

        # ---- 优先级 3：问征信 → 按画像回答（瑕疵型会先隐瞒）----
        if "ask_credit" in intents:
            if persona.persona_id == "P-CREDIT" and turn_index < 2:
                return "征信应该没问题吧，我没太注意过。"
            query = facts.get("credit_query_6m")
            overdue = facts.get("overdue_count_12m")
            loans = facts.get("loans_count")
            if query is not None:
                detail = f"查询大概 {query} 次"
                if overdue:
                    detail += f"，有 {overdue} 次逾期"
                if loans:
                    detail += f"，现在还有 {loans} 笔贷款在用"
                return f"我查过，{detail}。这个影响大吗？"
            return "我不太懂征信，你们能帮我查一下吗？"

        # ---- 优先级 4：问用途/额度 ----
        if "ask_purpose" in intents or "ask_amount" in intents:
            needs = facts.get("needs") or []
            amount = facts.get("desired_amount")
            need_text = "、".join(needs) if needs else "就是周转一下"
            amount_text = f"大概要 {amount:,.0f}" if amount else "具体数字还没想好"
            return f"{need_text}，{amount_text}。主要是想减轻一下月供压力。"

        # ---- 优先级 5：报费用 → 比价型客户压价 ----
        if "price" in intents:
            if persona.difficulty == "hard":
                return "2 个点还行，别家给我报 1.5 个点。你们要是能再低一点我就考虑。"
            return "这个费用是怎么算的？是放款之后才收吗？"

        # ---- 优先级 6：邀约 → 按画像推脱或推进 ----
        if "invite" in intents:
            if persona.persona_id == "P-WAIT":
                return "我最近比较忙，周末要陪家人，能不能先线上办？"
            if persona.persona_id == "P-PRICE":
                return "你先告诉我，我这个资质能不能做到 3.2% 的利率？能确定我再去。"
            if persona.persona_id == "P-CREDIT":
                return "我这个情况真能办下来吗？别到时候白跑一趟。"
            return "行，那你给我个时间，我看看能不能安排。"

        # ---- 兜底：抛出一个画像自带的异议（按轮次轮换）----
        objections = persona.common_objections or ["我再考虑考虑"]
        return objections[turn_index % len(objections)]

    # ------------------------------------------------------------------ #
    # LLM 实现
    # ------------------------------------------------------------------ #
    def _reply_llm(self, session: RoleplaySession, business_text: str, facts: dict) -> str:
        persona = session.persona
        history = "\n".join(
            f"{'业务员' if m.role == 'business' else '客户'}：{m.text}"
            for m in session.messages[-8:]
        )
        facts_text = "；".join(f"{k}={v}" for k, v in facts.items() if v) or "（暂无档案，按画像设定回答）"

        system = (
            "你在扮演一位助贷业务场景中的客户，用于训练业务员。必须严格遵守：\n"
            f"1. 你的身份：{persona.name}。背景：{persona.profile}\n"
            f"2. 你的抗性表现：{'；'.join(persona.resistance) or '正常沟通'}\n"
            f"3. 你的常见异议：{'；'.join(persona.common_objections) or '无明显异议'}\n"
            f"4. 你的真实情况（只有被问到才回答，不要主动全盘托出）：{facts_text}\n"
            "5. 绝不要配合业务员 —— 要会犹豫、会比价、会质疑、会推脱，这样才练得出本事。\n"
            "6. 每次只回 1~2 句话，口语化，不要一次给太多信息。\n"
            "7. 如果业务员做出'包过、保证下款、不看征信'这类承诺，你要表现出怀疑和警惕。"
        )
        user = f"对话记录：\n{history}\n\n业务员刚说：{business_text}\n\n请给出客户的回复："
        resp = self.llm.complete(
            [Message(role="system", content=system), Message(role="user", content=user)],
            temperature=0.8,  # 陪练需要多样性，温度调高
        )
        return resp.content.strip().strip('"')

    # ------------------------------------------------------------------ #
    # 质量校验（用于单测：保证模拟客户"不放水"）
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_resistant(reply: str) -> bool:
        """判断回复是否具备"抗性"（不含抗性的回复等于放水）。"""
        markers = ("考虑", "再说", "别家", "真的", "不放心", "白跑", "忙", "费用", "保证", "怀疑", "凭什么", "但是", "不过")
        return any(m in reply for m in markers)


def persona_prompt_block(persona: Persona) -> str:
    """把画像渲染成文本块（供 prompt 复用）。"""
    return (
        f"{persona.name}（难度 {persona.difficulty}，场景 {persona.scene}）\n"
        f"背景：{persona.profile}\n"
        f"抗性：{'、'.join(persona.resistance)}\n"
        f"常见异议：{'、'.join(persona.common_objections)}"
    )


def strip_quotes(text: str) -> str:
    return re.sub(r'^["“”\']+|["“”\']+$', "", text.strip())
