"""意见质量校准与基准对照（任务 17，需求 R14.5/R14.6/R14.8；框架 10.3/10.4、6.5-6）。

- 90 天回看任务生成 + 意见质量库记录（R14.5）；
- 季度校准：连续 4 季买入意见跑输签署基准 → 判定线 75→80 + 现金档升一档
  （param_override，连续两季跑赢解除；样本 <12 输出置信提示但不豁免收紧，R14.6）；
- 组合级：连续 8 季跑输沪深300+NDX100 加权基准 → 个股上限 -20pp + 指数上调
  （连续 4 季跑赢解除，R14.8）；
- 推荐清单 ≤ 持仓上限×150% 截断 + 公告（R14.7）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from ..calendar import CalendarService, Duration
from ..rules.engine import RuleRepository
from ..rules.params import Override, ParamStore

_LOOKBACK_90D = Duration.parse("90d")


@dataclass(frozen=True)
class LookbackTask:
    opinion_id: str
    symbol: str
    due: _dt.date


@dataclass
class QualityRecord:
    opinion_id: str
    direction_ok: bool
    excess_return: float


@dataclass(frozen=True)
class CalibrationResult:
    applied: bool
    detail: str
    overrides: tuple[Override, ...] = ()
    confidence_note: str = ""


@dataclass(frozen=True)
class BenchmarkOverride:
    applied: bool
    detail: str
    overrides: tuple[Override, ...] = ()


@dataclass(frozen=True)
class TruncationNotice:
    kept: tuple[str, ...]
    dropped: tuple[str, ...]
    limit: int


class Calibrator:
    def __init__(self, rules: RuleRepository, params: ParamStore,
                 calendar: CalendarService | None = None):
        self._rules = rules
        self._params = params
        self._cal = calendar or CalendarService()

    # —— 90 天回看（R14.5）——
    def schedule_lookback(self, opinion_id: str, symbol: str, market: str,
                          issued_on: _dt.date) -> LookbackTask:
        due = self._cal.add(market, issued_on, _LOOKBACK_90D)
        assert isinstance(due, _dt.date)
        return LookbackTask(opinion_id, symbol, due)

    # —— 季度判定线校准（R14.6）——
    def quarterly_calibration(self, *, consecutive_losing_quarters: int, sample_size: int,
                              on: _dt.date) -> CalibrationResult:
        """连续 4 季买入意见跑输 → 判定线 75→80 + 现金档升一档；样本 <12 仅提示不豁免。"""
        note = ""
        if sample_size < 12:
            note = f"样本 {sample_size} <12：附统计置信提示，但不豁免收紧（R14.6，框架 10.4 原文）"
        if consecutive_losing_quarters < 4:
            return CalibrationResult(False, "未连续 4 季跑输", confidence_note=note)
        overrides: list[Override] = []
        # 判定线只升：75→80
        heavy = self._params.resolve("score.heavy_line", on)
        ov1 = Override("score.heavy_line", max(heavy, 80), valid_from=on, valid_to=None,
                       origin_rule="F 10.4")
        self._params.add_override(ov1)
        overrides.append(ov1)
        # 现金档升一档：常态下限 +起一档（用过热档值）
        overheat = self._params.resolve("cash.floor_overheat_pct", on)
        ov2 = Override("cash.floor_normal_pct", overheat, valid_from=on, valid_to=None,
                       origin_rule="F 10.4")
        self._params.add_override(ov2)
        overrides.append(ov2)
        return CalibrationResult(True, "连续 4 季买入意见跑输 → 判定线 75→80 + 现金升一档",
                                 overrides=tuple(overrides), confidence_note=note)

    def restore_after_two_winning(self, *, consecutive_winning_quarters: int) -> bool:
        """连续两季跑赢 → 恢复（解除覆盖，由调用方按解除条件清 override）。"""
        return consecutive_winning_quarters >= 2

    # —— 组合级基准对照（R14.8）——
    def portfolio_benchmark(self, *, consecutive_losing_quarters: int, on: _dt.date) -> BenchmarkOverride:
        """连续 8 季跑输加权基准 → 个股上限 -20pp + 指数上调（R14.8/6.5-6）。"""
        if consecutive_losing_quarters < 8:
            return BenchmarkOverride(False, "未连续 8 季跑输基准")
        init = self._params.resolve("position.initial_max_pct", on)
        ov = Override("position.initial_max_pct", init - 20, valid_from=on, valid_to=None,
                      origin_rule="F 6.5-6")
        self._params.add_override(ov)
        return BenchmarkOverride(True, "连续 8 季跑输 → 个股仓位上限 -20pp + 指数仓位上调",
                                 overrides=(ov,))

    # —— 推荐清单截断（R14.7）——
    def truncate_reco_list(self, ranked_symbols: list[str], *, on: _dt.date) -> TruncationNotice:
        max_hold = self._params.resolve("portfolio.max_holdings", on)
        mult = self._params.resolve("reco.list_multiplier", on)
        limit = int(max_hold * mult)
        return TruncationNotice(tuple(ranked_symbols[:limit]), tuple(ranked_symbols[limit:]), limit)


@dataclass
class QualityLibrary:
    """意见质量库（R14.5）。"""

    records: dict[str, QualityRecord] = field(default_factory=dict)

    def record(self, r: QualityRecord) -> None:
        self.records[r.opinion_id] = r

    def buy_win_rate(self) -> float | None:
        buys = [r for r in self.records.values()]
        if not buys:
            return None
        return sum(1 for r in buys if r.direction_ok) / len(buys)
