"""项目七：销售素材推广与客户激活 AI。

背景（很重要，决定了这个包为什么这么设计）：
    公司**没有内容自运营账号**，获客第二条腿是**业务员的社交传播** ——
    成单后业务员会在微信朋友圈 / 抖音 / 小红书发素材，既拉新客、也激活
    还在考虑的未成单客户。

所以这个包做四件事，边界清晰：
    ① 素材生成与渠道语感适配 —— 产得多、风格对
    ② 成单案例脱敏包装     —— 把最有说服力的"客户证言"变成可用素材
    ③ 未成单客户激活       —— 给"考虑中"的客户对症的素材与节奏
    ④ 合规先审后发 + 效果归因 —— 不违规，且知道哪种素材真的带来线索

**边界**：AI 只做"素材生成与建议"，**发布动作与对客沟通由业务员完成**
（发布/发送是高风险非幂等动作，走平台 HITL 审批）。
"""

from marketing.domain import (
    ActivationPlan,
    ActivationTask,
    AttributionReport,
    Blueprint,
    CaseStudy,
    Channel,
    ComplianceFinding,
    ComplianceReport,
    MarketingStats,
    MaterialDraft,
    TouchRecord,
)
from marketing.service import MarketingService

__all__ = [
    "ActivationPlan",
    "ActivationTask",
    "AttributionReport",
    "Blueprint",
    "CaseStudy",
    "Channel",
    "ComplianceFinding",
    "ComplianceReport",
    "MarketingService",
    "MarketingStats",
    "MaterialDraft",
    "TouchRecord",
]
