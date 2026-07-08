"""批次纪律 + 冷静期 + 次数预算（任务 18.5，需求 R15.4/R15.5/R15.6）。

- 建仓分 2-3 批首批 ≤ 目标一半；超顶 30 交易日分批降回每批 ≥ 超额 1/3；
  分批兑现每次 1/3 间隔 ≥5 交易日（交易日算术走 CalendarService，禁裸 timedelta）；
- 同一标的滚动 24h 加仓累计 >5pp → 24h 冷静期；情绪自评 ≥4 → 48h（墙钟 Duration）；
- 月主动交易 6 次、年换股 6 次两个独立计数器；框架强制动作 origin=framework 不计入。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from ..calendar import CalendarService, Duration
from ..rules.engine import RuleRepository
from ..rules.params import ParamStore


@dataclass(frozen=True)
class DisciplineCheck:
    ok: bool
    rule_id: str
    detail: str = ""


class DisciplineEngine:
    def __init__(self, rules: RuleRepository, params: ParamStore,
                 calendar: CalendarService | None = None):
        self._rules = rules
        self._params = params
        self._cal = calendar or CalendarService()

    # —— 建仓批次（R15.4）——
    def check_entry_batches(self, target_pct: float, batches: list[float]) -> DisciplineCheck:
        rule = self._rules.get("RISK.BATCH.ENTRY")
        p = rule.params
        n = len(batches)
        if not (int(p["min_batches"]) <= n <= int(p["max_batches"])):
            return DisciplineCheck(False, rule.rule_id,
                                   f"批数 {n} 不在 [{p['min_batches']},{p['max_batches']}]")
        if batches and batches[0] > target_pct * float(p["first_batch_max_frac"]) + 1e-9:
            return DisciplineCheck(False, rule.rule_id,
                                   f"首批 {batches[0]}% > 目标 {target_pct}% 的一半")
        return DisciplineCheck(True, rule.rule_id)

    # —— 超顶降回（R15.4）——
    def check_cap_reduce_batch(self, excess_pct: float, batch_pct: float) -> DisciplineCheck:
        rule = self._rules.get("RISK.BATCH.CAP_REDUCE")
        floor = excess_pct / int(rule.params["batch_min_divisor"])
        if batch_pct + 1e-9 < floor:
            return DisciplineCheck(False, rule.rule_id, f"降回批 {batch_pct}% < 超额 1/3 = {floor:.2f}%")
        return DisciplineCheck(True, rule.rule_id)

    def cap_reduce_deadline(self, market: str, start: _dt.date) -> _dt.date:
        rule = self._rules.get("RISK.BATCH.CAP_REDUCE")
        return self._cal.add(market, start, Duration.parse(f"{int(rule.params['deadline_trading_days'])}td"))

    # —— 分批兑现（R15.4）——
    def check_take_profit(self, holding_pct: float, tranche_pct: float,
                          last_date: _dt.date | None, this_date: _dt.date, market: str) -> DisciplineCheck:
        rule = self._rules.get("RISK.BATCH.TAKE_PROFIT")
        p = rule.params
        expected = holding_pct / int(p["tranche_divisor"])
        if tranche_pct > expected * (1 + float(p["fraction_tolerance"])):
            return DisciplineCheck(False, rule.rule_id, f"兑现 {tranche_pct}% > 1/3 = {expected:.2f}%")
        if last_date is not None:
            earliest = self._cal.add(market, last_date,
                                     Duration.parse(f"{int(p['min_interval_trading_days'])}td"))
            if this_date < earliest:
                return DisciplineCheck(False, rule.rule_id,
                                       f"距上次兑现不足 {p['min_interval_trading_days']} 交易日")
        return DisciplineCheck(True, rule.rule_id)

    # —— 冷静期（R15.5）——
    def cooloff_required(self, adds_24h: list[float], emotion_score: int) -> Duration | None:
        """同一标的滚动 24h 加仓累计 >5pp → 冷静期；情绪 ≥4 → 48h。返回 None=无需冷静。"""
        rule = self._rules.get("RISK.COOLOFF")
        p = rule.params
        cum = sum(adds_24h)
        if cum <= float(p["cum_add_max_pp"]) and emotion_score < int(p["emotion_threshold"]):
            return None
        if emotion_score >= int(p["emotion_threshold"]):
            return Duration.parse(str(p["cooloff_extended"]))
        return Duration.parse(str(p["cooloff"]))

    def cooloff_until(self, start: _dt.datetime, duration: Duration) -> _dt.datetime:
        return self._cal.add_wall_clock(start, duration)

    # —— 次数预算（R15.6）——
    def counts_ok(self, monthly_active: int, annual_swaps: int, *, on: _dt.date,
                  kind: str) -> DisciplineCheck:
        """kind ∈ trade | swap。框架强制动作不应调用此校验（由调用方按 origin 过滤）。"""
        rule = self._rules.get("RISK.BUDGET.COUNTERS")
        p = rule.params
        if kind == "swap":
            limit = int(self._params.resolve(str(p["annual_swaps_param"]), on))
            if annual_swaps >= limit:
                return DisciplineCheck(False, rule.rule_id, f"年换股已达 {annual_swaps}/{limit}")
        limit_m = int(self._params.resolve(str(p["monthly_trades_param"]), on))
        if monthly_active >= limit_m:
            return DisciplineCheck(False, rule.rule_id, f"月主动交易已达 {monthly_active}/{limit_m}")
        return DisciplineCheck(True, rule.rule_id)
