"""口径归一化引擎（任务 6，需求 R2.4/R2.5，规格书 FR-D-04）。

可组合变换管道：拆股 / 53 周财年对齐 / ADR 半年现金流折算 / 终止经营剔除 /
一次性损益标记；每个变换记录（原值, 变换, 参数, 出处）到口径批注。
出口挂派生指标计算器（现金转化率序列等，任务 6.2）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Annotated:
    """带口径批注的数值序列。"""

    values: list[float]
    periods: list[str]
    annotations: list[dict] = field(default_factory=list)

    def annotate(self, transform: str, params: dict, source: str) -> None:
        self.annotations.append({"transform": transform, "params": params, "source": source})


# ---------- 变换（R2.4） ----------


def split_adjust(series: Annotated, ratio: float, effective_period: str) -> Annotated:
    """拆股调整（KLA 10:1 型）：拆股前期次的每股口径 ÷ ratio 对齐到拆后。"""
    if ratio <= 0:
        raise ValueError("拆股比例必须为正")
    idx = series.periods.index(effective_period)
    out = Annotated(
        values=[v / ratio if i < idx else v for i, v in enumerate(series.values)],
        periods=list(series.periods),
        annotations=list(series.annotations),
    )
    out.annotate("split_adjustment", {"ratio": ratio, "effective": effective_period}, "FR-D-04")
    return out


def align_53_week(fy_value: float, weeks: int) -> tuple[float, dict]:
    """53 周财年对齐（AVGO 型）：折算为 52 周口径做同比可比。"""
    if weeks not in (52, 53):
        raise ValueError("财年周数只能是 52/53")
    factor = 52 / weeks
    return fy_value * factor, {"transform": "fy53_align", "weeks": weeks, "factor": factor}


def semiannual_cf_periods(available_halves: int) -> tuple[int, dict]:
    """ADR 半年现金流（ARM/TSM 型）：连续期数要求按可得期数折算并标注（O-03）。

    框架"连续 8 季"在半年报口径下折算为连续 4 个半年期。
    """
    if available_halves < 0:
        raise ValueError("期数不可为负")
    return available_halves, {"transform": "adr_semiannual", "quarters_equiv": available_halves * 2}


def exclude_discontinued(total: float, discontinued: float) -> tuple[float, dict]:
    """终止经营剔除（AMD-ZT / SNPS-SIG 型）：持续经营口径。"""
    return total - discontinued, {"transform": "exclude_discontinued", "excluded": discontinued}


def mark_one_off(value: float, one_off: float) -> tuple[float, dict]:
    """一次性损益标记（NVDA 股权收益 / MPWR 税项型）：剔除后为经常性口径。"""
    return value - one_off, {"transform": "one_off_marked", "one_off": one_off}


# ---------- 派生指标（任务 6.2） ----------


@dataclass
class CashConversionPeriod:
    period: str
    ocf: float
    net_income: float


@dataclass
class CashConversionResult:
    per_period: list[dict]
    consecutive_failures: int
    max_consecutive_failures: int


def cash_conversion_series(periods: list[CashConversionPeriod]) -> CashConversionResult:
    """现金转化率序列（否决项 2 输入，R2.5 负净利确定规则）。

    - 净利 >0：转化率 = OCF/NI，是否失败由否决规则的阈值判定（此处仅产出数据）；
    - 净利 ≤0（MRVL 型）：OCF>0 → 该期通过；OCF≤0 → 计一次失败期进入连续计数。
    """
    out: list[dict] = []
    consec = 0
    max_consec = 0
    for p in periods:
        if p.net_income <= 0:
            failed = p.ocf <= 0
            ratio = None  # 负净利期不计算比率（口径无意义），只出通过/失败
            rule = "negative_ni:ocf_sign"
        else:
            ratio = p.ocf / p.net_income
            failed = p.ocf <= 0
            rule = "ratio"
        consec = consec + 1 if failed else 0
        max_consec = max(max_consec, consec)
        out.append({"period": p.period, "ratio": ratio, "failed": failed, "rule": rule})
    return CashConversionResult(out, consec, max_consec)


def receivables_vs_revenue(
    receivables_growth: list[float], revenue_growth: list[float], include_contract_assets: bool
) -> list[dict]:
    """应收 vs 收入 4 季差值（否决项 2 边缘输入；合同资产口径开关）。"""
    if len(receivables_growth) != len(revenue_growth):
        raise ValueError("期数不齐")
    return [
        {
            "diff_pp": (r - v) * 100,
            "contract_assets_included": include_contract_assets,
        }
        for r, v in zip(receivables_growth, revenue_growth, strict=True)
    ]


def diluted_share_trend(shares_by_year: list[float]) -> dict:
    """摊薄股数 3 年序列（否决项 5 输入）。"""
    if len(shares_by_year) < 2:
        raise ValueError("至少两年")
    total_growth = shares_by_year[-1] / shares_by_year[0] - 1
    return {"series": shares_by_year, "total_growth": total_growth}
