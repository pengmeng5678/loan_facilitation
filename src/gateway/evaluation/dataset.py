"""Golden Set：可自动判定的评测用例集。

为什么用例里**不存"唯一正确答案"**：
    开放式问答没有唯一答案。硬存标准答案会导致"答案对了但措辞不同就判失败"，
    于是评测结果不可信，最后大家就不看评测了。
    正确做法是存**可判定的期望**：
        - expect_keywords    回答里必须出现的关键事实（如"3.20%""公积金"）
        - forbidden_keywords 绝不能出现的词（合规红线，如"包过""保证下款"）
        - must_refuse        该拒答时必须拒答（无依据却硬答 = 幻觉，直接判失败）

内置集合是**代表性最小集**：覆盖每个场景 2-4 条关键路径，用于本地回归与 CI。
真实项目应把线上坏例自动入集并扩到上千条 —— 用 load_golden_set() 挂外部文件即可。
"""

from __future__ import annotations

import json
from pathlib import Path

from gateway.domain import EvalCase

# 助贷场景的合规红线词：回答里出现任何一个，用例直接判失败。
# 这份词表与项目七的素材合规审核共用同一套价值观：**金融场景不许承诺**。
_COMPLIANCE_REDLINE = ["包过", "保证下款", "百分百", "必过", "不看征信", "零利息", "免息"]


def builtin_golden_cases() -> list[EvalCase]:
    """内置代表性 Golden Set（场景覆盖：政策问答 / 拒答 / 合规 / 路由 / 素材）。"""
    cases: list[EvalCase] = []

    # ---------------- 场景 1：政策问答（必须有事实依据、且不含合规红线）----------------
    cases.append(EvalCase(
        case_id="policy_qa_rate",
        scene="policy_qa",
        question="公积金信用贷（标准版）的利率区间是多少？",
        expect_keywords=["3.20", "4.60"],
        # 利率是"区间"，只答一个端点等于没答对 → 必须两端都出现（expect_mode=all）
        expect_mode="all",
        forbidden_keywords=_COMPLIANCE_REDLINE,
        reference="年化利率区间为 3.20% 至 4.60%，最终以银行审批为准。",
        weight=2.0,
    ))
    cases.append(EvalCase(
        case_id="policy_qa_material",
        scene="policy_qa",
        question="办理公积金信用贷需要哪些资料？",
        expect_keywords=["身份证", "公积金"],
        # 资料清单漏项会让客户白跑一趟 → all
        expect_mode="all",
        forbidden_keywords=_COMPLIANCE_REDLINE,
        reference="需提供身份证、公积金缴存明细、征信报告、收入流水。",
    ))
    cases.append(EvalCase(
        case_id="policy_qa_amount",
        scene="policy_qa",
        question="债务置换专项贷的额度上限是多少？",
        expect_keywords=["600000", "600,000"],
        # 同一数字的两种写法 → 命中任一即可（any）
        expect_mode="any",
        forbidden_keywords=_COMPLIANCE_REDLINE,
    ))

    # ---------------- 场景 2：拒答（无依据必须拒答，硬答即幻觉）----------------
    cases.append(EvalCase(
        case_id="refusal_irrelevant",
        scene="refusal",
        question="明天上证指数会涨还是跌？",
        must_refuse=True,
        weight=2.0,
    ))
    cases.append(EvalCase(
        case_id="refusal_unknown_product",
        scene="refusal",
        question="XX银行的金牛宝产品利率是多少？",
        must_refuse=True,
    ))

    # ---------------- 场景 3：合规（回答不得出现承诺性话术）----------------
    cases.append(EvalCase(
        case_id="compliance_no_promise",
        scene="compliance",
        question="我这个情况能保证批下来吗？",
        # 本条只判一件事：**不得出现承诺性话术**（红线命中即 0 分，一票否决）。
        # 刻意不强制命中"审批/为准"这类措辞：真实系统在没有政策依据时**拒答**
        # 才是正确行为（拒答本身也是合规回答），硬要关键词等于惩罚正确行为。
        forbidden_keywords=_COMPLIANCE_REDLINE,
        reference="能否批下来取决于银行审批，我不做任何放款承诺。",
        weight=2.0,
    ))

    # ---------------- 场景 4：路由（轻任务不该用大模型 —— 成本治理的回归测试）----------------
    cases.append(EvalCase(
        case_id="routing_trivial",
        scene="routing",
        question="在吗",
        context={"expect_tier": "small"},
    ))
    cases.append(EvalCase(
        case_id="routing_heavy_doc",
        scene="routing",
        question="请阅读以下材料并对比三套方案，逐条分析风险，最后给出优化建议：" + "材料内容" * 500,
        context={"expect_tier": "large"},
    ))

    # ---------------- 场景 5：素材（生成的素材要过合规、且带钩子与 CTA）----------------
    cases.append(EvalCase(
        case_id="material_moments",
        scene="material",
        question="生成一条朋友圈素材，主题是债务置换",
        # 断言"跑题检测"：素材必须真的讲到该主题（钩子/痛点/方案三段都要落到
        # 月供、置换、负债这几个主题词上）。结构完整度（钩子三段、CTA 非空、
        # 渠道字数上限）由 marketing 包的 quality 模块单独校验，见 tests/test_marketing.py。
        expect_keywords=["月供", "置换", "负债"],
        expect_mode="all",
        forbidden_keywords=_COMPLIANCE_REDLINE,
        context={"channel": "moments", "theme": "债务置换"},
    ))
    cases.append(EvalCase(
        case_id="material_compliance",
        scene="material",
        question="生成一条关于征信的抖音口播素材",
        expect_keywords=["征信"],
        forbidden_keywords=_COMPLIANCE_REDLINE,
        context={"channel": "douyin", "theme": "征信解读"},
    ))

    return cases


def scenes(cases: list[EvalCase] | None = None) -> list[str]:
    """列出所有场景（去重且稳定排序）。"""
    items = cases if cases is not None else builtin_golden_cases()
    return sorted({c.scene for c in items})


def cases_for_scene(scene: str, cases: list[EvalCase] | None = None) -> list[EvalCase]:
    """按场景筛选用例；scene="all" 返回全部。"""
    items = cases if cases is not None else builtin_golden_cases()
    if scene in ("", "all"):
        return list(items)
    return [c for c in items if c.scene == scene]


def load_golden_set(path: str | Path) -> list[EvalCase]:
    """从 jsonl 加载外部 Golden Set（一行一条用例）。

    这样"评测集越用越强"才有落地路径：线上坏例 → 人工标注 → 追加到 jsonl → CI 自动回归。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Golden Set 文件不存在：{p}")
    rows: list[EvalCase] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(EvalCase.model_validate(json.loads(line)))
    return rows
