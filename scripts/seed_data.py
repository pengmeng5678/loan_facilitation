"""演示数据生成脚本。

    python scripts/seed_data.py

生成到 .data/ 下：
    policies/products.json      产品目录（10 款银行产品）
    policies/policies.jsonl     政策条款（带生效日期，用于检索与引用溯源）
    profiles/customers.json     客户档案（用于方案匹配）
    profiles/personas.json      客户画像（用于话术陪练的角色扮演）
    policies/conversations.jsonl 历史成交/未成单对话（用于话质检与话术挖掘）
    marketing/blueprints.json        素材结构模板库（项目七：钩子→痛点→方案→CTA）
    marketing/dormant_customers.json 未成单沉睡客户（项目七：激活对象）
    marketing/material_performance.json 素材效果漏斗（项目七：归因演示）

数据全部为**构造的演示数据**，产品编号/银行名/金额/效果指标均为虚构，
不涉及任何真实机构与真实客户；接入生产时替换为真实埋点与业务库即可。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

# 允许直接 `python scripts/seed_data.py` 而不必先 pip install
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_platform.config import get_settings  # noqa: E402
from agent_platform.logging_config import get_logger  # noqa: E402

logger = get_logger("seed")


# =========================================================================== #
# 一、产品目录
# =========================================================================== #
PRODUCTS = [
    {
        "code": "BANK-A-GJJ-001",
        "name": "公积金信用贷（标准版）",
        "bank": "演示银行A",
        "category": "公积金贷",
        "purposes": ["资金周转", "债务置换"],
        "age_min": 22, "age_max": 58,
        "income_min": 4000,
        "qualifications": ["公积金"],
        "occupations": ["受薪"],
        "credit_query_6m_max": 8, "overdue_count_max": 2, "overdue_days_max": 30,
        "debt_ratio_max": 0.65,
        "amount_min": 50000, "amount_max": 400000,
        "rate_min": 3.20, "rate_max": 4.60,
        "term_months": [12, 24, 36, 60],
        "quota_multiple": 40, "quota_base": "公积金基数",
        "materials": ["身份证", "公积金缴存明细（近12个月）", "征信报告", "收入流水"],
        "process": ["提交资料", "银行预审", "线下面签", "放款到账"],
        "effective_date": "2024-06-01", "expire_date": None,
        "substitutes": ["BANK-H-GJJ-002", "BANK-B-SB-001"],
    },
    {
        "code": "BANK-H-GJJ-002",
        "name": "公积金尊享贷（高额度）",
        "bank": "演示银行H",
        "category": "公积金贷",
        "purposes": ["资金周转", "债务置换", "负债优化"],
        "age_min": 25, "age_max": 55,
        "income_min": 8000,
        "qualifications": ["公积金"],
        "occupations": ["受薪", "企业主"],
        "credit_query_6m_max": 6, "overdue_count_max": 1, "overdue_days_max": 15,
        "debt_ratio_max": 0.60,
        "amount_min": 100000, "amount_max": 800000,
        "rate_min": 2.98, "rate_max": 3.90,
        "term_months": [12, 24, 36, 60, 84],
        "quota_multiple": 60, "quota_base": "公积金基数",
        "materials": ["身份证", "公积金缴存明细（近24个月）", "征信报告", "收入证明", "社保记录"],
        "process": ["提交资料", "银行预审", "面签", "抵押/放款"],
        "effective_date": "2025-01-01", "expire_date": None,
        "substitutes": ["BANK-A-GJJ-001"],
    },
    {
        "code": "BANK-B-SB-001",
        "name": "社保信用贷",
        "bank": "演示银行B",
        "category": "信用贷",
        "purposes": ["资金周转", "装修"],
        "age_min": 22, "age_max": 60,
        "income_min": 3500,
        "qualifications": ["社保"],
        "occupations": ["受薪"],
        "credit_query_6m_max": 10, "overdue_count_max": 3, "overdue_days_max": 30,
        "debt_ratio_max": 0.70,
        "amount_min": 30000, "amount_max": 200000,
        "rate_min": 4.20, "rate_max": 6.80,
        "term_months": [12, 24, 36],
        "quota_multiple": 20, "quota_base": "社保基数",
        "materials": ["身份证", "社保缴存明细", "征信报告"],
        "process": ["线上申请", "银行预审", "线上签约", "放款"],
        "effective_date": "2024-03-01", "expire_date": None,
        "substitutes": ["BANK-A-GJJ-001"],
    },
    {
        "code": "BANK-C-HOUSE-001",
        "name": "房产抵押经营贷",
        "bank": "演示银行C",
        "category": "抵押贷",
        "purposes": ["债务置换", "负债优化", "经营周转", "资金周转"],
        "age_min": 22, "age_max": 65,
        "income_min": 0,
        "qualifications": ["房产"],
        "occupations": [],
        "credit_query_6m_max": 15, "overdue_count_max": 5, "overdue_days_max": 90,
        "debt_ratio_max": 0.95,
        "amount_min": 200000, "amount_max": 3000000,
        "rate_min": 3.45, "rate_max": 4.80,
        "term_months": [12, 36, 60, 120, 240],
        "quota_multiple": 0.7, "quota_base": "房产评估价",
        "materials": ["身份证", "房产证", "房产评估报告", "征信报告", "用途合同"],
        "process": ["提交资料", "房产评估", "银行审批", "抵押登记", "放款"],
        "effective_date": "2024-01-15", "expire_date": None,
        "substitutes": ["BANK-G-DBT-001"],
    },
    {
        "code": "BANK-G-DBT-001",
        "name": "债务置换专项贷",
        "bank": "演示银行G",
        "category": "信用贷",
        "purposes": ["债务置换", "负债优化"],
        "age_min": 24, "age_max": 58,
        "income_min": 6000,
        "qualifications": ["公积金", "社保", "房产"],
        "occupations": ["受薪", "自雇", "企业主"],
        "credit_query_6m_max": 12, "overdue_count_max": 4, "overdue_days_max": 60,
        "debt_ratio_max": 0.80,
        "amount_min": 80000, "amount_max": 600000,
        "rate_min": 4.35, "rate_max": 7.20,
        "term_months": [12, 24, 36, 48],
        "quota_multiple": 30, "quota_base": "月收入",
        "materials": ["身份证", "征信报告", "存量贷款清单", "收入流水", "用途说明"],
        "process": ["负债梳理", "提交资料", "银行预审", "置换放款"],
        "effective_date": "2024-09-01", "expire_date": None,
        "substitutes": ["BANK-C-HOUSE-001", "BANK-H-GJJ-002"],
    },
    {
        "code": "BANK-D-BIZ-001",
        "name": "小微企业主经营贷",
        "bank": "演示银行D",
        "category": "经营贷",
        "purposes": ["经营周转", "资金周转"],
        "age_min": 25, "age_max": 62,
        "income_min": 0,
        "qualifications": ["营业执照"],
        "occupations": ["企业主", "自雇"],
        "credit_query_6m_max": 12, "overdue_count_max": 4, "overdue_days_max": 60,
        "debt_ratio_max": 0.85,
        "amount_min": 100000, "amount_max": 1000000,
        "rate_min": 3.85, "rate_max": 5.60,
        "term_months": [12, 24, 36, 60],
        "quota_multiple": 24, "quota_base": "年开票额",
        "materials": ["身份证", "营业执照", "近12个月开票/流水", "征信报告", "经营场所证明"],
        "process": ["提交资料", "上门尽调", "银行审批", "放款"],
        "effective_date": "2024-05-01", "expire_date": None,
        "substitutes": ["BANK-C-HOUSE-001"],
    },
    {
        "code": "BANK-E-BX-001",
        "name": "保单质押贷",
        "bank": "演示银行E",
        "category": "信用贷",
        "purposes": ["资金周转"],
        "age_min": 23, "age_max": 60,
        "income_min": 3000,
        "qualifications": ["保单"],
        "occupations": ["受薪", "自雇"],
        "credit_query_6m_max": 14, "overdue_count_max": 3, "overdue_days_max": 45,
        "debt_ratio_max": 0.85,
        "amount_min": 20000, "amount_max": 300000,
        "rate_min": 5.40, "rate_max": 8.50,
        "term_months": [12, 24, 36],
        "quota_multiple": 20, "quota_base": "年缴保费",
        "materials": ["身份证", "保单合同", "缴费凭证", "征信报告"],
        "process": ["提交资料", "保单核验", "银行审批", "放款"],
        "effective_date": "2024-02-01", "expire_date": None,
        "substitutes": ["BANK-B-SB-001"],
    },
    {
        "code": "BANK-F-CAR-001",
        "name": "车辆抵押贷",
        "bank": "演示银行F",
        "category": "车抵贷",
        "purposes": ["资金周转", "债务置换"],
        "age_min": 22, "age_max": 60,
        "income_min": 0,
        "qualifications": ["车产"],
        "occupations": [],
        "credit_query_6m_max": 18, "overdue_count_max": 6, "overdue_days_max": 90,
        "debt_ratio_max": 1.0,
        "amount_min": 30000, "amount_max": 500000,
        "rate_min": 6.80, "rate_max": 12.00,
        "term_months": [12, 24, 36],
        "quota_multiple": 0.8, "quota_base": "车辆评估价",
        "materials": ["身份证", "行驶证", "车辆登记证", "车辆评估", "征信报告"],
        "process": ["车辆评估", "提交资料", "银行审批", "抵押登记", "放款"],
        "effective_date": "2024-04-01", "expire_date": None,
        "substitutes": ["BANK-E-BX-001"],
    },
    {
        "code": "BANK-I-DEC-001",
        "name": "装修分期贷",
        "bank": "演示银行I",
        "category": "装修贷",
        "purposes": ["装修"],
        "age_min": 22, "age_max": 60,
        "income_min": 5000,
        "qualifications": ["房产", "公积金", "社保"],
        "occupations": ["受薪", "自雇"],
        "credit_query_6m_max": 8, "overdue_count_max": 2, "overdue_days_max": 30,
        "debt_ratio_max": 0.65,
        "amount_min": 50000, "amount_max": 300000,
        "rate_min": 3.60, "rate_max": 5.20,
        "term_months": [12, 24, 36, 60],
        "quota_multiple": 15, "quota_base": "月收入",
        "materials": ["身份证", "房产证明或装修合同", "征信报告", "收入流水"],
        "process": ["提交资料", "银行预审", "面签", "放款至装修账户"],
        "effective_date": "2024-07-01", "expire_date": None,
        "substitutes": ["BANK-B-SB-001"],
    },
    {
        "code": "BANK-J-GS-001",
        "name": "个人所得税信用贷",
        "bank": "演示银行J",
        "category": "信用贷",
        "purposes": ["资金周转", "装修", "负债优化"],
        "age_min": 25, "age_max": 55,
        "income_min": 6000,
        "qualifications": ["个税"],
        "occupations": ["受薪", "自雇"],
        "credit_query_6m_max": 8, "overdue_count_max": 2, "overdue_days_max": 30,
        "debt_ratio_max": 0.70,
        "amount_min": 50000, "amount_max": 500000,
        "rate_min": 3.55, "rate_max": 5.40,
        "term_months": [12, 24, 36, 60],
        "quota_multiple": 36, "quota_base": "年个税额",
        "materials": ["身份证", "个税完税证明", "征信报告", "收入流水"],
        "process": ["提交资料", "银行预审", "线上面签", "放款"],
        "effective_date": "2025-02-01", "expire_date": None,
        "substitutes": ["BANK-H-GJJ-002"],
    },
]


# =========================================================================== #
# 二、政策条款（一条 = 一个父块）
# =========================================================================== #
def build_policy_documents() -> list[dict]:
    """为每个产品生成 4 条政策条款：额度利率 / 准入条件 / 所需资料 / 办理流程与时效。"""
    docs: list[dict] = []
    for p in PRODUCTS:
        code = p["code"]
        eff = p["effective_date"]
        src = f"policies/{code}.pdf"
        base = {"product_code": code, "effective_date": eff, "expire_date": p.get("expire_date")}

        docs.append({
            **base,
            "policy_id": f"{code}-RATE-01",
            "clause_no": "2.1",
            "title": f"{p['name']} 额度与利率",
            "source_file": src,
            "text": (
                f"{p['name']}（{p['bank']}，产品编号 {code}）授信额度区间为 "
                f"{p['amount_min']:,.0f} 元至 {p['amount_max']:,.0f} 元；"
                f"年化利率区间为 {p['rate_min']:.2f}% 至 {p['rate_max']:.2f}%，"
                f"可选期限为 {'、'.join(str(m) for m in p['term_months'])} 个月。"
                f"额度测算方式：{p['quota_base']} × {p['quota_multiple']}（倍数仅供参考）。"
                f"最终额度与利率以银行审批结果为准，本行不做任何放款承诺。"
            ),
        })
        docs.append({
            **base,
            "policy_id": f"{code}-ELIG-01",
            "clause_no": "3.1",
            "title": f"{p['name']} 准入条件",
            "source_file": src,
            "text": (
                f"申请 {p['name']} 需同时满足以下条件：年龄 {p['age_min']} 至 {p['age_max']} 周岁；"
                f"月收入不低于 {p['income_min']:,.0f} 元；"
                f"具备任一资质：{'、'.join(p['qualifications'])}；"
                f"近 6 个月征信查询次数不超过 {p['credit_query_6m_max']} 次；"
                f"近 12 个月逾期次数不超过 {p['overdue_count_max']} 次，且最大逾期天数不超过 {p['overdue_days_max']} 天；"
                f"负债收入比不超过 {p['debt_ratio_max']:.2f}。"
                f"受理客群：{'不限' if not p['occupations'] else '、'.join(p['occupations'])}。"
            ),
        })
        docs.append({
            **base,
            "policy_id": f"{code}-DOC-01",
            "clause_no": "4.2",
            "title": f"{p['name']} 所需资料",
            "source_file": src,
            "text": (
                f"办理 {p['name']} 需提供以下资料：{'、'.join(p['materials'])}。"
                f"资料不齐将退回补件，可能影响审批时效。"
            ),
        })
        docs.append({
            **base,
            "policy_id": f"{code}-FLOW-01",
            "clause_no": "5.1",
            "title": f"{p['name']} 办理流程",
            "source_file": src,
            "text": (
                f"{p['name']} 办理流程：{' → '.join(p['process'])}。"
                f"标准审批时效约 3 至 7 个工作日，抵押类产品需额外办理抵押登记。"
            ),
        })
    return docs


# =========================================================================== #
# 三、客户档案
# =========================================================================== #
CUSTOMERS = [
    {
        "customer_id": "C1001", "name_masked": "张*生", "age": 34, "city": "南京",
        "occupation_type": "受薪", "monthly_income": 18000,
        "qualifications": ["公积金", "社保", "房产"],
        "credit_query_6m": 3, "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 6200, "loans_count": 2,
        "needs": ["债务置换"], "desired_amount": 300000,
        "note": "有 3 笔网贷想做置换，公积金缴存 3 年",
    },
    {
        "customer_id": "C1002", "name_masked": "李*士", "age": 41, "city": "杭州",
        "occupation_type": "企业主", "monthly_income": 45000,
        "qualifications": ["营业执照", "房产", "车产"],
        "credit_query_6m": 9, "overdue_count_12m": 1, "overdue_days_max": 12,
        "monthly_debt": 21000, "loans_count": 4,
        "needs": ["经营周转", "负债优化"], "desired_amount": 1500000,
        "note": "公司有开票流水，需要大额经营周转",
    },
    {
        "customer_id": "C1003", "name_masked": "王*姐", "age": 29, "city": "上海",
        "occupation_type": "受薪", "monthly_income": 9000,
        "qualifications": ["社保"],
        "credit_query_6m": 6, "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 3200, "loans_count": 3,
        "needs": ["资金周转"], "desired_amount": 100000,
        "note": "刚工作几年，无房无车，社保连续缴纳",
    },
    {
        "customer_id": "C1004", "name_masked": "刘*生", "age": 52, "city": "深圳",
        "occupation_type": "自雇", "monthly_income": 26000,
        "qualifications": ["房产", "保单"],
        "credit_query_6m": 14, "overdue_count_12m": 3, "overdue_days_max": 45,
        "monthly_debt": 19000, "loans_count": 6,
        "needs": ["债务置换", "负债优化"], "desired_amount": 800000,
        "note": "征信查询较多，有多次逾期，需要抵押类方案",
    },
    {
        "customer_id": "C1005", "name_masked": "陈*女", "age": 26, "city": "南京",
        "occupation_type": "受薪", "monthly_income": 5000,
        "qualifications": [],
        "credit_query_6m": 12, "overdue_count_12m": 2, "overdue_days_max": 20,
        "monthly_debt": 4500, "loans_count": 5,
        "needs": ["资金周转"], "desired_amount": 80000,
        "note": "资质薄弱、查询偏多，预期需要转人工或养征信",
    },
    # ---- C1006 起：为流程看板补全中间段与成交段的业务场景 ----
    {
        "customer_id": "C1006", "name_masked": "赵*生", "age": 31, "city": "南京",
        "occupation_type": "受薪", "monthly_income": 15000,
        "qualifications": ["公积金", "社保"],
        "credit_query_6m": 2, "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 4800, "loans_count": 2,
        "needs": ["资金周转"], "desired_amount": 200000,
        "note": "公积金缴存 2 年，想置换信用卡分期",
    },
    {
        "customer_id": "C1007", "name_masked": "钱*姐", "age": 27, "city": "杭州",
        "occupation_type": "受薪", "monthly_income": 8000,
        "qualifications": ["社保"],
        "credit_query_6m": 5, "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 2600, "loans_count": 1,
        "needs": ["资金周转"], "desired_amount": 80000,
        "note": "抖音凌晨留资，表单只填了姓名和手机号",
    },
    {
        "customer_id": "C1008", "name_masked": "吴*总", "age": 45, "city": "南京",
        "occupation_type": "企业主", "monthly_income": 60000,
        "qualifications": ["营业执照", "房产"],
        "credit_query_6m": 4, "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 32000, "loans_count": 3,
        "needs": ["经营周转"], "desired_amount": 1500000,
        "note": "门店经营 8 年，有开票流水，要按天算资金成本",
    },
    {
        "customer_id": "C1009", "name_masked": "周*士", "age": 33, "city": "上海",
        "occupation_type": "受薪", "monthly_income": 22000,
        "qualifications": ["公积金", "保单"],
        "credit_query_6m": 3, "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 8800, "loans_count": 3,
        "needs": ["债务置换"], "desired_amount": 400000,
        "note": "有 4 笔信用贷想做整合，只关心月供能不能降",
    },
    {
        "customer_id": "C1010", "name_masked": "郑*总", "age": 38, "city": "深圳",
        "occupation_type": "自雇", "monthly_income": 34000,
        "qualifications": ["房产", "车产"],
        "credit_query_6m": 7, "overdue_count_12m": 1, "overdue_days_max": 8,
        "monthly_debt": 26000, "loans_count": 4,
        "needs": ["负债优化"], "desired_amount": 1000000,
        "note": "房产可抵押，主要诉求是把月供压下来",
    },
    {
        "customer_id": "C1011", "name_masked": "孙*姐", "age": 42, "city": "南京",
        "occupation_type": "受薪", "monthly_income": 30000,
        "qualifications": ["公积金", "房产"],
        "credit_query_6m": 2, "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 12000, "loans_count": 2,
        "needs": ["经营周转"], "desired_amount": 600000,
        "note": "夫妻双方都有公积金，可做额度放大",
    },
]


# =========================================================================== #
# 四、客户画像（话术陪练用）
# =========================================================================== #
PERSONAS = [
    {
        "persona_id": "P-VAGUE",
        "name": "张先生（信息模糊型）",
        "difficulty": "easy",
        "scene": "首触",
        "profile": "34岁，公积金缴存3年，有3笔网贷想置换，但不愿意主动说负债情况",
        "resistance": ["回答简短", "不愿提供细节", "需要业务员主动追问资质"],
        "common_objections": ["我随便看看", "你们是怎么收费的"],
        "customer_id": "C1001",
    },
    {
        "persona_id": "P-PRICE",
        "name": "李总（比价型）",
        "difficulty": "hard",
        "scene": "异议处理",
        "profile": "41岁企业主，需要150万经营周转，已经问过3家中介在比价",
        "resistance": ["反复压价", "拿别家报价对比", "追问能不能保证下款"],
        "common_objections": [
            "你们的服务费太高了",
            "别家说能做到3.2%的利率，你们行不行",
            "能不能保证批下来",
        ],
        "customer_id": "C1002",
    },
    {
        "persona_id": "P-CREDIT",
        "name": "刘先生（征信瑕疵型）",
        "difficulty": "medium",
        "scene": "征信解释",
        "profile": "52岁，征信查询14次、有3次逾期，担心办不下来",
        "resistance": ["隐瞒逾期", "情绪敏感", "反复问'我这个情况还能办吗'"],
        "common_objections": [
            "我逾期是忘了还，应该没什么影响吧",
            "我朋友比我查询还多都办下来了",
            "你们是不是骗人的，办不下来是不是要收费",
        ],
        "customer_id": "C1004",
    },
    {
        "persona_id": "P-WAIT",
        "name": "王女士（拖延型）",
        "difficulty": "medium",
        "scene": "邀约",
        "profile": "29岁，需要10万周转，但一直说再考虑考虑，不想到店",
        "resistance": ["推脱到店", "说要跟家人商量", "不回复"],
        "common_objections": ["我再考虑考虑", "我周末没时间", "先线上办不行吗"],
        "customer_id": "C1003",
    },
]


# =========================================================================== #
# 五、历史对话（质检 + 话术挖掘输入）
# =========================================================================== #
CONVERSATIONS = [
    {
        "conversation_id": "CONV-0001",
        "customer_id": "C1001",
        "agent": "业务员小陈",
        "outcome": "won",
        "channel": "wecom",
        "started_at": "2026-08-12T10:05:00",
        "messages": [
            {"role": "business", "text": "张先生您好，我是助贷顾问小陈。刚才智能助手把您的情况同步给我了，您是想把几笔网贷置换成银行贷款，主要关心利率和放款速度，对吗？", "ts": 0},
            {"role": "customer", "text": "对，我大概有3笔网贷，月供压力有点大", "ts": 30},
            {"role": "business", "text": "明白。我先帮您做一下资质初审：您公积金缴存多久了？近半年有没有申请过其他贷款、征信查询大概几次？", "ts": 60},
            {"role": "customer", "text": "公积金交了3年，最近查过3次吧", "ts": 95},
            {"role": "business", "text": "好的，公积金3年、查询3次，这个资质可以做公积金置换方案，年化大概3%出头，比您现在的网贷成本低很多。", "ts": 120},
            {"role": "customer", "text": "真的能到3%吗？", "ts": 150},
            {"role": "business", "text": "预估区间是2.98%到3.9%，具体以银行审批为准，我不会给您承诺死数字，但按您的资质通过率是比较高的。", "ts": 175},
        ],
    },
    {
        "conversation_id": "CONV-0002",
        "customer_id": "C1002",
        "agent": "业务员小林",
        "outcome": "lost",
        "channel": "wecom",
        "started_at": "2026-08-14T15:20:00",
        "messages": [
            {"role": "business", "text": "李总您好，您需要的150万经营周转我们可以做，成功率很高，包过！", "ts": 0},
            {"role": "customer", "text": "服务费怎么算？别家收2个点", "ts": 40},
            {"role": "business", "text": "我们收3个点，但能保证下款，不看征信的", "ts": 65},
            {"role": "customer", "text": "真的不看征信？那我先不用了", "ts": 90},
        ],
    },
    {
        "conversation_id": "CONV-0003",
        "customer_id": "C1003",
        "agent": "业务员小陈",
        "outcome": "pending",
        "channel": "wecom",
        "started_at": "2026-08-18T09:30:00",
        "messages": [
            {"role": "business", "text": "王女士您好，您之前咨询的10万周转，方案我这边准备好了", "ts": 0},
            {"role": "customer", "text": "我还在考虑", "ts": 600},
            {"role": "business", "text": "好的，那我明天再联系您", "ts": 900},
        ],
    },
    {
        "conversation_id": "CONV-0004",
        "customer_id": "C1004",
        "agent": "业务员小周",
        "outcome": "won",
        "channel": "wecom",
        "started_at": "2026-08-20T14:10:00",
        "messages": [
            {"role": "business", "text": "刘先生，您的征信情况我看了，查询14次、有3次逾期。说实话这种情况做信用贷难度大，但我们有抵押方案可以走。", "ts": 0},
            {"role": "customer", "text": "那能办下来吗？", "ts": 45},
            {"role": "business", "text": "抵押类比信用贷通过率高很多，虽然不能保证，但按您的房产评估价，额度能做到比较高。我建议您带上房产证来一趟，我们当面算一下。", "ts": 80},
            {"role": "customer", "text": "行，那我周六过去", "ts": 130},
        ],
    },
]


# =========================================================================== #
# 六、素材结构模板库（项目七：销售素材推广与客户激活）
# =========================================================================== #
# 为什么模板要写成"钩子→痛点→方案→CTA"四段而不是一段成品文案：
#   业务员真正写不出来的是"怎么抓人"（钩子）和"怎么推进"（CTA），
#   而痛点与方案其实可以复用行业知识。把结构固化成资产，模型只负责填内容，
#   产出才稳定；否则每次生成的风格飘忽，无法沉淀、无法做 A/B。
#
# 主题与"客户犹豫原因"是一一对应的（见 marketing/activation.py 的映射表）：
#   费用透明   ← 嫌利率高、嫌服务费贵
#   征信解读   ← 担心办不下来、征信有瑕疵
#   案例见证   ← 一直拖着、还在考虑
#   服务对比   ← 在比价、拿别家报价压价
#   流程科普   ← 嫌手续麻烦、不知道要交什么
#   债务置换   ← 主动求方案（高意向）
#
# 文本已刻意避开合规红线词（承诺审批结果 / 承诺利率 / 规避征信 / 绝对化用语），
# 因此这些模板的产出应当全部 approved；命中红线会走"自动改写"或"拦截转人工"。
MARKETING_BLUEPRINTS = [
    # ---------------- 朋友圈：熟人场，短、平、近 ----------------
    {
        "blueprint_id": "bp_m_swap",
        "name": "债务置换·朋友圈",
        "channel": "moments",
        "theme": "债务置换",
        "hook": "月供压得喘不过气，问题出在哪？",
        "pain": "几笔网贷加信用卡分期叠在一起，每月还款日像打仗。",
        "solution": "先把手上的负债按利率排个序，再看哪些能置换成低息长期的产品，月供结构会松很多。",
        "cta": "想看自己能不能置换，评论或私信我，帮你看一眼负债结构。",
        "tags": ["债务置换", "负债管理"],
    },
    {
        "blueprint_id": "bp_m_fee",
        "name": "费用透明·朋友圈",
        "channel": "moments",
        "theme": "费用透明",
        "hook": "担心服务费白花？说点实在的。",
        "pain": "怕前期收钱，怕办不下来钱退不回。",
        "solution": "费用口径在签约前就写清楚，办不下来不收，办成了才结算。这是最基本的诚意。",
        "cta": "想先弄清费用怎么算，私信我一句话说清。",
        "tags": ["费用透明", "避坑指南"],
    },
    {
        "blueprint_id": "bp_m_case",
        "name": "案例见证·朋友圈",
        "channel": "moments",
        "theme": "案例见证",
        "hook": "被拒过 2 次，后来为什么成了？",
        "pain": "查询次数多、有过逾期，以为自己再也没机会了。",
        "solution": "换成抵押类方案后，资质短板就不再是硬伤；关键是先看清自己手里有什么。",
        "cta": "有类似经历的，私信我聊聊，先判断有没有空间。",
        "tags": ["案例见证", "征信知识"],
    },
    {
        "blueprint_id": "bp_m_compare",
        "name": "服务对比·朋友圈",
        "channel": "moments",
        "theme": "服务对比",
        "hook": "同样的问题，为什么报价能差这么多？",
        "pain": "只看谁便宜，最后往往在材料、时效、售后上被补回来。",
        "solution": "比价看三件事：方案是不是匹配你的资质、费用是否前置透明、办不下来怎么处理。",
        "cta": "拿不定主意的，把情况发我，我按这三条帮你对一遍。",
        "tags": ["服务对比", "避坑指南"],
    },
    # ---------------- 抖音：陌生场，前 3 秒必须抓人 ----------------
    {
        "blueprint_id": "bp_d_swap",
        "name": "债务置换·抖音口播",
        "channel": "douyin",
        "theme": "债务置换",
        "hook": "网贷五六笔的人，别再一笔笔还了。",
        "pain": "每一笔利息不一样，你以为在还本金，其实大部分在填利息。",
        "solution": "正确顺序是：先把所有负债列出来算清综合成本，再用一笔低息长期的产品把高息部分置换掉。顺序颠倒，越还越多，这是最常见的坑。",
        "cta": "不知道自己能不能置换的，评论区留个 1，我看到了就回。",
        "tags": ["债务置换", "贷款攻略"],
    },
    {
        "blueprint_id": "bp_d_credit",
        "name": "征信解读·抖音口播",
        "channel": "douyin",
        "theme": "征信解读",
        "hook": "查了十几次征信，为什么反而办不下来？",
        "pain": "硬查询次数是银行直接看的指标，短时间密集申请会被判定为资金紧张。",
        "solution": "遇到这种情况，第一件事不是继续申贷，而是停下来：看清查询结构、算清可选方向，该等的就等，别把征信再查花了。",
        "cta": "不确定自己征信能不能做的，点头像问我，先判断方向再动手。",
        "tags": ["征信知识", "贷款攻略"],
    },
    {
        "blueprint_id": "bp_d_compare",
        "name": "服务对比·抖音口播",
        "channel": "douyin",
        "theme": "服务对比",
        "hook": "为什么有人办得又快又顺，有人跑三个月没结果？",
        "pain": "差别往往不在资质，而在方案选错了方向，材料反复补、时间全耗在试错上。",
        "solution": "先做资质与征信初判，确定能走的类型，再准备对应材料，一次走对；而不是拿材料去试每一家。",
        "cta": "想少走弯路的，评论区说说你的情况，我帮你判断先走哪条路。",
        "tags": ["服务对比", "贷款攻略"],
    },
    {
        "blueprint_id": "bp_d_flow",
        "name": "流程科普·抖音口播",
        "channel": "douyin",
        "theme": "流程科普",
        "hook": "做一笔贷款到底要走几步？3 分钟讲清。",
        "pain": "很多人卡在不知道下一步要交什么，于是来回跑、来回问。",
        "solution": "流程就四步：需求与资质沟通、征信与资质初判、方案确定与材料准备、签约与放款；每一步都有明确的交付物，不用猜。",
        "cta": "想提前知道每一步要准备什么，评论区留个 流程，我发你清单。",
        "tags": ["贷款攻略", "资金规划"],
    },
    # ---------------- 小红书：搜索场，第一人称经验贴 ----------------
    {
        "blueprint_id": "bp_x_fee",
        "name": "费用透明·小红书经验贴",
        "channel": "xiaohongshu",
        "theme": "费用透明",
        "hook": "在纠结服务费到底值不值？说点实话。",
        "pain": "我一开始也怕：前期把钱交了，万一办不下来，钱打水漂，还找不到人。后来才知道，正规做法是费用口径在签约前就写清楚，办不下来不收费，办成了才结算，这是最基本的诚意。",
        "solution": "把你关心的问题逐条问清、写在纸上，再决定要不要往下走。别怕问，愿意把费用讲透的人，才值得托付。",
        "cta": "有疑问的姐妹评论区问，我一条条回，不推销。",
        "tags": ["费用透明", "避坑指南", "负债管理"],
    },
    {
        "blueprint_id": "bp_x_credit",
        "name": "征信解读·小红书经验贴",
        "channel": "xiaohongshu",
        "theme": "征信解读",
        "hook": "征信被我自己查花了，后来是怎么补救的？",
        "pain": "当时急着用钱，看到点一下就能测额度就点，一个月点了十几次，等真去申请的时候才发现硬查询次数已经超标，银行直接不给过。",
        "solution": "先停下来，别再申请。把查询记录拉出来看清楚是哪一类查询、集中在什么时间，再判断是等待覆盖还是换方向做抵押类方案。方向对了，很多情况是有空间的。",
        "cta": "不确定自己征信情况的，可以评论区说说，我帮你判断要不要等。",
        "tags": ["征信知识", "负债管理", "避坑指南"],
    },
    {
        "blueprint_id": "bp_x_case",
        "name": "案例见证·小红书经验贴",
        "channel": "xiaohongshu",
        "theme": "案例见证",
        "hook": "被拒过两次的人，最后是这样办下来的。",
        "pain": "他一开始的情况挺糟：查询多、有过逾期，自己试了两家都被拒，信心基本没了，甚至觉得这辈子都办不了。",
        "solution": "我们做的第一件事不是再递材料，而是把资质重新摊开看：名下有什么、能走哪条路。换成抵押类方案后，短板就不再是硬伤。所以别急着否定自己，先让专业的人看一眼再下结论。",
        "cta": "有被拒经历的姐妹别灰心，评论区聊聊，我先帮你判断有没有空间。",
        "tags": ["案例见证", "征信知识", "负债管理"],
    },
    {
        "blueprint_id": "bp_x_flow",
        "name": "流程科普·小红书经验贴",
        "channel": "xiaohongshu",
        "theme": "流程科普",
        "hook": "办贷款要准备什么？我整理了一份清单。",
        "pain": "以前我最怕的就是来回跑：今天让补收入流水，明天说缺公积金明细，一周下来假请了三次，事情还没办成。",
        "solution": "其实流程就四步，每一步要交什么都是确定的：需求与资质沟通、征信与资质初判、方案与材料准备、签约与放款。提前拿到清单，一次备齐，基本不用来回折腾，心里也有底。",
        "cta": "需要清单的姐妹评论区留个 流程，我发你一份。",
        "tags": ["贷款攻略", "资金规划", "避坑指南"],
    },
]


# =========================================================================== #
# 七、未成单（沉睡）客户 —— 激活对象（项目七）
# =========================================================================== #
# dormant_days     距最后一次互动天数（低于阈值（默认 7 天）不打扰）
# last_touch_days  距上次被激活触达的天数（低于静默期（默认 3 天）不打扰）
# intent           意向分（来自智能客服的评级），用于算优先级（意向 × 沉睡程度）
# objection        客户自己说出的犹豫原因 → 决定用哪个主题的素材对症触达
# concern          关注点 → 决定首选渠道（可解释的业务规则，不是技术约束）
MARKETING_DORMANT_CUSTOMERS = [
    {
        "customer_id": "C1002", "name_masked": "李*士", "dormant_days": 21,
        "objection": "利率太高，服务费也不便宜", "concern": "费用",
        "intent": 0.7, "last_touch_days": 30,
    },
    {
        "customer_id": "C1005", "name_masked": "陈*女", "dormant_days": 35,
        "objection": "我这种资质担心办不下来", "concern": "征信",
        "intent": 0.8, "last_touch_days": 15,
    },
    {
        "customer_id": "C1003", "name_masked": "王*姐", "dormant_days": 12,
        "objection": "还在考虑，想先弄明白流程", "concern": "流程",
        "intent": 0.45, "last_touch_days": 10,
    },
    {
        "customer_id": "C1006", "name_masked": "赵*生", "dormant_days": 9,
        "objection": "别家给的报价更低", "concern": "费用",
        "intent": 0.6, "last_touch_days": 8,
    },
    {
        "customer_id": "C1007", "name_masked": "孙*女", "dormant_days": 2,
        "objection": "再说吧，最近忙", "concern": "征信",
        "intent": 0.5, "last_touch_days": 20,
    },
    {
        "customer_id": "C1008", "name_masked": "周*生", "dormant_days": 18,
        "objection": "想再比较一下利率", "concern": "利率",
        "intent": 0.55, "last_touch_days": 1,
    },
    {
        "customer_id": "C1009", "name_masked": "吴*女", "dormant_days": 60,
        "objection": "没时间，手续太麻烦", "concern": "流程",
        "intent": 0.35, "last_touch_days": 45,
    },
    {
        "customer_id": "C1010", "name_masked": "郑*生", "dormant_days": 25,
        "objection": "手续太麻烦，不知道要交什么", "concern": "流程",
        "intent": 0.65, "last_touch_days": 20,
    },
]


# =========================================================================== #
# 八、素材效果种子数据（项目七：归因演示）
# =========================================================================== #
# 真实环境由埋点实时写入 touch_records 表；这里给一份**演示用漏斗**，
# 让"素材 → 咨询 → 留资 → 成单"的归因报告一打开就有内容。
#
# 注意按 **渠道 + 主题** 标注，而不是 material_id：
# 素材 id 是运行时生成的（mat_md_xxxx），写死在种子文件里必然对不上，
# 归因报告就会满屏"素材档案缺失"。加载时会解析到当前素材库里同渠道同主题的素材上。
MARKETING_PERFORMANCE = [
    {"channel": "moments", "theme": "债务置换",
     "funnel": {"impression": 820, "consult": 96, "lead": 38, "deal": 6}, "ts": "2026-09-20 20:30"},
    {"channel": "douyin", "theme": "债务置换",
     "funnel": {"impression": 1500, "consult": 120, "lead": 29, "deal": 3}, "ts": "2026-09-21 12:10"},
    {"channel": "moments", "theme": "费用透明",
     "funnel": {"impression": 640, "consult": 71, "lead": 26, "deal": 4}, "ts": "2026-09-22 19:40"},
    {"channel": "xiaohongshu", "theme": "费用透明",
     "funnel": {"impression": 980, "consult": 88, "lead": 22, "deal": 2}, "ts": "2026-09-22 21:00"},
    {"channel": "douyin", "theme": "征信解读",
     "funnel": {"impression": 1850, "consult": 143, "lead": 41, "deal": 5}, "ts": "2026-09-23 12:30"},
    {"channel": "xiaohongshu", "theme": "征信解读",
     "funnel": {"impression": 760, "consult": 66, "lead": 19, "deal": 1}, "ts": "2026-09-23 22:15"},
    {"channel": "moments", "theme": "案例见证",
     "funnel": {"impression": 520, "consult": 47, "lead": 17, "deal": 3}, "ts": "2026-09-24 20:10"},
    {"channel": "xiaohongshu", "theme": "案例见证",
     "funnel": {"impression": 880, "consult": 79, "lead": 24, "deal": 2}, "ts": "2026-09-25 21:20"},
    {"channel": "moments", "theme": "服务对比",
     "funnel": {"impression": 430, "consult": 29, "lead": 8, "deal": 1}, "ts": "2026-09-26 19:50"},
    {"channel": "douyin", "theme": "服务对比",
     "funnel": {"impression": 1120, "consult": 52, "lead": 11, "deal": 1}, "ts": "2026-09-26 12:00"},
    {"channel": "douyin", "theme": "流程科普",
     "funnel": {"impression": 1340, "consult": 47, "lead": 12, "deal": 1}, "ts": "2026-09-27 12:20"},
    {"channel": "xiaohongshu", "theme": "流程科普",
     "funnel": {"impression": 610, "consult": 38, "lead": 9, "deal": 0}, "ts": "2026-09-27 21:45"},
]


# =========================================================================== #
# 九、语音客服种子数据（项目一）
# =========================================================================== #
# 场景 = 人设提示词 + 语音参数 + 检索策略。业务上就是"客户打进来时，
# AI 以什么身份、用什么口径接待"。
#
# 注意 knowledge_tags / voice_type 这两列：
#   * knowledge_tags 让"资料清单"场景只召回资料类条款，不把利率条款念出来；
#   * voice_type 留空表示用全局默认音色；填 S_xxx 就是复刻音色
#     （cluster 会跟着从 volcano_tts 切到 volcano_icl，代码里已处理）。
VOICE_SCENES = [
    {
        "scene_id": "consultant",
        "name": "助贷接待顾问",
        "description": "客户主动来电/点广告进来：快速判断需求与资质，给出方向，推进到加微信或约到店",
        "prompt_key": "consultant",
        "bot_user_id": "Huoshan01",
        "welcome_message": "您好，我是这边的顾问，您是想了解哪方面的资金需求？",
        "temperature": 0.3,
        "max_tokens": 220,
        "knowledge_limit": 2,
        "knowledge_tags": [],
        "interrupt_mode": True,
        "voice_type": "",
        "speed_ratio": 1.0,
    },
    {
        "scene_id": "amount",
        "name": "额度与资质初判",
        "description": "客户最关心能贷多少：只给区间与影响因素，绝不承诺额度",
        "prompt_key": "amount",
        "bot_user_id": "Huoshan02",
        "welcome_message": "您好，额度这块我帮您先估一下，您公积金或社保缴了多久？",
        "temperature": 0.2,
        "max_tokens": 200,
        "knowledge_limit": 2,
        "knowledge_tags": [],
        "interrupt_mode": True,
        "voice_type": "",
        # 报价类内容语速略慢一点，客户更容易记住数字
        "speed_ratio": 0.95,
    },
    {
        "scene_id": "docs",
        "name": "资料清单顾问",
        "description": "已决定办理的客户：一次说清三样资料，强调真实性",
        "prompt_key": "docs",
        "bot_user_id": "Huoshan03",
        "welcome_message": "您好，咱们先说下需要准备的资料，不复杂，就三样。",
        "temperature": 0.2,
        "max_tokens": 200,
        "knowledge_limit": 1,
        "knowledge_tags": ["资料"],
        "interrupt_mode": True,
        "voice_type": "",
        "speed_ratio": 1.0,
    },
]

# 一条"候选提示词"（v2），用来演示"改提示词必须先进候选 → 跑评测 → 再切 active"。
# 真实项目里 v2 是人写的；这里给一版更短的，方便在 demo 里看到 max_tokens 被夹住。
VOICE_PROMPT_CANDIDATE = {
    "key": "consultant",
    "system_prompt": (
        "你是助贷公司的语音接待顾问，正在和客户通电话。任务：快速判断资质、给出方向、推进下一步。\n\n"
        "【说话方式】\n"
        "- 短句口语，一条回复不超过 80 字、最多 3 句。\n"
        "- 不用列表/编号/符号，TTS 会把符号念出来。\n\n"
        "【底线】\n"
        "- 只依据下方知识库回答，库里没有就说\"这个我得让专员跟您确认\"。\n"
        "- 绝不做结果承诺：不说保证、百分百、包过、必下、无视征信。\n"
        "- 不索要验证码/密码/完整卡号；不诱导以贷养贷。\n\n"
        "【参考知识库】\n{{knowledge}}\n\n【补充信息】\n{{extra}}"
    ),
    "note": "v2 候选：更短更口语，等评测通过后再切 active",
    "temperature": 0.3,
    "max_tokens": 180,
}


# =========================================================================== #
# 写盘
# =========================================================================== #
# =========================================================================== #
# 项目八：助贷业务流程数字员工 —— 演示线索
# =========================================================================== #
# 每条线索对应 SOP 上的一个真实场景，做法是"把一条线索推进到目标阶段"，
# 而不是直接改数据库里的 stage 字段 —— 这样种子跑完，每个线索都带完整的
# 流转历史、审计流水、预约记录、准备包与费用试算，看板上的数字全部可点开追溯。
#
# 看板 7 列（待分配 / 待首触 / 跟进中 / 已邀约 / 到店推进 / 已成交 / 已流失）
# 每一列都至少有一条线索：空白看板看不出业务流转，这是给面试官看的第一屏。
#
# 字段说明：
#   stage     最终阶段
#   store     有值则走真实「邀约」接口，生成预约记录（门店 + 到店时间）
#   amount    走真实「费用试算」接口的贷款金额
#   lost_from 流失线索在哪个阶段判死（不填则直接以首个阶段流失）
FLOW_LEADS = [
    # ---------------- 待分配：广告刚进来，还没人接 ----------------
    {
        "customer_id": "C1007",
        "customer_name": "钱雨",
        "phone_masked": "135****2011",
        "source": "douyin",
        "owner": "",  # 未分配 = 还在池子里，看板落在「待分配」列
        "stage": "new",
        "note": "抖音凌晨 1 点留资，表单只有姓名手机号，等人认领",
    },
    # ---------------- 待首触：一通电话的距离 ----------------
    {
        "customer_id": "C1005",
        "customer_name": "陈静",
        "phone_masked": "139****8888",
        "source": "referral",
        "owner": "rep_01",
        "stage": "assigned",
        # 首呼 SLA 30 分钟，晾了 195 分钟 —— 已超 2×SLA，预警页最高一级
        "backdate_minutes": 195,
        "note": "老客户介绍，分配后一直没人首呼（演示 SOP 逾期预警）",
    },
    {
        "customer_id": "C1006",
        "customer_name": "赵国栋",
        "phone_masked": "133****6620",
        "source": "infoflow",
        "owner": "rep_02",
        "stage": "first_called",
        # 加微 SLA 2 小时，只超了 25 分钟 —— 预警页"刚超时·还能救"这一档
        "backdate_minutes": 145,
        "note": "首呼已通，客户同意加微信，还没加上",
    },
    # ---------------- 跟进中：最难的一段，全靠顾问节奏 ----------------
    {
        "customer_id": "C1002",
        "customer_name": "李志士",
        "phone_masked": "138****5678",
        "source": "douyin",
        "owner": "rep_01",
        "stage": "nurturing",
        "note": "抖音信息流留资，想做大额经营周转，正在跟同行比价",
    },
    {
        "customer_id": "C1009",
        "customer_name": "周敏",
        "phone_masked": "137****9032",
        "source": "wechat",
        "owner": "rep_02",
        "stage": "wechat_added",
        "note": "已加微信，客户发来 4 笔信用贷账单，等出置换方案",
    },
    # ---------------- 已邀约：等客户上门 ----------------
    {
        "customer_id": "C1003",
        "customer_name": "王娟",
        "phone_masked": "137****1234",
        "source": "infoflow",
        "owner": "rep_02",
        "stage": "invited",
        "store": "上海静安门店",
        "visit_in_hours": 30,
        "note": "已约周六下午到店，带身份证和社保记录",
    },
    # ---------------- 到店推进：接待 → 方案 → 费用，三个节点各一条 ----------------
    {
        "customer_id": "C1008",
        "customer_name": "吴建平",
        "phone_masked": "136****3388",
        "source": "douyin",
        "owner": "rep_03",
        "stage": "arrived",
        "store": "南京新街口门店",
        "visit_in_hours": -1,  # 一小时前已到店，正在前台
        "note": "今天到店，人已经在接待区，等顾问带资料过去",
    },
    {
        "customer_id": "C1010",
        "customer_name": "郑海涛",
        "phone_masked": "138****7712",
        "source": "referral",
        "owner": "rep_03",
        "stage": "proposal_ready",
        "store": "深圳福田门店",
        "visit_in_hours": -3,
        "amount": 1000000,
        "note": "房产抵押方案已出，月供可压到 1.8 万，等客户确认",
        # 方案已出 → 顾问申请发合同，等主管批（审批中心「待我审批」）
        "approvals": [("send_contract", "pending", "客户要求今晚看到合同文本")],
    },
    {
        "customer_id": "C1011",
        "customer_name": "孙丽娟",
        "phone_masked": "139****4455",
        "source": "infoflow",
        "owner": "rep_01",
        "stage": "fee_estimated",
        "store": "南京新街口门店",
        "visit_in_hours": -5,
        "amount": 600000,
        "note": "费用已试算，客户认可服务费区间，等签约",
        "approvals": [
            ("confirm_fee", "pending", "客户要求书面确认服务费口径"),
            # 已批准但还没执行 —— 审批中心「已批准待执行」就是教这个
            ("send_contract", "approved", "合同条款已与客户对齐"),
        ],
    },
    # ---------------- 已成交：走完审批 + 执行的完整链路 ----------------
    {
        "customer_id": "C1001",
        "customer_name": "张建国",
        "phone_masked": "138****1234",
        "source": "douyin",
        "owner": "rep_01",
        "stage": "signed",
        "store": "南京新街口门店",
        "visit_in_hours": -72,
        "amount": 300000,
        "note": "30 万债务置换已签约，等放款收佣",
        # 真实链路：申请 → 主管批准 → 执行（先 assert_approved 再动 CRM）
        "approvals": [("confirm_fee", "executed", "客户确认 2% 服务费，同意签约")],
    },
    # ---------------- 已流失：终态，状态机冻结 ----------------
    {
        "customer_id": "C1004",
        "customer_name": "刘强",
        "phone_masked": "136****4321",
        "source": "douyin",
        "owner": "rep_03",
        "stage": "lost",
        "lost_from": "first_called",
        "lost_reason": "征信近半年查询 14 次、有 45 天逾期，暂不符合准入",
        "note": "首呼后初审即判死，客户自己也知道征信有问题",
    },
]

# 推进阶段时写进流转历史的业务理由（演示数据也要说人话：
# 每一行流转记录都该像是真做过这件事，而不是"演示数据推进"）
_FLOW_STAGE_REASON = {
    "assigned": "线索已分配给顾问",
    "first_called": "完成首呼，客户接听并了解产品",
    "wechat_added": "已加上客户微信，发送了产品简介",
    "nurturing": "持续跟进，客户在做同行比价",
}

# 推进到目标阶段要经过的路径（与状态机的合法流转一致）
_FLOW_STAGE_PATH = [
    "assigned", "first_called", "wechat_added",
    "nurturing", "invited", "arrived",
    "proposal_ready", "fee_estimated", "signed",
]


def seed_flow_leads(settings) -> None:
    """把演示线索写进流程库（可重复执行：每次都是同一份干净数据）。

    每条线索都走真实业务接口：邀约走 invite（生成预约记录）、到店走
    confirm_arrival、方案走 prepare_arrival（生成一页纸，含项目二给的
    方案初稿）、费用走 estimate_fee、高风险动作走 request_high_risk +
    decide + execute。所以前端每一屏看到的数据，都能在同一条线索的
    审计流水里对上。

    provider 直接复用装配层那两个工厂（而不是在这里重写一遍字段映射），
    保证"种子数据"和"线上真实请求"走的是同一份口径。

    **先清表再灌**：演示数据要的是"每次跑完都长一个样"。如果只做增量，
    时间轴摊平（`_spread_timeline`）会以已经摊过的值为锚点再摊一次，
    跑两遍"停留时长"就翻倍了 —— 那种"越跑越歪"的种子最难排查。
    """
    from app_context import _make_plan_provider, _make_profile_provider
    from flow_ops.domain import LeadStage
    from flow_ops.service import FlowOpsService
    from product_assistant.service import ProductAssistantService

    product = ProductAssistantService(settings=settings)
    product.bootstrap()  # 加载产品/档案 + 建索引，否则 match 会返回"共评估 0 个产品"
    svc = FlowOpsService(
        settings=settings,
        plan_provider=_make_plan_provider(product),
        profile_provider=_make_profile_provider(product),
    )
    try:
        svc.bootstrap()
        _clear_flow_tables(svc.engine.store)
        created = 0
        backdated: set[str] = set()
        for spec in FLOW_LEADS:
            lead = svc.intake_lead(
                customer_id=spec["customer_id"],
                customer_name=spec["customer_name"],
                phone_masked=spec["phone_masked"],
                source=spec["source"],
                owner=spec["owner"],
                actor=spec.get("owner") or "rep_01",
                note=spec.get("note", ""),
            )
            if lead["idempotent_hit"]:
                continue
            # 这两条是"故意做成逾期"的，`stage_entered_at` 是演示的锚点，
            # 后面摊时间轴时不能再错开它们，否则 165/25 分钟就变味了
            if spec.get("backdate_minutes"):
                backdated.add(lead["lead_id"])
            lead_id = lead["lead_id"]
            actor = spec.get("owner") or "rep_01"  # 流转历史里记的是真人，不是"seed"
            target = LeadStage(spec["stage"])
            stop_at = spec.get("lost_from") or spec["stage"]
            for stage in _FLOW_STAGE_PATH:
                if _rank(stage) > _rank(stop_at):
                    break
                if stage == "invited" and spec.get("store"):
                    svc.invite(
                        lead_id,
                        store=spec["store"],
                        scheduled_at=_visit_time(spec.get("visit_in_hours", 24)),
                        actor=actor,
                        note="已与客户确认到店时间",
                    )
                elif stage == "arrived":
                    svc.confirm_arrival(lead_id, actor=actor)
                elif stage == "proposal_ready":
                    svc.prepare_arrival(lead_id, actor=actor)
                elif stage == "fee_estimated":
                    svc.estimate_fee(
                        lead_id,
                        loan_amount=float(spec.get("amount") or 300000),
                        actor=actor,
                    )
                else:
                    svc.advance(
                        lead_id, stage, actor=actor,
                        reason=_FLOW_STAGE_REASON.get(stage, "阶段推进"),
                    )
            if target is LeadStage.LOST:
                svc.advance(lead_id, "lost", actor=actor,
                            reason=spec.get("lost_reason", "不符合准入条件"))
            if spec.get("backdate_minutes"):
                # 制造 SOP 逾期：把进入本阶段的时间往前推。
                # 两个不同量级各来一条，预警页的"分级"才有意义：
                #   超 2×SLA 的事基本已经黄了，必须先救；
                #   刚超一点的还来得及，只是提醒。
                stored = svc.engine.store.get_lead(lead_id)
                stored.stage_entered_at = time.time() - spec["backdate_minutes"] * 60
                svc.engine.store.update_lead(stored)
            _seed_approvals(svc, lead_id, spec)
            created += 1
        _spread_timeline(svc.engine.store, fixed_anchors=backdated)
        # 扫一轮 SOP：真实环境里调度器启动即扫一次，种子也该如此，
        # 否则"预警页"打开是空的 —— 逾期线索明明就在那里，系统却没揪出来，
        # 正好是这套系统要解决的第一个问题。
        swept = svc.check_sop(actor="seed")
        snap = svc.metrics_snapshot()
        print(f"   流程线索  {created} 条新建（共 {snap['leads']['total']} 条，"
              f"活跃 {snap['leads']['active']}，待审批 {snap['hitl']['pending']}，"
              f"预警 {swept.get('count', 0)}）")
    finally:
        svc.close()


# 一条线索从进来到成交，真实节奏是"按天"的：首呼当天、加微当天、跟进两三天、
# 邀约到店隔一天、到店当天出方案和费用。但种子脚本一次跑完，所有动作都落在
# 同一秒 —— 客户手机上的"办理记录"会出现 8 个同一时刻的节点，一眼就看得出
# 是假数据。
#
# 所以按阶段给一个"距线索进来多少小时"的偏移，把时间轴摊回去。
# 只在夹具层做（脚本直连 SQLite 改 ts），生产代码里没有任何"改审计时间"
# 的入口 —— 能改的审计就不叫审计了。
_STAGE_HOURS = {
    "new": 0.0,
    "assigned": 0.2,
    "first_called": 1.0,
    "wechat_added": 2.0,
    "nurturing": 3.0,
    "invited": 27.0,
    "arrived": 30.0,
    "proposal_ready": 30.5,
    "fee_estimated": 31.0,
    "signed": 32.0,
    "lost": 5.0,
}


def _visit_time(hours: float) -> float:
    """邀约到店的时间落在营业时段（10:00 / 14:00 / 16:00）。

    直接 `now + 24h` 会算出"明天 20:54 到店"——门店那时候已经关门了，
    客户看着手机上这条预约只会觉得系统不靠谱。
    """
    hour = 10 if hours <= 20 else 14 if hours <= 30 else 16
    stage = time.localtime(time.time() + max(hours, 1) * 3600)
    when = time.mktime((stage.tm_year, stage.tm_mon, stage.tm_mday, hour, 0, 0, 0, 0, -1))
    if when <= time.time():
        when += 86400
    return when


def _clear_flow_tables(store) -> None:
    """清空流程库的演示数据（不删库文件，也不动 locks 表）。

    顺序无所谓（没有外键约束），但一次清干净：只清 leads 不清 audit_log，
    会留下"客户已经不存在了"的孤儿审计，最容易被当成 bug 报上来。
    """
    conn = sqlite3.connect(str(store.db_path))
    try:
        for table in (
            "leads",
            "transitions",
            "appointments",
            "sop_alerts",
            "approvals",
            "audit_log",
            "idempotency_keys",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()


def _work_hours(ts: float, slot: int = 0) -> float:
    """把落在凌晨/深夜的节点挪回工作时段。

    `_STAGE_HOURS` 是从"进入当前阶段"倒推的，倒推出来常常落在清晨 5 点 ——
    客户手机上显示"06:46 已电话沟通"，谁都看得出这份数据是编的。
    09:00 之前的一律推到当天 09:00 之后，21:00 之后的压到 21:00。

    `slot` 是"这条线索里第几个被挪回来的节点"，用来错开分钟数：
    同一天四条全挤在 09:00 看着还是假，改成 09:00 / 09:30 / 10:00 / 10:30
    就是"一个上午把首触做完"的正常节奏。只动展示用的时间，不动
    `stage_entered_at` —— 那是 SOP 的判据。
    """
    local = time.localtime(ts)
    if local.tm_hour < 9:
        base = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 9, 0, 0, 0, 0, -1))
        return base + slot * 30 * 60
    if local.tm_hour >= 21:
        return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 21, 0, 0, 0, 0, -1))
    return ts


def _spread_timeline(store, fixed_anchors: set[str] | None = None) -> None:
    """把演示线索的流转记录与审计流水摊到一条像样的时间轴上。

    锚点用 `stage_entered_at`（哪一刻进入当前阶段是已知的），倒推出线索
    进入时间，再把每个阶段按 `_STAGE_HOURS` 落位。审计流水按插入顺序在这
    条区间里线性铺开，只保证"顺序对、时间不同"，不追求与流转记录逐条对齐；
    `sop.*` 的条目不动 —— 预警是刚刚扫出来的，时间必须是真的。

    `fixed_anchors` 里的线索（故意做成逾期的那两条）保持原时间不动：
    它们的分级演示依赖"超时 165 分钟"和"超时 25 分钟"这两个准确值。
    """
    fixed = fixed_anchors or set()
    conn = sqlite3.connect(str(store.db_path))
    try:
        # 种子可以反复跑（幂等），但每跑一次就会留一条"重复录入被拦截"的
        # 审计 —— 那是重复执行脚本的噪音，不是业务事件，先清掉再摊时间轴。
        conn.execute("DELETE FROM audit_log WHERE result='idempotent_hit'")
        # 拦截类条目要保留真实时间，不参与摊平
        conn.execute("UPDATE audit_log SET ts=? WHERE result<>'ok'", (time.time(),))
        rows = conn.execute(
            "SELECT lead_id, stage, stage_entered_at, created_at FROM leads ORDER BY lead_id"
        ).fetchall()
        for index, (lead_id, stage, entered, created) in enumerate(rows):
            # 11 条线索是同一次脚本跑出来的，`stage_entered_at` 会是同一秒 ——
            # "最近动态"就会显示 11 条一模一样的时间，同样一眼假。
            # 按线索序号错开最多 90 分钟，仅用于展示层的错峰。
            stagger = 0 if lead_id in fixed else (index * 17 % 90) * 60
            anchor = (entered or created or time.time()) - stagger
            base = anchor - _STAGE_HOURS.get(stage, 0.0) * 3600
            conn.execute(
                "UPDATE leads SET created_at=?, stage_entered_at=? WHERE lead_id=?",
                # 线索进入时间同样落到工作时段，否则详情里会出现"创建于 07:58"
                (_work_hours(base), anchor, lead_id),
            )

            slot = 0
            prev = 0.0
            for transition_id, to_stage in conn.execute(
                "SELECT transition_id, to_stage FROM transitions WHERE lead_id=? ORDER BY rowid",
                (lead_id,),
            ).fetchall():
                raw = base + _STAGE_HOURS.get(to_stage, 1.0) * 3600
                ts = _work_hours(raw, slot)
                if ts != raw:
                    slot += 1
                # 时间不许倒流：把凌晨的节点往前挪以后，可能顶到下一个节点后面去
                ts = max(ts, prev + 600)
                prev = ts
                conn.execute("UPDATE transitions SET ts=? WHERE transition_id=?", (ts, transition_id))

            entries = conn.execute(
                "SELECT entry_id, action FROM audit_log "
                "WHERE target_type='lead' AND target_id=? AND result='ok' ORDER BY rowid",
                (lead_id,),
            ).fetchall()
            shifts = [(eid, act) for eid, act in entries if not act.startswith("sop.")]
            if len(shifts) > 1:
                span = anchor - base
                slot = 0
                prev = 0.0
                for ordinal, (entry_id, _action) in enumerate(shifts):
                    if span > 0:
                        raw = base + span * ordinal / (len(shifts) - 1)
                    else:
                        # 刚进来的线索（今天才到「新线索」）没有历史可摊，
                        # 每 5 分钟往回铺一条 —— 绝不能铺到未来去
                        raw = anchor - (len(shifts) - 1 - ordinal) * 300
                    ts = _work_hours(raw, slot)
                    if ts != raw:
                        slot += 1
                    ts = max(ts, prev + 300)
                    prev = ts
                    conn.execute("UPDATE audit_log SET ts=? WHERE entry_id=?", (ts, entry_id))
        conn.commit()
    finally:
        conn.close()


def _seed_approvals(svc, lead_id: str, spec: dict) -> None:
    """按 spec 生成高风险审批单（pending / approved / executed 三态）。

    三种状态是刻意都留的：审批中心那一页要教的就是
    「待批 → 已批准（还没发） → 已执行」这三步不能混。
    """
    customer_id = spec["customer_id"]
    texts = {
        "send_contract": f"客户 {customer_id} 的合同文本，请查收确认",
        "confirm_fee": f"客户 {customer_id} 的服务费口径确认，请查收",
    }
    for action, state, reason in spec.get("approvals", []):
        appr = svc.request_high_risk(
            action,
            lead_id=lead_id,
            payload={"customer_id": customer_id, "text": texts.get(action, "")},
            requester=spec.get("owner") or "rep_01",
            reason=reason,
        )
        if state in ("approved", "executed"):
            svc.decide_approval(
                appr["approval_id"], approver="mgr_01", approved=True,
                comment="已核对资质与报价口径，同意",
            )
        if state == "executed":  # 批准 ≠ 已执行：执行必须单独一次调用
            svc.execute_approved(appr["approval_id"], actor=spec.get("owner") or "rep_01")


def _rank(stage: str) -> int:
    from flow_ops.domain import LeadStage
    from flow_ops.workflow import stage_rank

    return stage_rank(LeadStage(stage))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("写入 %s（%d 条）", path, len(payload) if hasattr(payload, "__len__") else 1)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("写入 %s（%d 行）", path, len(rows))


def seed_voice_prompts(settings) -> None:
    """写出语音提示词库（v1 active + v2 候选）。

    为什么不直接写 json 文件：提示词库的版本号/status 是 PromptRegistry 维护的，
    手写 json 很容易写出两个 active 版本（切版本就失效了）。所以这里复用注册表。

    幂等：已有 v2 及以上就不再追加，重复跑 seed 不会堆出一堆 v3/v4。
    """
    from voice_service.prompt import PromptRegistry

    registry = PromptRegistry(settings.voice_abs_dir / "prompts.json")
    registry.load()
    key = VOICE_PROMPT_CANDIDATE["key"]
    if len(registry.versions(key)) > 1:
        logger.info("语音提示词已存在候选版本，跳过追加（%s）", key)
        return
    registry.add(
        key,
        VOICE_PROMPT_CANDIDATE["system_prompt"],
        note=VOICE_PROMPT_CANDIDATE["note"],
        temperature=VOICE_PROMPT_CANDIDATE["temperature"],
        max_tokens=VOICE_PROMPT_CANDIDATE["max_tokens"],
        activate=False,          # 候选版本：必须跑过评测再切
    )


def seed_data_platform(settings) -> None:
    """项目四：生成"微信本地库"结构的数据源（离线夹具）。

    为什么生成的是 SQLite 而不是 JSONL：
        真实抽取路径是"读微信 MSG 库"，夹具按同一张表结构生成，
        才能验到 wxid→显示名、群消息发送者还原、消息类型占位这些真问题。

    同时**清空中台自己的库**：seed 的语义是"重置演示数据"，
    否则重跑会看到上一轮的审核结论与导出记录，漏斗数字对不上。
    用 SQL 清表而不是删文件 —— 开发服务器可能正开着这个库。
    """
    from data_platform.source import DEMO_SESSIONS, build_wechat_fixture
    from data_platform.store import DataPlatformStore

    store = DataPlatformStore(settings.data_sqlite_abs_path)
    try:
        store.reset()
    finally:
        store.close()
    path = build_wechat_fixture(settings.data_source_abs_path, DEMO_SESSIONS)
    messages = sum(len(s.get("messages") or []) for s in DEMO_SESSIONS)
    print(f"   数据中台源库 {path.name}（{messages} 条消息 / {len(DEMO_SESSIONS)} 个会话）")


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()

    write_json(settings.policy_abs_dir / "products.json", PRODUCTS)
    write_jsonl(settings.policy_abs_dir / "policies.jsonl", build_policy_documents())
    write_jsonl(settings.policy_abs_dir / "conversations.jsonl", CONVERSATIONS)
    write_json(settings.profile_abs_dir / "customers.json", CUSTOMERS)
    write_json(settings.profile_abs_dir / "personas.json", PERSONAS)

    # ---- 项目七：素材推广与客户激活 ----
    write_json(settings.marketing_abs_dir / "blueprints.json", MARKETING_BLUEPRINTS)
    write_json(settings.marketing_abs_dir / "dormant_customers.json", MARKETING_DORMANT_CUSTOMERS)
    write_json(settings.marketing_abs_dir / "material_performance.json", MARKETING_PERFORMANCE)

    # ---- 项目一：语音客服 ----
    write_json(settings.voice_abs_dir / "scenes.json", VOICE_SCENES)
    seed_voice_prompts(settings)

    # ---- 项目八：流程数字员工（直接写流程库，幂等）----
    seed_flow_leads(settings)

    # ---- 项目四：对话数据中台（生成微信库结构的源数据）----
    seed_data_platform(settings)

    print("\n✅ 演示数据已生成：")
    print(f"   产品      {len(PRODUCTS)} 款")
    print(f"   政策条款  {len(build_policy_documents())} 条")
    print(f"   客户档案  {len(CUSTOMERS)} 位")
    print(f"   客户画像  {len(PERSONAS)} 个（话术陪练用）")
    print(f"   历史对话  {len(CONVERSATIONS)} 段（质检/话术挖掘用）")
    print(f"   素材模板  {len(MARKETING_BLUEPRINTS)} 条（素材生成用）")
    print(f"   沉睡客户  {len(MARKETING_DORMANT_CUSTOMERS)} 位（激活对象）")
    print(f"   效果漏斗  {len(MARKETING_PERFORMANCE)} 组（归因演示用）")
    print(f"   语音场景  {len(VOICE_SCENES)} 个（语音客服接待用）")
    print(f"   提示词    {VOICE_PROMPT_CANDIDATE['key']} v1（active）+ v2（候选）")
    print(f"   流程线索  {len(FLOW_LEADS)} 条（覆盖看板 7 列，含 1 条首呼逾期 + 4 张审批单）")
    print("   数据中台  7 个会话 / 33 条消息（覆盖 9 类分类与脱敏边界）")
    print(f"\n   数据目录：{settings.policy_abs_dir.parent}")


if __name__ == "__main__":
    main()
