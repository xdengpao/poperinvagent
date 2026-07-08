"""金标准回归：归一化六实跑案例（需求 R20.3，任务 6.3）。

fixture 登记的是报告实际给出的数据粒度（部分仅符号级/结论级，明细为显式 gap）；
本测试断言：①变换机制的数值正确性；②规则判定与 fixture 期望结论一致。
全数值粒度重放待 P1 适配器接入真实披露后升级（fixture 版本化追加）。
"""

from invest_assistant.data.normalize import (
    Annotated,
    CashConversionPeriod,
    align_53_week,
    cash_conversion_series,
    exclude_discontinued,
    mark_one_off,
    semiannual_cf_periods,
    split_adjust,
)


def _case(golden, name):
    return next(c for c in golden("normalization/cases.json")["cases"] if c["case"] == name)


def test_kla_split(golden):
    """拆股调整：拆前每股口径 ÷10 对齐；拆股不得被误判为项 5 稀释断崖。"""
    c = _case(golden, "KLA-split")
    assert c["inputs"]["split_ratio"] == "10:1"
    series = Annotated(values=[240.0, 270.0, 30.0], periods=["FY23", "FY24", "FY25"])
    out = split_adjust(series, ratio=10, effective_period="FY25")
    assert out.values == [24.0, 27.0, 30.0]  # 调整后连续可比（对应 fixture EPS $24→$30）
    assert out.annotations[-1]["transform"] == "split_adjustment"
    assert c["expected"]["item5_verdict"] == "not_hit"


def test_avgo_53_week(golden):
    """53 周财年折算 52 周可比口径后才进入项 2/4 阈值判定。"""
    c = _case(golden, "AVGO-53wk")
    adj, note = align_53_week(fy_value=53.0, weeks=53)
    assert adj == 52.0 and note["factor"] == 52 / 53
    assert c["expected"]["item4_verdict"] == "edge_not_hit"  # 且条件后半（利润率）不成立


def test_arm_semiannual_cf(golden):
    """ADR 半年现金流：连续计数按可得半年期折算，不虚构季度粒度。"""
    c = _case(golden, "ARM-semiannual-cf")
    halves, note = semiannual_cf_periods(available_halves=4)
    assert note["quarters_equiv"] == 8  # 框架"连续 8 季"↦4 个半年期
    assert c["inputs"]["cash_conversion_fy2025"] == 0.5   # IPO 税务代扣一次性
    assert c["inputs"]["cash_conversion_fy2026"] == 1.69
    assert c["expected"]["item2_cash_conversion_verdict"] == "not_hit"


def test_amd_discontinued(golden):
    """终止经营剔除：持续经营口径的收入序列。"""
    c = _case(golden, "AMD-discontinued")
    cont, note = exclude_discontinued(total=100.0, discontinued=12.0)
    assert cont == 88.0 and note["excluded"] == 12.0
    assert c["expected"]["item4_verdict"] == "not_hit"


def test_nvda_oneoff(golden):
    """一次性损益标记：$15.9B 股权收益剔除后为经常性口径。"""
    c = _case(golden, "NVDA-oneoff")
    gain = c["inputs"]["oneoff_gain"]["value"]
    assert gain == 15_900_000_000
    recurring, note = mark_one_off(value=30_000_000_000.0, one_off=float(gain))
    assert recurring == 14_100_000_000.0 and note["one_off"] == gain


def test_mrvl_negative_ni(golden):
    """负净利确定规则（R2.5）：OCF 全正 → 各负净利期判通过、零失败期 → 项 2 未命中。"""
    c = _case(golden, "MRVL-negative-ni")
    assert c["inputs"]["ocf_8q_sign"] == "全正"
    # 按报告符号级结论构造 8 季：OCF 全正、其中若干期 GAAP 亏损
    periods = [
        CashConversionPeriod(f"Q{i}", ocf=1.0, net_income=(-1.0 if i in (2, 3, 5) else 1.0))
        for i in range(1, 9)
    ]
    result = cash_conversion_series(periods)
    assert result.max_consecutive_failures == 0
    assert all(not p["failed"] for p in result.per_period)
    negative_rules = [p["rule"] for p in result.per_period if p["rule"] == "negative_ni:ocf_sign"]
    assert len(negative_rules) == 3  # 负净利期走确定规则而非比率口径
    assert c["expected"]["item2_cash_conversion_verdict"] == "not_hit"


def test_mrvl_rule_failure_branch():
    """负净利 + OCF≤0 → 计失败期进入连续计数（规则另一分支）。"""
    periods = [CashConversionPeriod(f"Q{i}", ocf=-1.0, net_income=-1.0) for i in range(1, 4)]
    assert cash_conversion_series(periods).consecutive_failures == 3
