"""RAG 答案后校验（P1-5）：数值一致、引用只用实际用到的块。"""

from __future__ import annotations

from product_assistant.domain.models import Citation, RetrievedChunk
from product_assistant.rag.verify import verify_answer


def _chunk(chunk_id: str, text: str, *, policy_id: str = "P-1") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=text[:20],
        parent_text=text,
        citation=Citation(policy_id=policy_id, product_code="BANK-T-GJJ-001", clause_no="2.1"),
    )


def test_answer_with_wrong_rate_is_rejected():
    hits = [
        _chunk(
            "c1",
            "公积金信用贷的年化利率区间为 3.20% 至 4.60%，最终以银行审批结果为准。",
        )
    ]
    result = verify_answer("该产品年化利率为 2.80%，额度最高 40 万元。", hits)
    assert result.ok is False
    assert ("2.80%", "未找到") in result.mismatched_numbers


def test_citations_only_include_used_chunks():
    first = _chunk("c1", "公积金信用贷的年化利率区间为 3.20% 至 4.60%。")
    second = _chunk("c2", "办理需提供身份证、公积金缴存明细与征信报告。", policy_id="P-2")
    answer = "根据政策：公积金信用贷的年化利率区间为 3.20% 至 4.60%。"
    result = verify_answer(answer, [first, second])
    assert result.ok is True
    assert [h.chunk_id for h in result.used_hits] == ["c1"]


def test_unknown_product_code_is_flagged():
    hits = [_chunk("c1", "公积金信用贷的年化利率区间为 3.20% 至 4.60%。")]
    result = verify_answer("推荐 BANK-X-UNKNOWN-999，利率 3.20%。", hits)
    assert result.ok is False
    assert "BANK-X-UNKNOWN-999" in result.unsupported_claims


def test_service_refuses_when_answer_disagrees_with_policy(product_service, monkeypatch):
    monkeypatch.setattr(
        product_service,
        "_synthesize_answer",
        lambda question, hits: "该产品年化利率为 2.80%，一定可以批下来。",
    )
    answer = product_service.answer_question("公积金信用贷利率多少")

    assert answer.refused is True
    assert answer.verified is False
    assert answer.verify_issues
    assert answer.citations == []
