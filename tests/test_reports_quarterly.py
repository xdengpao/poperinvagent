"""季度校准报告（P3 任务 23，需求 R14.5/R14.6/R14.8）。"""

import datetime as dt

from invest_assistant.analysis.regime import RegimeLevel
from invest_assistant.audit.violations import ViolationCategory, ViolationLedger
from invest_assistant.opinion.calibration import Calibrator, QualityLibrary, QualityRecord
from invest_assistant.opinion.factory import DISCLAIMER
from invest_assistant.reports import QuarterlyReportBuilder
from invest_assistant.rules import load_params, load_rules

TODAY = dt.date(2026, 7, 8)


def _fixture_report(*, losing=4, sample=15):
    rules, params = load_rules(), load_params()
    cal = Calibrator(rules, params)
    calib = cal.quarterly_calibration(consecutive_losing_quarters=losing, sample_size=sample, on=TODAY)
    bench = cal.portfolio_benchmark(consecutive_losing_quarters=3, on=TODAY)  # 未触发

    lib = QualityLibrary()
    lib.record(QualityRecord("#2026-04-001", True, 0.05))
    lib.record(QualityRecord("#2026-04-002", False, -0.03))

    ledger = ViolationLedger()
    ledger.register(ViolationCategory.COOLOFF_BREACH, "AMD", "拆单避冷静期", "2026-05-01")
    e = ledger.register(ViolationCategory.REDLINE_TOUCH, "BIG", "单票越红线", "2026-05-02")
    ledger.close(e.entry_id, "2026-05-03")

    return QuarterlyReportBuilder().build(
        "2026Q2", quality_lib=lib, calibration=calib,
        heavy_line_now=params.resolve("score.heavy_line", TODAY),
        cash_floor_now=params.resolve("cash.floor_normal_pct", TODAY),
        benchmark=bench, benchmark_losing_quarters=3,
        initial_cap_now=params.resolve("position.initial_max_pct", TODAY),
        ledger=ledger, regime_levels=[RegimeLevel.NORMAL, RegimeLevel.OVERHEAT],
        current_regime=RegimeLevel.OVERHEAT, gap_rate=0.02, total_gaps=3,
        sample_size=sample, consecutive_losing_quarters=losing,
        avg_excess_return=-0.015,
    )


def test_report_assembles_all_sections():
    r = _fixture_report()
    assert r.period == "2026Q2"
    assert r.quality.total_opinions == 2 and r.quality.buy_win_rate == 0.5
    assert r.calibration.tightened and r.calibration.heavy_line_now == 80
    assert r.calibration.cash_floor_now == 30  # 升一档
    assert not r.benchmark.override_active
    assert r.violations.open_count == 1  # 一条闭环、一条未闭环
    assert r.violations.counts_by_category["冷静期违规"] == 1
    assert r.violations.counts_by_category["红线触碰"] == 0  # 已闭环不计入待复盘汇总
    assert sum(r.violations.counts_by_category.values()) == r.violations.open_count
    assert r.regime.current_level == "overheat"


def test_report_markdown_has_disclaimer():
    md = _fixture_report().to_markdown()
    assert md.rstrip().endswith(DISCLAIMER)  # 涉标的输出恒附声明（R14.9）
    assert "季度校准报告 · 2026Q2" in md
    assert "判定线 75→80" in md


def test_small_sample_note_surfaces():
    r = _fixture_report(sample=8)
    assert "不豁免" in r.calibration.confidence_note
    assert "不豁免" in r.to_markdown()  # 样本<12 提示进报告但仍收紧


def test_report_does_not_recompute():
    """报告只汇集，数字来自库；空库不虚构。"""
    rules, params = load_rules(), load_params()
    cal = Calibrator(rules, params)
    calib = cal.quarterly_calibration(consecutive_losing_quarters=0, sample_size=0, on=TODAY)
    r = QuarterlyReportBuilder().build(
        "2026Q1", quality_lib=QualityLibrary(), calibration=calib,
        heavy_line_now=75, cash_floor_now=15,
        benchmark=cal.portfolio_benchmark(consecutive_losing_quarters=0, on=TODAY),
        benchmark_losing_quarters=0, initial_cap_now=10, ledger=ViolationLedger(),
        regime_levels=[], current_regime=RegimeLevel.NORMAL, gap_rate=None, total_gaps=0,
        sample_size=0, consecutive_losing_quarters=0)
    assert r.quality.buy_win_rate is None and r.gaps.gap_rate is None
    assert not r.calibration.tightened and r.violations.open_count == 0
    md = r.to_markdown()
    assert "买入意见方向正确率：—" in md and "缺口率：—" in md
