"""季度校准报告自动产出（P3 任务 23，需求 R14.5/R14.6/R14.8；框架 10.3/10.4、6.5-6）。

汇集本季度可观测事实为结构化报告：意见质量回看统计、判定线校准状态、
组合基准对照、违规台账七类汇总、缺口率、市况级别历史。报告是"涉标的输出"，
经 lifecycle.render_output 统一出口恒附非投资建议声明（R14.9）。

本模块只汇集与呈现既有判定产物，不产生任何新判定（A1）；全部数字来自
传入的库对象，报告不重算、不插值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..analysis.regime import RegimeLevel
from ..audit.violations import ViolationCategory, ViolationLedger
from ..opinion.calibration import (
    BenchmarkOverride,
    CalibrationResult,
    QualityLibrary,
)
from ..opinion.lifecycle import render_output


@dataclass(frozen=True)
class QualitySection:
    """意见质量回看（R14.5）。"""

    total_opinions: int
    buy_win_rate: float | None       # 90 天回看方向正确比例
    avg_excess_return: float | None  # 相对签署基准超额


@dataclass(frozen=True)
class CalibrationSection:
    """判定线校准状态（R14.6）。"""

    consecutive_losing_quarters: int
    sample_size: int
    tightened: bool                  # 本季是否触发 75→80 + 现金升档
    heavy_line_now: float
    cash_floor_now: float
    confidence_note: str = ""


@dataclass(frozen=True)
class BenchmarkSection:
    """组合级基准对照（R14.8，6.5-6）。"""

    consecutive_losing_quarters: int
    override_active: bool             # 连续 8 季跑输 → 个股上限 -20pp
    initial_cap_now: float


@dataclass(frozen=True)
class ViolationSection:
    """违规台账七类汇总（R18.3）。"""

    counts_by_category: dict[str, int]
    open_count: int


@dataclass(frozen=True)
class RegimeSection:
    """市况级别历史（R11）。"""

    levels_seen: tuple[str, ...]
    current_level: str


@dataclass(frozen=True)
class GapSection:
    """缺口率（R2.6）。"""

    gap_rate: float | None
    total_gaps: int


@dataclass(frozen=True)
class QuarterlyReport:
    """季度报告结构化对象。``period`` 如 "2026Q2"。"""

    period: str
    quality: QualitySection
    calibration: CalibrationSection
    benchmark: BenchmarkSection
    violations: ViolationSection
    regime: RegimeSection
    gaps: GapSection
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_markdown(self) -> str:
        """渲染为 markdown；经统一出口恒附非投资建议声明（R14.9）。"""
        q, c, b = self.quality, self.calibration, self.benchmark
        v, r, g = self.violations, self.regime, self.gaps
        win = "—" if q.buy_win_rate is None else f"{q.buy_win_rate:.1%}"
        excess = "—" if q.avg_excess_return is None else f"{q.avg_excess_return:+.2%}"
        gap_rate = "—" if g.gap_rate is None else f"{g.gap_rate:.1%}"
        cat_lines = "\n".join(f"  - {k}：{n}" for k, n in sorted(v.counts_by_category.items()) if n) or "  - 无"
        body = f"""# 季度校准报告 · {self.period}

> 本报告为框架规则对本季度判定产物的汇集，非新判定；全部数字来自系统留痕库。

## 一、意见质量回看（10.3/R14.5）
- 回看意见数：{q.total_opinions}
- 买入意见方向正确率：{win}
- 相对签署基准平均超额：{excess}

## 二、判定线校准（10.4/R14.6）
- 连续跑输季数：{c.consecutive_losing_quarters}（样本 {c.sample_size} 条）
- 本季收紧动作：{"已触发（判定线 75→80 + 现金升一档）" if c.tightened else "未触发"}
- 当前判定线：{c.heavy_line_now:g}；当前常态现金下限：{c.cash_floor_now:g}%
{("- 提示：" + c.confidence_note) if c.confidence_note else ""}

## 三、组合基准对照（6.5-6/R14.8）
- 连续跑输加权基准季数：{b.consecutive_losing_quarters}
- 个股上限 -20pp 覆盖：{"生效中" if b.override_active else "未生效"}
- 当前单票初始上限：{b.initial_cap_now:g}%

## 四、违规台账七类汇总（R18.3）
{cat_lines}
- 未闭环条目：{v.open_count}（已自动注入复盘议程）

## 五、市况与数据质量
- 本季市况级别历经：{"、".join(r.levels_seen) or "—"}；当前：{r.current_level}
- 缺口率：{gap_rate}（累计缺口 {g.total_gaps} 条）
"""
        if self.notes:
            body += "\n## 六、附注\n" + "\n".join(f"- {n}" for n in self.notes) + "\n"
        return render_output(body.rstrip() + "\n")


class QuarterlyReportBuilder:
    """从各留痕库装配季度报告（不重算，A1）。"""

    def build(
        self,
        period: str,
        *,
        quality_lib: QualityLibrary,
        calibration: CalibrationResult,
        heavy_line_now: float,
        cash_floor_now: float,
        benchmark: BenchmarkOverride,
        benchmark_losing_quarters: int,
        initial_cap_now: float,
        ledger: ViolationLedger,
        regime_levels: list[RegimeLevel],
        current_regime: RegimeLevel,
        gap_rate: float | None,
        total_gaps: int,
        sample_size: int,
        consecutive_losing_quarters: int,
        avg_excess_return: float | None = None,
        notes: tuple[str, ...] = (),
    ) -> QuarterlyReport:
        # 按未闭环条目分类计数（与 open_count 一致：闭环条目不计入待复盘汇总）
        counts: dict[str, int] = {c.value: 0 for c in ViolationCategory}
        for e in ledger.open_entries():
            counts[e.category.value] = counts.get(e.category.value, 0) + 1

        return QuarterlyReport(
            period=period,
            quality=QualitySection(
                total_opinions=len(quality_lib.records),
                buy_win_rate=quality_lib.buy_win_rate(),
                avg_excess_return=avg_excess_return,
            ),
            calibration=CalibrationSection(
                consecutive_losing_quarters=consecutive_losing_quarters,
                sample_size=sample_size,
                tightened=calibration.applied,
                heavy_line_now=heavy_line_now,
                cash_floor_now=cash_floor_now,
                confidence_note=calibration.confidence_note,
            ),
            benchmark=BenchmarkSection(
                consecutive_losing_quarters=benchmark_losing_quarters,
                override_active=benchmark.applied,
                initial_cap_now=initial_cap_now,
            ),
            violations=ViolationSection(
                counts_by_category=counts,
                open_count=len(ledger.open_entries()),
            ),
            regime=RegimeSection(
                levels_seen=tuple(dict.fromkeys(lv.value for lv in regime_levels)),
                current_level=current_regime.value,
            ),
            gaps=GapSection(gap_rate=gap_rate, total_gaps=total_gaps),
            notes=notes,
        )
