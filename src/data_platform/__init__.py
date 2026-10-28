"""项目四：助贷对话数据中台（Data Platform）。

一句话定位：**把微信原始聊天记录加工成可训练、可检索、可考核的数据资产**，
成为其他所有 AI 项目的数据源头。

数据流（四层，每层职责单一）：

    原始层 raw_messages（不可变，只增不改）
        │  抽取：微信本地 SQLite（MSG0.5.db / MicroMsg.db）
        │        wxid → 显示名、群消息发送者还原、时间过滤、去重
        ▼
    清洗层 conversation_blocks（过滤 + 脱敏 + 切块 + 预标注）
        │  9 级分类、三层冗余脱敏、价格硬红线、5 分钟时间窗切会话、
        │  同角色合并、QA 抽取、质量分
        ▼
    审核层 staging_conversations（人工闸门：approve / reject / edit）
        │  只有 approved 才能进下游 —— 这是"唯一可信数据源"的落地
        ▼
    复用层 exports / knowledge_chunks / knowledge_articles
           多格式导出（ShareGPT / Alpaca / OpenAI / RAG CSV）+ 知识产物

设计原则（与全仓一致）：
    · **业务判据用确定性代码**：分类、脱敏、切块、质量分全是规则，
      LLM 只用于"从对话里提炼结构化知识"这一层语言任务；
    · **离线可跑**：微信源库有真实适配器，也有夹具生成器；
      Embedding 走运行时注入（本地实现零密钥）；
    · **合规是流程约束**：价格脱敏是硬红线、脱敏三层冗余、
      敏感对话永远不进训练集（dataset_eligible=False）；
    · **写操作幂等 + 留痕**：块 id / 暂存 id 由内容哈希派生，
      审核与导出都有审计记录。
"""
