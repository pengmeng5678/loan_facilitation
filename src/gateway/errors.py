"""网关异常。

刻意**不**往 agent_platform.errors 里加 —— 网关是独立基础设施包，
平台不应该知道网关存在。这样网关可以被任意项目复用，也可以随时摘掉。
"""

from __future__ import annotations


class GatewayError(Exception):
    """网关异常基类。"""


class GatewayRateLimitedError(GatewayError):
    """触发限流。属于"稍后重试即可成功"，调用方可退避重试。"""

    def __init__(self, dimension: str) -> None:
        super().__init__(f"触发限流：{dimension}。请稍后重试，或降低并发。")
        self.dimension = dimension


class GatewayQuotaExceededError(GatewayError):
    """超出配额。属于"业务侧需要干预"（提额或降低消耗），不是技术故障。"""

    def __init__(self, scope: str, remaining: int) -> None:
        super().__init__(f"作用域 {scope} 的 token 配额已耗尽（剩余 {remaining}）。")
        self.scope = scope
        self.remaining = remaining


class AllProvidersFailedError(GatewayError):
    """整条降级链都失败了。这是真正需要告警的情况。"""

    def __init__(self, chain: list[str], errors: list[str]) -> None:
        super().__init__(f"降级链全部失败：{chain}。错误：{errors}")
        self.chain = chain
        self.errors = errors
