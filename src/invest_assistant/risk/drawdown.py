"""回撤阶梯（任务 18.4，需求 R15.3；框架 6.1，校验 C-12）。

回撤 = 自历史峰值日终净值（高水位）的百分比跌幅。三档触发：
-15% 冻结加仓+复盘；-20% 降权益指令；-30% 防守底仓（权益 ≤30% 且仅留
≥80 分标的）+停手一月。本模块只产出指令与冻结标志建议——防守类动作
不受冻结阻断由 orchestration/freeze.py 的 A5 断言层承担。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..rules.engine import RuleRepository


@dataclass(frozen=True)
class DrawdownDirective:
    tier: int                     # 0=无 1=-15% 2=-20% 3=-30%
    drawdown: float
    high_water: float
    freeze_add: bool = False      # 冻结加仓标志建议（-15% 起）
    review_task: bool = False     # 全面复盘任务（-15%）
    deleverage: bool = False      # 权益降回目标下限指令（-20%）
    defensive_floor: bool = False  # 防守底仓指令（-30%）
    defensive_equity_max_pct: float | None = None
    defensive_min_score: float | None = None
    halt_months: int = 0          # 停手期（-30%）
    rule_ids: list[str] = field(default_factory=list)
    detail: str = ""


class DrawdownLadder:
    def __init__(self, rules: RuleRepository):
        self._rules = rules

    @staticmethod
    def high_water(nav_series: list[float]) -> float:
        """高水位 = 历史日终净值最大值。"""
        if not nav_series:
            raise ValueError("净值序列为空")
        return max(nav_series)

    def evaluate(self, nav_series: list[float]) -> DrawdownDirective:
        rule = self._rules.get("RISK.DRAWDOWN.LADDER")
        p = rule.params
        hw = self.high_water(nav_series)
        current = nav_series[-1]
        dd = current / hw - 1 if hw > 0 else 0.0

        if dd <= float(p["defensive_at"]):
            return DrawdownDirective(
                tier=3, drawdown=dd, high_water=hw, freeze_add=True, deleverage=True,
                defensive_floor=True,
                defensive_equity_max_pct=float(p["defensive_equity_max_pct"]),
                defensive_min_score=float(p["defensive_min_score"]),
                halt_months=int(p["halt_months"]), rule_ids=[rule.rule_id],
                detail=f"回撤 {dd:.1%} ≤ -30%：防守底仓（权益 ≤{p['defensive_equity_max_pct']}%、"
                       f"仅留 ≥{p['defensive_min_score']} 分）+停手 {p['halt_months']} 月")
        if dd <= float(p["deleverage_at"]):
            return DrawdownDirective(tier=2, drawdown=dd, high_water=hw, freeze_add=True,
                                     deleverage=True, rule_ids=[rule.rule_id],
                                     detail=f"回撤 {dd:.1%} ≤ -20%：权益降回目标下限")
        if dd <= float(p["freeze_at"]):
            return DrawdownDirective(tier=1, drawdown=dd, high_water=hw, freeze_add=True,
                                     review_task=True, rule_ids=[rule.rule_id],
                                     detail=f"回撤 {dd:.1%} ≤ -15%：冻结加仓+全面复盘")
        return DrawdownDirective(tier=0, drawdown=dd, high_water=hw, rule_ids=[rule.rule_id],
                                 detail=f"回撤 {dd:.1%}：无触发")

    def defensive_survivors(self, holdings: list[tuple[str, float | None]], directive: DrawdownDirective) -> list[str]:
        """-30% 档防守底仓：仅保留评分 ≥ 阈值的标的（评分缺失=不保留，保守）。"""
        if not directive.defensive_floor or directive.defensive_min_score is None:
            return [s for s, _ in holdings]
        return [s for s, score in holdings if score is not None and score >= directive.defensive_min_score]
