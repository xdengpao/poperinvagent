"""风控联立校验器（任务 18.1，需求 R15.1）。

S5/S7 放行前调用 validate_all(组合状态, 提案) → **完整违规清单**（不遇错即停——
用户需要一次看全部违规）。阈值全部经 ParamStore/规则条目取值（代码零硬编码，
红线断言层除外）。带内漂移出带为告警（非拒绝）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from ..rules.engine import RuleRepository
from ..rules.params import ParamStore
from . import redline


@dataclass(frozen=True)
class Holding:
    symbol: str
    weight_pct: float
    subindustry: str = ""
    theme: str = ""
    is_ai: bool = False
    is_shovel: bool = False       # 卖铲子（True）/淘金者（False）
    small_cap: bool = False
    score: float | None = None    # 八维总分（防守底仓筛选用）


@dataclass(frozen=True)
class Proposal:
    """新开仓/加仓提案。"""

    symbol: str
    target_weight_pct: float
    first_batch_weight_pct: float
    stop_distance_pct: float       # 止损距离（首批×距离 ≤ 风险预算）
    subindustry: str = ""
    theme: str = ""
    is_ai: bool = False
    is_shovel: bool = False
    small_cap: bool = False


@dataclass(frozen=True)
class PortfolioState:
    holdings: tuple[Holding, ...] = ()
    cash_pct: float = 100.0
    leverage: float = 0.0
    regime: str = "normal"         # normal | overheat | mania | freeze
    family_asset_bucket_pct: float | None = None  # 组合净值 / 家庭可投资产
    target_weights: dict[str, float] = field(default_factory=dict)  # 带内漂移基准


@dataclass(frozen=True)
class Violation:
    rule_id: str
    kind: str
    detail: str
    severity: str = "block"        # block | alert（带内漂移为 alert）


class RiskEngine:
    def __init__(self, rules: RuleRepository, params: ParamStore):
        self._rules = rules
        self._params = params

    def _p(self, name: str, on: _dt.date) -> float:
        return self._params.resolve(name, on)

    def validate_all(self, state: PortfolioState, proposal: Proposal | None = None,
                     *, on: _dt.date | None = None) -> list[Violation]:
        """联立校验：全部限额逐条检查，返回完整违规清单（R15.1）。"""
        on = on or _dt.date.today()
        v: list[Violation] = []

        # 合并提案后的持仓视图
        after = {h.symbol: h.weight_pct for h in state.holdings}
        if proposal is not None:
            after[proposal.symbol] = after.get(proposal.symbol, 0.0) + proposal.target_weight_pct

        # —— 红线断言层（硬编码，先于规则库，R15.1）——
        try:
            redline.check_redlines(positions=after, leverage=state.leverage)
        except redline.RedlineViolation as e:
            v.append(Violation("RISK.REDLINE", "redline", str(e)))

        # —— 单票初始/硬顶（提案标的）——
        if proposal is not None:
            init_rule = self._rules.get("RISK.POSITION.INITIAL")
            hard_rule = self._rules.get("RISK.POSITION.HARDCAP")
            if proposal.small_cap:
                init_cap = self._p(str(init_rule.params["smallcap_cap_param"]), on)
                hard_cap = float(hard_rule.params["smallcap_cap_pct"])
            else:
                init_cap = self._p(str(init_rule.params["cap_param"]), on)
                hard_cap = self._p(str(hard_rule.params["cap_param"]), on)
            if proposal.target_weight_pct > init_cap:
                v.append(Violation("RISK.POSITION.INITIAL", "initial_cap",
                                   f"{proposal.symbol} 初始 {proposal.target_weight_pct}% > {init_cap}%"))
            if after[proposal.symbol] > hard_cap:
                v.append(Violation("RISK.POSITION.HARDCAP", "hard_cap",
                                   f"{proposal.symbol} 合计 {after[proposal.symbol]}% > 硬顶 {hard_cap}%"))
            # —— 风险预算：首批 × 止损距离 ≤ 净值 2% ——
            budget_rule = self._rules.get("RISK.BUDGET.FIRST_BATCH")
            budget = self._p(str(budget_rule.params["budget_param"]), on)
            risk_amt = proposal.first_batch_weight_pct * proposal.stop_distance_pct / 100
            if risk_amt > budget:
                v.append(Violation("RISK.BUDGET.FIRST_BATCH", "risk_budget",
                                   f"{proposal.symbol} 首批风险 {risk_amt:.2f}% > 预算 {budget}%"))

        # —— 子行业 ≤30% ——
        self._check_group_cap(v, after, state, "subindustry", "RISK.SUBINDUSTRY.CAP", proposal)
        # —— 主题穿透 ≤50% ——
        self._check_group_cap(v, after, state, "theme", "RISK.THEME.CAP", proposal)
        # —— AI 总敞口 ≤85% ——
        ai_rule = self._rules.get("RISK.AI.EXPOSURE.CAP")
        ai_sum = self._sum_flag(after, state, "is_ai", proposal)
        if ai_sum > float(ai_rule.params["max_pct"]):
            v.append(Violation(ai_rule.rule_id, "ai_exposure",
                               f"AI 总敞口 {ai_sum:.1f}% > {ai_rule.params['max_pct']}%"))

        # —— 现金分档下限 ——
        cash_rule = self._rules.get("RISK.CASH.FLOOR")
        if state.regime == "overheat":
            floor = self._p(str(cash_rule.params["overheat_param"]), on)
        elif state.regime == "mania":
            floor = float(cash_rule.params["mania_floor_pct"])
        else:
            floor = self._p(str(cash_rule.params["normal_param"]), on)
        cash_after = state.cash_pct - (proposal.target_weight_pct if proposal else 0.0)
        if cash_after < floor:
            v.append(Violation(cash_rule.rule_id, "cash_floor",
                               f"现金 {cash_after:.1f}% < {state.regime} 档下限 {floor}%"))

        # —— 持仓只数 ≤8 ——
        hold_rule = self._rules.get("RISK.HOLDINGS.MAX")
        n_after = len(after) if all(w > 0 for w in after.values()) else len([w for w in after.values() if w > 0])
        max_hold = int(self._p(str(hold_rule.params["max_param"]), on))
        if n_after > max_hold:
            v.append(Violation(hold_rule.rule_id, "holdings", f"持仓 {n_after} 只 > {max_hold}"))

        # —— 卖铲子占比 ≥40% ——
        shovel_rule = self._rules.get("RISK.SHOVEL.RATIO")
        equity = sum(after.values())
        if equity > 0:
            shovel = self._sum_flag(after, state, "is_shovel", proposal)
            ratio = shovel / equity * 100
            if ratio < float(shovel_rule.params["min_pct"]):
                v.append(Violation(shovel_rule.rule_id, "shovel_ratio",
                                   f"卖铲子占比 {ratio:.1f}% < {shovel_rule.params['min_pct']}%"))

        # —— 分桶 ≤ 家庭可投资产×签署占比 ——
        bucket_rule = self._rules.get("RISK.BUCKET.CAP")
        if state.family_asset_bucket_pct is not None:
            if state.family_asset_bucket_pct > float(bucket_rule.params["family_asset_max_pct"]):
                v.append(Violation(bucket_rule.rule_id, "bucket",
                                   f"分桶 {state.family_asset_bucket_pct}% > 签署占比 "
                                   f"{bucket_rule.params['family_asset_max_pct']}%"))

        # —— 带内漂移 ±3pp（告警，非拒绝，R15.1）——
        drift_rule = self._rules.get("RISK.DRIFT.BAND")
        band = float(drift_rule.params["band_pp"])
        for h in state.holdings:
            target = state.target_weights.get(h.symbol)
            if target is not None and abs(h.weight_pct - target) > band:
                v.append(Violation(drift_rule.rule_id, "drift",
                                   f"{h.symbol} 漂移 {h.weight_pct - target:+.1f}pp 超 ±{band}pp",
                                   severity="alert"))
        return v

    def _sum_flag(self, after: dict[str, float], state: PortfolioState, flag: str,
                  proposal: Proposal | None) -> float:
        flags = {h.symbol: getattr(h, flag) for h in state.holdings}
        if proposal is not None:
            flags[proposal.symbol] = getattr(proposal, flag)
        return sum(w for sym, w in after.items() if flags.get(sym))

    def _check_group_cap(self, v: list[Violation], after: dict[str, float], state: PortfolioState,
                         attr: str, rule_id: str, proposal: Proposal | None) -> None:
        rule = self._rules.get(rule_id)
        groups: dict[str, str] = {h.symbol: getattr(h, attr) for h in state.holdings}
        if proposal is not None:
            groups[proposal.symbol] = getattr(proposal, attr)
        totals: dict[str, float] = {}
        for sym, w in after.items():
            g = groups.get(sym) or ""
            if g:
                totals[g] = totals.get(g, 0.0) + w
        for g, total in totals.items():
            if total > float(rule.params["max_pct"]):
                v.append(Violation(rule_id, attr, f"{attr}={g} 合计 {total:.1f}% > {rule.params['max_pct']}%"))
