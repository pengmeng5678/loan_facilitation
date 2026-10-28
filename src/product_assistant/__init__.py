"""项目二：助贷产品知识助手与智能方案匹配。

把两件事交给 AI：
    1. 产品政策问答 —— 可检索、可溯源、无依据一律拒答（合规底线）
    2. 方案匹配决策 —— 把"资质 × 征信 × 需求 → 产品组合"从老师傅经验
       变成可复制、可回测的结构化流程

本包通过 agent_platform 的 AgentCapabilities 装配到平台上，
因此自动获得状态持久化、HITL 审批、失败自愈、作用域裁剪与全链路观测。
"""

from product_assistant.service import ProductAssistantService

__all__ = ["ProductAssistantService"]
