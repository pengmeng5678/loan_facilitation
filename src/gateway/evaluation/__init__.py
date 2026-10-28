"""评测闭环：Golden Set + Judge + 轨迹评测 + CI 准入。

这解决的是简历里的第三个痛点：**模型升级 / Prompt 变更后，效果好坏靠感觉**。

四件套：
    ① Golden Set      —— 可判定的用例集（本模块提供内置代表性集合，也支持外部 jsonl）
    ② Judge           —— 确定性 rubric 判分（关键词/禁止词/拒答正确性），可选叠加 LLM-as-Judge
    ③ 轨迹评测        —— 把"走了哪条路由、花了多少 token、是否降级"一并记录，
                          因为 Agent 的失败往往在中间步而不是最终答案
    ④ CI 准入         —— 与基线比对，退化超阈值就**拦住发布**
"""

from gateway.evaluation.dataset import (
    builtin_golden_cases,
    cases_for_scene,
    load_golden_set,
    scenes,
)
from gateway.evaluation.judge import Judge
from gateway.evaluation.runner import EvalRunner

__all__ = [
    "EvalRunner",
    "Judge",
    "builtin_golden_cases",
    "cases_for_scene",
    "load_golden_set",
    "scenes",
]
