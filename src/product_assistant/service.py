"""产品知识助手服务（项目二的门面）。

对外提供三个能力，正好对应业务的三件事：
    answer_question     产品政策问答（可溯源，无依据拒答）
    match_customer      方案匹配决策（结构化、可解释、可回测）
    generate_proposal   方案书生成（分钟级、可发送客户）

同时负责把这三个能力**注册成平台工具**，并声明自己的 AgentCapabilities。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from agent_platform.config import Settings, get_settings
from agent_platform.errors import PolicyNotFoundError
from agent_platform.llm.base import EmbeddingProvider, LLMProvider, Message
from agent_platform.llm.providers import build_embedder, build_llm
from agent_platform.logging_config import get_logger
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from product_assistant.domain.matching import MatchingEngine
from product_assistant.domain.models import (
    MatchResult,
    Proposal,
    RetrievedChunk,
)
from product_assistant.knowledge.graph_rag import ProductGraph
from product_assistant.knowledge.repository import PolicyRepository, ProfileStore
from product_assistant.proposal import ProposalBuilder
from product_assistant.rag.hybrid import HybridRetriever
from product_assistant.rag.vector_store import build_vector_store
from product_assistant.rag.verify import verify_answer

logger = get_logger(__name__)


class QAAnswer(BaseModel):
    """一次产品问答的结果。"""

    question: str
    answer: str
    citations: list[str] = Field(default_factory=list, description="引用溯源（可跳原文）")
    refused: bool = Field(False, description="是否因无政策依据而拒答")
    reason: str = Field("", description="拒答/说明理由")
    verified: bool = Field(True, description="答案是否通过引用一致性校验")
    verify_issues: list[str] = Field(default_factory=list, description="校验发现的不一致项")
    related_products: list[str] = Field(default_factory=list, description="图谱推荐的关联产品编号")
    hits: list[dict[str, Any]] = Field(default_factory=list, description="检索命中的原始信息（排查用）")


class ProductAssistantService:
    """产品知识助手。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMProvider | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or build_llm(self.settings)
        self.embedder = embedder or build_embedder(self.settings)

        # ---- 数据层 ----
        self.repository = PolicyRepository(self.settings.policy_abs_dir)
        self.profiles = ProfileStore(self.settings.profile_abs_dir)
        self.graph = ProductGraph()

        # ---- 检索层 ----
        self.store = build_vector_store(self.settings)
        self.retriever = HybridRetriever(
            self.embedder,
            self.store,
            min_rerank_score=self.settings.rag_min_rerank_score,
            min_dense_score=self.settings.rag_min_dense_score,
        )

        # ---- 决策层 ----
        self.matcher = MatchingEngine()
        self.proposal_builder = ProposalBuilder(llm=self.llm)

        self._indexed = False

    # ------------------------------------------------------------------ #
    # 初始化与索引
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> dict[str, Any]:
        """加载数据 → 建图谱 → 建检索索引。启动时调用一次。"""
        self.repository.load()
        self.profiles.load()
        self.graph.build(self.repository.list_products())

        chunks = self.repository.all_chunks()
        if chunks:
            self.retriever.index(chunks)
            self._indexed = True
        else:
            logger.warning("政策语料为空，检索不可用（请先运行 scripts/seed_data.py 灌入演示数据）")

        stats = {
            **self.repository.stats(),
            "profiles": len(self.profiles.all()),
            "graph": self.graph.stats(),
            "retriever": self.retriever.stats(),
        }
        logger.info("产品助手初始化完成：%s", stats)
        return stats

    # ------------------------------------------------------------------ #
    # 能力一：产品政策问答
    # ------------------------------------------------------------------ #
    def answer_question(
        self,
        question: str,
        *,
        as_of: date | None = None,
        product_code: str = "",
    ) -> QAAnswer:
        """基于政策原文回答产品问题。

        合规硬规则：**检索不到可支撑的条款就拒答**，绝不编造利率/额度/通过率。
        """
        as_of = as_of or date.today()

        hits: list[RetrievedChunk] = self.retriever.retrieve(
            question, top_k=4, as_of=as_of, product_code=product_code or None
        )

        if not hits:
            # —— 拒答路径：助贷行业的底线，"不敢承诺"本身就是产品能力 ——
            logger.info("产品问答无政策依据，执行拒答：%s", question[:50])
            return QAAnswer(
                question=question,
                answer=(
                    "抱歉，我未检索到可支撑该问题的现行政策条款。为避免给您错误信息，"
                    "我不做推测性回答——建议转人工顾问核实，或提供具体的产品编号我再查一次。"
                ),
                refused=True,
                reason="无政策依据（检索结果为空或低于相关度阈值）",
            )

        citations = [h.citation.render() for h in hits]
        answer = self._synthesize_answer(question, hits)

        # —— 引用闭环：答案里的数字/编号必须能在引用原文里找到 ——
        # 不通过就按"无依据"处理，绝不把"看起来有引用"的错误数字发给客户。
        verdict = verify_answer(answer, hits)
        if not verdict.ok:
            logger.warning("答案未通过引用校验：%s", verdict.issues())
            return QAAnswer(
                question=question,
                answer=(
                    "抱歉，我生成的回答与现行政策原文存在不一致，为避免误导，"
                    "本次不做结论性回答——建议转人工顾问核实后再答复客户。"
                ),
                refused=True,
                reason="答案与政策原文不一致，已拦截",
                verified=False,
                verify_issues=verdict.issues(),
                hits=[
                    {
                        "chunk_id": h.chunk_id,
                        "score": h.score,
                        "citation": h.citation.render(),
                    }
                    for h in hits
                ],
            )
        if not verdict.usage_uncertain and verdict.used_hits:
            citations = list(dict.fromkeys(h.citation.render() for h in verdict.used_hits))

        # 图谱补充：从命中的产品延伸出"替代产品"，让业务员一次拿到更多选择
        related: list[str] = []
        for h in hits:
            code = h.citation.product_code
            if code and code not in related:
                related.append(code)
        for code in list(related):
            for sub, _score in self.graph.substitutes(code):
                if sub not in related:
                    related.append(sub)

        return QAAnswer(
            question=question,
            answer=answer,
            citations=citations,
            related_products=related[:5],
            hits=[
                {
                    "chunk_id": h.chunk_id,
                    "score": h.score,
                    "bm25": h.bm25_score,
                    "dense": h.dense_score,
                    "rerank": h.rerank_score,
                    "citation": h.citation.render(),
                }
                for h in hits
            ],
        )

    def _synthesize_answer(self, question: str, hits: list[RetrievedChunk]) -> str:
        """把检索到的政策条款组织成回答。

        离线模式（echo）走"抽取式"：直接引用条款原文，天然不会幻觉；
        接入真实模型时走"生成式"：把条款作为唯一依据让模型组织语言，并强制保留引用。
        """
        contexts = "\n\n".join(
            f"【条款 {h.citation.policy_id}】{h.parent_text}" for h in hits
        )

        if getattr(self.llm, "name", "echo") == "echo":
            # 抽取式回答：最安全的做法 —— 直接用原文，杜绝幻觉
            lines = [f"根据现行政策条款，关于您的问题（{question}），有以下依据："]
            for h in hits[:3]:
                lines.append(f"- {h.parent_text.strip()}")
            lines.append("以上信息来自银行政策原文，最终以银行审批为准。")
            return "\n".join(lines)

        system = (
            "你是助贷产品政策助手。**只能依据提供的政策条款回答**，"
            "不得编造利率、额度、通过率。若条款不足以回答，请明确说明'政策未覆盖'。"
            "回答末尾用【引用】标出使用的条款编号。"
        )
        user = f"问题：{question}\n\n可用政策条款：\n{contexts}"
        resp = self.llm.complete(
            [Message(role="system", content=system), Message(role="user", content=user)]
        )
        return resp.content.strip()

    # ------------------------------------------------------------------ #
    # 能力二：结构化方案匹配
    # ------------------------------------------------------------------ #
    def match_customer(
        self, customer_id: str, *, as_of: date | None = None
    ) -> MatchResult:
        """为客户做方案匹配决策。"""
        profile = self.profiles.get(customer_id)
        if profile is None:
            raise PolicyNotFoundError(f"客户档案不存在：{customer_id}")

        as_of = as_of or date.today()
        result = self.matcher.match(profile, self.repository.list_products(), as_of=as_of)

        # 图谱做二次补充：硬筛全被拒时，用"资质-用途"多跳找更宽的产品
        if not result.matched and not result.alternates:
            suggestions = self.graph.recommend_by_tags(
                profile.qualifications, profile.needs, top_k=3
            )
            if suggestions:
                codes = [code for code, _score in suggestions if code in self.repository.products]
                if codes:
                    relaxed = self.matcher.match(
                        profile,
                        [self.repository.products[c] for c in codes],
                        as_of=as_of,
                    )
                    if relaxed.matched:
                        result = relaxed
                        result.handoff_reason = ""
                        logger.info("硬筛全拒后经图谱多跳找到替代方案：%s", codes)

        return result

    # ------------------------------------------------------------------ #
    # 能力三：方案书生成
    # ------------------------------------------------------------------ #
    def generate_proposal(
        self, customer_id: str, *, as_of: date | None = None
    ) -> Proposal:
        """生成方案书。无匹配结果时抛异常，由上层转人工（不硬配）。"""
        profile = self.profiles.get(customer_id)
        if profile is None:
            raise PolicyNotFoundError(f"客户档案不存在：{customer_id}")

        match = self.match_customer(customer_id, as_of=as_of)
        if not match.matched:
            raise PolicyNotFoundError(match.handoff_reason or "无匹配产品，建议转人工专家")

        return self.proposal_builder.build(profile, match, self.repository.products)

    # ------------------------------------------------------------------ #
    # 工具注册（装配到平台）
    # ------------------------------------------------------------------ #
    def register_tools(self, registry: ToolRegistry) -> list[str]:
        """把本服务的三个能力注册成平台工具，供 Agent 编排调用。"""
        registered: list[str] = []

        # ---- 工具 1：政策检索（RAG）----
        def _search_policy(query: str, product_code: str = "", as_of: str = "") -> dict[str, Any]:
            parsed = date.fromisoformat(as_of) if as_of else None
            result = self.answer_question(query, as_of=parsed, product_code=product_code)
            return result.model_dump()

        registry.register(
            ToolSpec(
                name="product.search_policy",
                description=(
                    "检索银行产品政策条款并给出带引用的回答。适用于产品利率、额度、"
                    "准入条件、资料清单等问题。**无政策依据时返回 refused=true**，"
                    "此时必须转人工，不要自行编造答案。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "客户/业务员的问题原文"},
                        "product_code": {"type": "string", "description": "限定产品编号（可选）"},
                        "as_of": {"type": "string", "description": "政策基准日期 YYYY-MM-DD（可选）"},
                    },
                    "required": ["query"],
                },
                handler=_search_policy,
                tags=("product", "rag", "read"),
            )
        )
        registered.append("product.search_policy")

        # ---- 工具 2：产品详情 ----
        def _get_detail(product_code: str) -> dict[str, Any]:
            product = self.repository.get_product(product_code)
            if product is None:
                return {"found": False, "product_code": product_code}
            return {"found": True, "product": product.model_dump(mode="json")}

        registry.register(
            ToolSpec(
                name="product.get_detail",
                description="按产品编号查询产品完整要素（额度区间、利率、期限、资料、流程）。",
                parameters={
                    "type": "object",
                    "properties": {"product_code": {"type": "string"}},
                    "required": ["product_code"],
                },
                handler=_get_detail,
                tags=("product", "read"),
            )
        )
        registered.append("product.get_detail")

        # ---- 工具 3：图谱多跳推荐 ----
        def _graph_recommend(qualifications: list, purposes: list) -> dict[str, Any]:
            ranked = self.graph.recommend_by_tags(list(qualifications), list(purposes), top_k=5)
            return {
                "recommendations": [
                    {
                        "product_code": code,
                        "product_name": (self.repository.get_product(code).name
                                         if self.repository.get_product(code) else code),
                        "graph_score": score,
                    }
                    for code, score in ranked
                ]
            }

        registry.register(
            ToolSpec(
                name="product.graph_recommend",
                description=(
                    "基于产品关系图谱做多跳推荐：输入客户资质与资金用途，返回匹配产品及图谱得分。"
                    "适用于'征信查询多 + 有按揭房 + 想做置换'这类多条件组合场景。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "qualifications": {"type": "array", "description": "客户资质，如 公积金/房产/营业执照"},
                        "purposes": {"type": "array", "description": "资金用途，如 债务置换/资金周转"},
                    },
                    "required": ["qualifications"],
                },
                handler=_graph_recommend,
                tags=("product", "graph", "read"),
            )
        )
        registered.append("product.graph_recommend")

        # ---- 工具 4：读取客户档案 ----
        def _get_profile(customer_id: str) -> dict[str, Any]:
            profile = self.profiles.get(customer_id)
            if profile is None:
                return {"found": False, "customer_id": customer_id}
            return {
                "found": True,
                "profile": profile.model_dump(mode="json"),
                "debt_ratio": profile.debt_ratio,
            }

        registry.register(
            ToolSpec(
                name="customer.get_profile",
                description="读取客户档案（资质、征信、负债、需求），来自智能客服槽位填充与数据中台。",
                parameters={
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                    "required": ["customer_id"],
                },
                handler=_get_profile,
                tags=("customer", "read"),
            )
        )
        registered.append("customer.get_profile")

        # ---- 工具 5：结构化匹配 ----
        def _match(customer_id: str, as_of: str = "") -> dict[str, Any]:
            parsed = date.fromisoformat(as_of) if as_of else None
            result = self.match_customer(customer_id, as_of=parsed)
            return result.model_dump(mode="json")

        registry.register(
            ToolSpec(
                name="product.match",
                description=(
                    "按'资质 × 征信 × 需求'执行结构化方案匹配，返回主力产品 + 备选产品 + "
                    "淘汰原因 + 风险提示。无匹配时 matched=false，此时应转人工。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                        "as_of": {"type": "string", "description": "政策基准日期 YYYY-MM-DD（可选）"},
                    },
                    "required": ["customer_id"],
                },
                handler=_match,
                tags=("matching", "customer", "read"),
            )
        )
        registered.append("product.match")

        # ---- 工具 6：方案书生成（中风险：会产出对外材料，需人工把关后发送）----
        def _proposal(customer_id: str, as_of: str = "") -> dict[str, Any]:
            parsed = date.fromisoformat(as_of) if as_of else None
            try:
                proposal = self.generate_proposal(customer_id, as_of=parsed)
            except PolicyNotFoundError as exc:
                return {"generated": False, "reason": str(exc)}
            return {
                "generated": True,
                "proposal_id": proposal.proposal_id,
                "markdown": proposal.to_markdown(),
                "amount_range": proposal.amount_range,
                "rate_range": proposal.rate_range,
            }

        registry.register(
            ToolSpec(
                name="product.generate_proposal",
                description=(
                    "为客户生成结构化方案书（产品组合、预估额度/利率、资料清单、办理流程、"
                    "风险提示）。所有预估均标注'以银行审批为准'。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                        "as_of": {"type": "string", "description": "政策基准日期 YYYY-MM-DD（可选）"},
                    },
                    "required": ["customer_id"],
                },
                handler=_proposal,
                tags=("proposal", "write"),
                risk_level="medium",
                idempotent=True,
            )
        )
        registered.append("product.generate_proposal")

        logger.info("产品助手注册工具 %d 个：%s", len(registered), registered)
        return registered

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        return {
            "products": len(self.repository.products),
            "policy_documents": len(self.repository.documents),
            "profiles": len(self.profiles.all()),
            "indexed": self._indexed,
            "graph": self.graph.stats(),
            "retriever": self.retriever.stats() if self._indexed else {},
        }
