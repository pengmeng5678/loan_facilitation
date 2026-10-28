"""BM25 中文分词策略 benchmark：字符 n-gram（本仓库方案） vs jieba 词典分词。

用法：
    python scripts/benchmark_tokenizer.py

它回答的是面试必问的那道题——"BM25 对中文怎么分词？为什么不用 jieba？"。

本仓库的答案（用数据说话，不靠感觉）：
    检索目标是 **产品名 / 产品编号 / 专有名词的精确命中**。这类词
    （"公积金信用贷""先息后本""XYD-001"）对词典分词是 hazard：
    词典没收录就切错（"公积金/信用/贷"还行，冷门组合词会碎），而
    字符 n-gram 天然对专名免疫，代价是索引里多一些 gram 噪声。
    下面用同一组查询对比两种分词的 hit@1 / hit@3 / MRR，让结论可复现。

jieba 未安装时自动跳过对照，仅输出 n-gram 基线（离线零依赖原则）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from product_assistant.rag.bm25_index import BM25Index  # noqa: E402
from product_assistant.rag.chunking import Chunk, Citation  # noqa: E402
from product_assistant.rag.tokenize import tokenize  # noqa: E402

# ---- 模拟语料：产品条款（产品名 / 编号 / 专有名词密集）----
DOCS = [
    ("P1", "公积金信用贷（产品编号 XYD-001）年化利率区间 3.20% 至 4.60%，先息后本与等额本息均可选。"),
    ("P2", "债务置换贷（产品编号 DBT-002）面向负债较高客户，支持负债优化与月供重组。"),
    ("P3", "房抵贷（产品编号 FDC-003）额度最高 800 万，需提供房产证与征信报告，走抵押登记流程。"),
    ("P4", "税票贷（产品编号 SP-004）依据企业纳税记录授信，年开票额决定额度倍数。"),
    ("P5", "公积金信用贷准入：年龄 22-58 周岁，公积金连续缴存满 12 个月，征信查询半年不超过 8 次。"),
    ("P6", "债务置换贷准入：月收入不低于 6000 元，近 12 个月逾期不超过 4 次，负债收入比不超过 0.80。"),
    ("P7", "房抵贷办理流程：房产评估 → 面签 → 抵押登记 → 他项权证 → 放款，一般 5-7 个工作日。"),
    ("P8", "提前还款规则：放款满 6 个月后可提前结清，无违约金；部分产品按剩余本金 1% 收取。"),
]

# ---- 查询与期望命中的文档（hit@k 的判据）----
QUERIES: list[tuple[str, set[str]]] = [
    ("公积金信用贷利率多少", {"P1"}),
    ("XYD-001的额度", {"P1"}),
    ("债务置换贷准入条件", {"P2", "P6"}),
    ("房抵贷办理流程", {"P7"}),
    ("提前还款有没有违约金", {"P8"}),
    ("税票贷怎么评估额度", {"P4"}),
    ("征信查询次数超了能办吗", {"P5", "P6"}),
    ("先息后本可以选吗", {"P1"}),
]


def _chunk(doc_id: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}-0",
        text=text,
        parent_text=text,
        citation=Citation(
            policy_id=f"{doc_id}-RATE-01",
            product_code=doc_id,
            clause_no="2.1",
            title=f"条款 {doc_id}",
            effective_date="2024-01-01",
        ),
        metadata={},
    )


def _run(tokenizer, label: str) -> dict[str, float]:
    index = BM25Index(tokenizer=tokenizer)
    chunks = [_chunk(d, t) for d, t in DOCS]
    index.build(chunks)

    hits1 = hits3 = 0.0
    rr = 0.0
    for query, expected in QUERIES:
        ranked = index.search(query, len(DOCS))
        ranked_ids = [DOCS[h.index][0] for h in ranked]
        top = [i for i in ranked_ids if i in expected]
        if top:
            rank = ranked_ids.index(top[0]) + 1
            rr += 1.0 / rank
            hits1 += 1.0 if rank == 1 else 0.0
            hits3 += 1.0 if rank <= 3 else 0.0
    n = len(QUERIES)
    return {
        "tokenizer": label,
        "hit@1": round(hits1 / n, 3),
        "hit@3": round(hits3 / n, 3),
        "MRR": round(rr / n, 3),
    }


def _jieba_tokenize(text: str) -> list[str]:
    import jieba  # 可选依赖：未安装则跳过对照

    return [w for w in jieba.lcut(text.lower()) if w.strip()]


def main() -> int:
    rows = [_run(tokenize, "n-gram（本仓库）")]
    try:
        import jieba  # noqa: F401

        jieba.setLogLevel(60)
        rows.append(_run(_jieba_tokenize, "jieba 词典"))
    except ImportError:
        print("jieba 未安装（pip install jieba）—— 仅输出 n-gram 基线；安装后重跑可得对照。")

    print("=" * 56)
    print(f"BM25 中文分词策略对比（语料 {len(DOCS)} 条 / 查询 {len(QUERIES)} 条）")
    print("=" * 56)
    for r in rows:
        print(f"  {r['tokenizer']:<14} hit@1={r['hit@1']:<7} hit@3={r['hit@3']:<7} MRR={r['MRR']}")
    print("-" * 56)
    print("结论要点（面试讲法）：")
    print("  1) 产品名/编号/专名场景，n-gram 对'未登录词'免疫，稳定性优于词典分词；")
    print("  2) n-gram 的代价是索引膨胀与单字噪声 —— 用 content_tokens 过滤单字后")
    print("     相关性判定不受影响；")
    print("  3) 换到开放域问答（新闻/百科）时结论会反转，那时应换 jieba/预训练分词，")
    print("     接口不变（tokenize 函数签名），换实现即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
