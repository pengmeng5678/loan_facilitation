"""RAG 拒答阈值校准脚本 —— 换真实 Embedding 后必须重跑的"体检"。

用法：
    python scripts/seed_data.py            # 先灌演示数据
    python scripts/calibrate_rag.py        # 输出正负例分离度与建议阈值

它解决的问题是：`RAG_MIN_RERANK_SCORE` / `RAG_MIN_DENSE_SCORE` 的默认值
是针对离线 LocalHashEmbedding + LocalReranker 校准的；一旦换成
text-embedding-v3 这类真实模型，分数分布整体漂移，旧阈值会**误拒**（把该答的拒了）
或**误放**（把该拒的编了）——两者在助贷场景都是事故。

脚本做的事：
    1. 用与生产装配一致的方式构建产品助手（PolicyRepository + HybridRetriever）；
    2. 跑一组"有政策依据 / 无政策依据"的标注问题；
    3. 输出每个问题的 (top rerank, top dense) 与两类分数的分离带、建议阈值；
    4. 给出 .env 修改建议（阈值取分离带中点，宁可略保守）。

扩展：把你的真实 badcase 按同样格式加进 LABELS，这就是一个可持续
维护的校准集 —— 和 Golden Set（测答案质量）互补，测的是"该不该答"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_platform.config import get_settings  # noqa: E402
from agent_platform.logging_config import configure_logging  # noqa: E402
from product_assistant.service import ProductAssistantService  # noqa: E402

# (问题, 是否有政策依据)。正面覆盖利率/准入/资料/流程四类高频问法，
# 负面覆盖闲聊、超纲、常识三类典型误触发场景。
LABELS: list[tuple[str, bool]] = [
    # ---- 正例：库里确实有政策依据 ----
    ("公积金信用贷的利率区间是多少", True),
    ("债务置换贷额度上限多少", True),
    ("申请公积金信用贷需要什么资料", True),
    ("你们的准入条件是什么", True),
    ("办理流程是怎样的", True),
    ("利率最低能到多少", True),
    # ---- 负例：任何政策库都不该接 ----
    ("今天天气怎么样", False),
    ("你会写代码吗", False),
    ("帮我推荐一部电影", False),
    ("隔壁银行怎么样", False),
    ("特朗普是谁", False),
]


def main() -> int:
    configure_logging("WARNING")
    settings = get_settings()
    svc = ProductAssistantService(settings=settings)
    stats = svc.bootstrap()
    if not stats.get("policy_documents"):
        print("未发现政策语料（.data/policies 为空）—— 先运行 `python scripts/seed_data.py`。")
        return 1

    report = svc.retriever.calibrate(LABELS)

    print("=" * 72)
    print(
        "拒答闸门校准报告"
        f"（当前阈值：rerank < {settings.rag_min_rerank_score:.2f} "
        f"且 dense < {settings.rag_min_dense_score:.2f} → 拒答）"
    )
    print("=" * 72)
    for row in report["detail"]:
        mark = "相关" if row["relevant"] else "无关"
        print(f"  [{mark}] rerank={row['top_rerank']:<7} dense={row['top_dense']:<7} "
              f"检索 {row['retrieved']} 条 | {row['query']}")

    for name in ("rerank", "dense"):
        r = report[name]
        print("-" * 72)
        if r.get("separable"):
            print(f"{name}: 可分。无关最大 {r['irrelevant_max']} / 相关最小 {r['relevant_min']}"
                  f" → 建议阈值 {r['suggested_threshold']}（宁拒勿编可取中点偏下）")
            print(f"  对应 .env 配置：RAG_MIN_{name.upper()}_SCORE={r['suggested_threshold']}")
        else:
            print(f"{name}: 不可分（无关最大 {r.get('irrelevant_max')} ≥ 相关最小 {r.get('relevant_min')}）。"
                  "说明该信号在当前 embedding 下没有区分度，不要硬调阈值，换模型后重测。")
    print("=" * 72)
    print(json.dumps({"chunks": stats.get("chunks"), "products": stats.get("products")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
