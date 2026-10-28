"""费用预估（项目八"费用评估"自动化）。

**业务口径先说清楚**（这是面试必问的点）：

    客户侧成本 = 放款资金的利息（等额本息试算）
    公司侧收入 = 服务费 = 放款金额 × 服务费率（分档）

**为什么用确定性代码而不是让模型算**：
费用是钱。模型算错一位小数就是事故，而且错了你查不出来
（同样的输入两次问模型可能两个结果）。等额本息是封闭公式，
代码算 3 微秒出结果、可单测、可复现 —— 业务判据用代码，
这是全局设计原则①。

**分档费率**：大额单（≥ 30 万）费率低于小额单，因为大额单
获客成本占比低。分档规则显式可配（flow_fee_rate_large_loan）。

**免责声明是字段的一部分**，不是可选的 UI 文案：
预估 ≠ 报价，任何对外发送的预估必须带口径说明。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Context, Decimal, localcontext

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from flow_ops.domain import FeeEstimate

logger = get_logger(__name__)

# 金融计算的精度上下文：28 位有效数字（Decimal 默认值），显式声明出来是为了
# 把"够用"论证成文：等额本息的中间量（(1+r)^n）最多十几个数量级，28 位
# 有效数字下的累积误差远小于"分"（10^-2 元），最后统一 quantize 一次定音。
# 用 localcontext 限定作用域而不是改全局 getcontext()：进程里别的模块也
# 可能用 Decimal，改全局精度是隐形副作用（线程安全也由局部上下文保证）。
_FEE_CONTEXT = Context(prec=28)

#: 金额的最小单位：分。所有对外的钱都以它为粒度
_CENTS = Decimal("0.01")


def _money(value: float) -> Decimal:
    """float → Decimal，经 str 中转取"十进制最短表示"。

    为什么不直接 Decimal(0.072)：那会得到二进制浮点的真实值
    （0.0720000000000000011102230246251565404236316680908203125），
    把浮点噪声带进金额计算。str() 给出的是人写下的那个十进制数。
    """
    return Decimal(str(value))


def _cents(value: Decimal) -> float:
    """quantize 到分（ROUND_HALF_UP）后转回 float。

    为什么是 ROUND_HALF_UP 而不是 Python 默认的 round()：
    float 的 round 是"银行家舍入"（round(0.5)=0、round(0.15)=0.1），
    金融口径是四舍五入；且 Decimal.quantize 是十进制精确取整，
    不经过二进制表示的二次误差。
    返回 float 是刻意的：FeeEstimate 字段与 API/JSON 契约都是 float，
    对外类型不变，只有内部计算升级为 Decimal。
    """
    return float(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


class FeeEstimator:
    """服务费与放款成本试算器。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------ #
    # 公开能力
    # ------------------------------------------------------------------ #
    def estimate(
        self,
        lead_id: str,
        *,
        loan_amount: float,
        months: int | None = None,
        annual_rate: float | None = None,
    ) -> FeeEstimate:
        """一次完整预估：利息试算 + 服务费分档 + 到手金额。"""
        if loan_amount <= 0:
            raise ValueError(f"放款金额必须为正：{loan_amount}")

        months = months or self._settings.flow_default_loan_months
        annual_rate = (
            annual_rate if annual_rate is not None else self._settings.flow_default_annual_rate
        )
        if months <= 0:
            raise ValueError(f"期数必须为正：{months}")

        monthly_payment, total_repayment = self._annuity(loan_amount, annual_rate, months)
        # 利息也走 Decimal 精确相减：float 的 132366.60 - 120000 会得到
        # ...99999 尾数，靠 round() 补救不如源头就不产生误差
        total_interest = _cents(_money(total_repayment) - _money(loan_amount))

        rate, tier = self._service_fee_rate(loan_amount)
        # 服务费与到手金额同口径 Decimal 取整（分档逻辑不变，只换算术底座）
        loan = _money(loan_amount)
        service_fee = _cents(loan * _money(rate))
        net_amount = _cents(loan - _money(service_fee))

        basis = (
            f"等额本息：年化 {annual_rate:.2%} / {months} 期；"
            f"服务费率 {rate:.2%}（{tier}）"
        )
        return FeeEstimate(
            lead_id=lead_id,
            loan_amount=round(loan_amount, 2),
            months=months,
            annual_rate=annual_rate,
            monthly_payment=monthly_payment,
            total_interest=round(total_interest, 2),
            total_repayment=round(total_repayment, 2),
            service_fee_rate=rate,
            service_fee=service_fee,
            net_amount=net_amount,
            basis=basis,
        )

    def service_fee_rate_for(self, loan_amount: float) -> tuple[float, str]:
        """分档费率（暴露给测试与 API，保证口径只有一处）。"""
        return self._service_fee_rate(loan_amount)

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _service_fee_rate(self, loan_amount: float) -> tuple[float, str]:
        threshold = self._settings.flow_fee_large_loan_threshold
        if loan_amount >= threshold:
            return self._settings.flow_fee_rate_large_loan, "大额档"
        return self._settings.flow_service_fee_rate, "常规档"

    @staticmethod
    def _annuity(principal: float, annual_rate: float, months: int) -> tuple[float, float]:
        """等额本息还款计算（Decimal，逐分取整）。

        月供 = P × r × (1+r)^n / ((1+r)^n - 1)，r = 年化 / 12
        零利率时退化为 P / n（避免除零，也让"银行贴息活动"能试算）。

        为什么总额 = 取整后的月供 × 期数，而不是"未取整月供 × 期数再取整"：
        客户每期实际还的就是已取整到分的月供，总额是它的累加 ——
        这让 total_repayment == monthly_payment × months 恒成立，
        对账不会出现"每期都对、合计差几分"的口径裂缝。
        """
        if principal <= 0:
            return 0.0, 0.0
        p = _money(principal)
        with localcontext(_FEE_CONTEXT):
            monthly_rate = _money(annual_rate) / Decimal(12)
            if monthly_rate <= 0:
                monthly = _cents(p / months)
            else:
                # 整数次幂与四则运算都在 28 位精度内完成，误差 << 1 分
                factor = (Decimal(1) + monthly_rate) ** months
                monthly = _cents(p * monthly_rate * factor / (factor - 1))
        total = _cents(_money(monthly) * months)
        return monthly, total
