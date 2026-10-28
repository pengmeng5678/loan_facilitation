"""项目八：助贷业务流程数字员工。

模块流向：

    api.py        HTTP 接口（/flow/*）
    agent.py      平台 Agent（路由 / 作用域 / 决策 / 应答）
    service.py    门面（六组能力 + 工具注册 + 评测）
    workflow.py   FlowEngine：数字员工本体（幂等 + 锁 + 状态机 + 审计）
    leads.py      状态机与 SOP 规则（确定性判据）
    readiness.py  到店前准备（档案 + 方案初稿 + 费用预估）
    fees.py       费用试算（等额本息 + 分档服务费）
    approval.py   HITL 审批门（高风险动作必须人批）
    progress.py   客户自助查进度（身份校验 + 脱敏）
    idempotency.py 幂等键 + 分布式锁
    audit.py      全程审计留痕
    crm.py        CRM / 企微 OpenAPI 抽象（离线可替换）
    store.py      SQLite 持久化（八张表）
    metrics.py    流程指标（漏跟进率 / 准备时长 / 幂等拦截 / HITL）
    evaluation.py 流程行为 Golden Set 与 CI 准入
    domain.py     领域模型（含人机边界定义）

**业务边界（代码级保证，不是文档约定）**：
    自动化 = 咨询 → 邀约 → 方案生成 → 费用评估
    人工节点 = 接待 / 谈判 / 费用确认（HumanStage，Agent 禁区）
    高风险动作 = send_contract / confirm_fee（必须 ApprovalGate 放行）
"""

from flow_ops.service import FlowOpsService

__all__ = ["FlowOpsService"]
