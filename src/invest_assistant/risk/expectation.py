"""预期调节校验器（任务 18.3，需求 R15.2；框架 1.3）。

滚动 12 个月组合收益 >+100% → 生成 6 个月有效期 param_override
（单票初始上限 -2pp、小盘同步 -2pp），经 ParamStore.add_override 落地；
到期自动回落由 ParamStore.resolve 的覆盖有效期语义承担（已有）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from ..calendar import CalendarService, Duration
from ..rules.engine import RuleRepository
from ..rules.params import Override, ParamStore


@dataclass(frozen=True)
class ExpectationOverride:
    applied: bool
    overrides: tuple[Override, ...] = ()
    trailing_return: float | None = None
    detail: str = ""


class ExpectationAdjuster:
    def __init__(self, rules: RuleRepository, params: ParamStore,
                 calendar: CalendarService | None = None):
        self._rules = rules
        self._params = params
        self._cal = calendar or CalendarService()

    def evaluate(self, nav_series: list[tuple[str, float]], on: _dt.date) -> ExpectationOverride:
        """nav_series: [(账务日 ISO, 日终净值)]，须含 ≥12 个月跨度。

        滚动 12 个月收益 = 最新净值 / 12 个月前净值 - 1；>+100% 触发覆盖。
        """
        rule = self._rules.get("RISK.EXPECT.ADJUST")
        p = rule.params
        if len(nav_series) < 2:
            return ExpectationOverride(applied=False, detail="净值序列不足")

        cutoff = self._cal.add("US", _shift_months_back(on, int(p["window_months"])),
                               Duration.parse("0d"))
        past = [nav for d, nav in nav_series if _dt.date.fromisoformat(d) <= cutoff]
        base = past[-1] if past else nav_series[0][1]
        latest = nav_series[-1][1]
        if base <= 0:
            return ExpectationOverride(applied=False, detail="基期净值非正")
        trailing = latest / base - 1

        if trailing <= float(p["trailing_return_min"]):
            return ExpectationOverride(applied=False, trailing_return=trailing,
                                       detail=f"滚动 12 月收益 {trailing:.1%} 未达 +100%")

        valid_to = _shift_months_forward(on, int(p["validity_months"]))
        overrides: list[Override] = []
        delta = float(p["cap_delta_pp"])
        for name in p["target_params"]:
            base_val = self._params.resolve(name, on)
            ov = Override(name=name, value=base_val - delta, valid_from=on,
                          valid_to=valid_to, origin_rule=rule.rule_id)
            self._params.add_override(ov)
            overrides.append(ov)
        return ExpectationOverride(applied=True, overrides=tuple(overrides),
                                   trailing_return=trailing,
                                   detail=f"滚动 12 月 {trailing:.1%} > +100% → 单票初始上限 -{delta}pp / 6 个月")


def _shift_months_back(d: _dt.date, months: int) -> _dt.date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return d.replace(year=y, month=m, day=min(d.day, 28))


def _shift_months_forward(d: _dt.date, months: int) -> _dt.date:
    y, m = d.year, d.month + months
    while m > 12:
        m -= 12
        y += 1
    return d.replace(year=y, month=m, day=min(d.day, 28))
