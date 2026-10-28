"""意图路由评测脚本（对应 P1-1 的验收口径）。

用法：
    .\\.venv\\Scripts\\python.exe scripts/eval_intent.py              # 规则基线
    .\\.venv\\Scripts\\python.exe scripts/eval_intent.py --embedding  # 规则 + 向量两级
    .\\.venv\\Scripts\\python.exe scripts/eval_intent.py --enforce    # CI：不达标退出码 1

输出：单意图 top1 准确率、多意图召回、fallback（澄清）率、混淆矩阵、分意图 P/R/F1。
门槛：macro-F1 >= 0.85 且 fallback_rate <= 0.05（可在命令行覆盖）。
为什么盯 macro-F1 而不是准确率：意图类别不均衡，"全猜 qa"在样本里准确率也不低，
macro-F1 会把这个把戏打回原形。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_platform.llm.providers import build_embedder  # noqa: E402
from agent_platform.routing import (  # noqa: E402
    EmbeddingRouter,
    RuleRouter,
    ThreeTierRouter,
    loan_intent_exemplars,
    loan_intent_keywords,
)
from agent_platform.routing.presets import LOAN_INTENT_PRIORITY  # noqa: E402

DATA = ROOT / "data" / "intent_eval.jsonl"


def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
    return cases


def build_router(mode: str):
    rule = RuleRouter(keywords=loan_intent_keywords(), priority=LOAN_INTENT_PRIORITY)
    if mode == "rule":
        return rule
    embedding = EmbeddingRouter(embedder=build_embedder(), exemplars=loan_intent_exemplars())
    return ThreeTierRouter(rule=rule, embedding=embedding)


def evaluate(cases: list[dict], router) -> dict:
    allowed = set(loan_intent_keywords())
    expected_count: Counter[str] = Counter()
    predicted_count: Counter[str] = Counter()
    true_positive: Counter[str] = Counter()
    confusion: Counter[tuple[str, str]] = Counter()
    fallback = 0
    top1_hits = 0
    multi_total = 0
    multi_recall_sum = 0.0
    failures: list[dict] = []

    for case in cases:
        expected = list(case["intents"])
        result = router.route(case["text"], allowed=allowed)
        predicted = result.names
        if result.need_clarify:
            fallback += 1

        if predicted and predicted[0] == expected[0]:
            top1_hits += 1
        confusion[(expected[0], predicted[0] if predicted else "(clarify)")] += 1

        if len(expected) > 1:
            multi_total += 1
            multi_recall_sum += len(set(expected) & set(predicted)) / len(set(expected))

        for intent in set(expected):
            expected_count[intent] += 1
        for intent in set(predicted):
            predicted_count[intent] += 1
        for intent in set(expected) & set(predicted):
            true_positive[intent] += 1

        # 失败判定：主意图错、多意图漏召回、或本可直接路由却请求澄清
        # （不要求预测列表与期望列表顺序完全相同：多意图路由允许带上更细的子意图）
        multi_missed = len(set(expected) - set(predicted))
        if (predicted[:1] != expected[:1]) or multi_missed or result.need_clarify:
            failures.append(
                {
                    "case_id": case.get("case_id"),
                    "text": case["text"],
                    "expected": expected,
                    "predicted": predicted,
                    "clarify": result.need_clarify,
                    "note": case.get("note", ""),
                }
            )

    per_intent: dict[str, dict[str, float]] = {}
    for intent in sorted(expected_count):
        tp = true_positive[intent]
        precision = tp / predicted_count[intent] if predicted_count[intent] else 0.0
        recall = tp / expected_count[intent] if expected_count[intent] else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_intent[intent] = {
            "support": expected_count[intent],
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }

    macro_f1 = sum(v["f1"] for v in per_intent.values()) / len(per_intent) if per_intent else 0.0
    return {
        "total": len(cases),
        "top1_accuracy": round(top1_hits / len(cases), 4) if cases else 0.0,
        "multi_intent_recall": round(multi_recall_sum / multi_total, 4) if multi_total else 0.0,
        "fallback_rate": round(fallback / len(cases), 4) if cases else 0.0,
        "macro_f1": round(macro_f1, 4),
        "per_intent": per_intent,
        "confusion": {f"{k[0]}→{k[1]}": v for k, v in sorted(confusion.items())},
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="意图路由评测")
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--router", choices=("rule", "three-tier"), default="rule")
    parser.add_argument("--embedding", action="store_true", help="等价于 --router three-tier")
    parser.add_argument("--out", type=Path, default=ROOT / ".data" / "eval" / "intent_report.json")
    parser.add_argument("--min-f1", type=float, default=0.85)
    parser.add_argument("--max-fallback", type=float, default=0.05)
    parser.add_argument("--enforce", action="store_true", help="不达标时退出码 1")
    args = parser.parse_args()

    mode = "three-tier" if args.embedding else args.router
    cases = load_cases(args.data)
    report = evaluate(cases, build_router(mode))
    report["router"] = mode

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"路由模式：{mode}    样本数：{report['total']}")
    print(f"top1 准确率：{report['top1_accuracy']:.2%}")
    print(f"多意图召回：{report['multi_intent_recall']:.2%}")
    print(f"澄清率（fallback）：{report['fallback_rate']:.2%}")
    print(f"macro-F1：{report['macro_f1']:.4f}")
    print("\n分意图：")
    for intent, metrics in report["per_intent"].items():
        print(
            f"  {intent:<11} support={metrics['support']:<3} "
            f"P={metrics['precision']:.2f} R={metrics['recall']:.2f} F1={metrics['f1']:.2f}"
        )
    if report["failures"]:
        print(f"\n未命中 {len(report['failures'])} 条（详见报告）：")
        for item in report["failures"][:10]:
            print(f"  {item['case_id']}: 期望 {item['expected']} → 实际 {item['predicted']}")
    print(f"\n报告已写入：{args.out}")

    passed = report["macro_f1"] >= args.min_f1 and report["fallback_rate"] <= args.max_fallback
    if args.enforce and not passed:
        print(
            f"\n未达门槛：macro-F1 {report['macro_f1']:.4f} < {args.min_f1}"
            f" 或 fallback {report['fallback_rate']:.2%} > {args.max_fallback:.0%}"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
