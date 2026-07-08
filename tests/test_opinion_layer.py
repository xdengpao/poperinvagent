"""意见层全量：七类构造器 + M10 合规 + 校准 + 生命周期（任务 16/17，需求 R14）。"""

import datetime as dt

import pytest

from invest_assistant.opinion.calibration import Calibrator, QualityLibrary, QualityRecord
from invest_assistant.opinion.compliance import ComplianceChecker, Unpublishable, register_forbidden_terms
from invest_assistant.opinion.factory import (
    DISCLAIMER,
    FRAMEWORK_OPINION_TYPES,
    EvidenceItem,
    OpinionFactory,
    OpinionType,
    PositionPlan,
    ScenarioTargets,
    ScoreDetail,
    StopLossCheck,
)
from invest_assistant.opinion.lifecycle import (
    DisclaimerRemovalError,
    evaluate_expiry,
    render_output,
)
from invest_assistant.rules import load_params, load_rules

TODAY = dt.date(2026, 7, 8)


def _kwargs(target_high=8.0, total=80.0):
    return dict(
        symbol="AMD", market="US", basis_clauses=("3.4", "10.1"),
        evidence=(EvidenceItem(field_name="AI收入占比", value="45%", source="EDGAR", as_of=TODAY),),
        score=ScoreDetail(total=total, dimensions={"AI收入真实性": 18}),
        scenarios=ScenarioTargets(bear="-20%", base="+120%", bull="+300%"),
        position_plan=PositionPlan(target_low_pct=0, target_high_pct=target_high, batch_plan="分3批"),
        stop_loss=StopLossCheck(stop_level="-18%", risk_budget_check="首批×距离≤2%"),
        risk_alert="6.2/6.3 信号当前无命中",
    )


@pytest.fixture()
def factory():
    return OpinionFactory(now=lambda: dt.datetime(2026, 7, 8, 12, 0))


# ---------- 七类完备 ----------


def test_seven_framework_types_covered():
    covered = {t for types in FRAMEWORK_OPINION_TYPES.values() for t in types}
    assert covered == set(OpinionType)  # 枚举全部映射到框架七类
    assert len(FRAMEWORK_OPINION_TYPES) == 7


def test_all_constructors(factory):
    assert factory.buy(score_line=75, **_kwargs(total=80)).opinion_type is OpinionType.BUY
    assert factory.trial_buy(initial_cap_pct=10, **_kwargs(target_high=5)).opinion_type is OpinionType.TRIAL_BUY
    assert factory.add(**_kwargs()).opinion_type is OpinionType.ADD
    assert factory.reduce(**_kwargs()).opinion_type is OpinionType.REDUCE
    assert factory.sell(**_kwargs()).opinion_type is OpinionType.SELL
    assert factory.staged_take_profit(**_kwargs()).opinion_type is OpinionType.STAGED_TAKE_PROFIT
    assert factory.hold(**_kwargs()).opinion_type is OpinionType.HOLD
    assert factory.liquidate(**_kwargs(target_high=0)).opinion_type is OpinionType.LIQUIDATE


def test_buy_below_line_rejected(factory):
    with pytest.raises(ValueError):
        factory.buy(score_line=75, **_kwargs(total=70))


def test_trial_buy_position_capped(factory):
    with pytest.raises(ValueError):
        factory.trial_buy(initial_cap_pct=10, **_kwargs(target_high=6))  # >5


# ---------- M10 合规 ----------


def test_compliance_all_pass(factory):
    op = factory.buy(score_line=75, **_kwargs())
    report = ComplianceChecker().run(op, valid_evidence_ids={"x"})
    assert report.passed and len(report.results) == 10


def test_compliance_guarantee_language_blocked(factory):
    kw = _kwargs()
    kw["risk_alert"] = "该标的必涨，稳赚不赔"
    op = factory.buy(score_line=75, **kw)
    report = ComplianceChecker().run(op)
    fail = [r for r in report.failures if r.check_id == "no_guarantee_language"]
    assert fail and "必涨" in fail[0].detail


def test_compliance_number_over_signed_rejected(factory):
    params = load_params()
    op = factory.buy(**_kwargs(target_high=15))  # >签署初始上限 10
    report = ComplianceChecker(params).run(op, on=TODAY)
    assert any(r.check_id == "numbers_signed" and not r.passed for r in report.results)


def test_compliance_enforce_unpublishable(factory):
    kw = _kwargs()
    kw["risk_alert"] = "保证收益"
    op = factory.reduce(**kw)
    out = ComplianceChecker().enforce(op)
    assert isinstance(out, Unpublishable) and out.kind == "无法合规输出"


def test_register_forbidden_terms_extensible(factory):
    register_forbidden_terms("暴富密码")
    kw = _kwargs()
    kw["risk_alert"] = "暴富密码"
    op = factory.hold(**kw)
    assert not ComplianceChecker().run(op).passed


# ---------- 校准 ----------


def test_quarterly_calibration_tightens():
    rules, params = load_rules(), load_params()
    cal = Calibrator(rules, params)
    r = cal.quarterly_calibration(consecutive_losing_quarters=4, sample_size=15, on=TODAY)
    assert r.applied and params.resolve("score.heavy_line", TODAY) == 80
    assert params.resolve("cash.floor_normal_pct", TODAY) == 30  # 升一档


def test_calibration_small_sample_no_exemption():
    rules, params = load_rules(), load_params()
    cal = Calibrator(rules, params)
    r = cal.quarterly_calibration(consecutive_losing_quarters=4, sample_size=8, on=TODAY)
    assert r.applied and "不豁免" in r.confidence_note  # 样本<12 仍收紧


def test_portfolio_8q_benchmark_override():
    rules, params = load_rules(), load_params()
    cal = Calibrator(rules, params)
    r = cal.portfolio_benchmark(consecutive_losing_quarters=8, on=TODAY)
    assert r.applied and params.resolve("position.initial_max_pct", TODAY) == -10  # 10-20pp


def test_reco_list_truncation():
    rules, params = load_rules(), load_params()
    cal = Calibrator(rules, params)
    notice = cal.truncate_reco_list([f"S{i}" for i in range(20)], on=TODAY)
    assert notice.limit == 12 and len(notice.kept) == 12 and len(notice.dropped) == 8


def test_quality_library():
    lib = QualityLibrary()
    lib.record(QualityRecord("#2026-07-001", True, 0.05))
    lib.record(QualityRecord("#2026-07-002", False, -0.03))
    assert lib.buy_win_rate() == 0.5


# ---------- 生命周期 + 声明 ----------


def test_expiry(factory):
    op = factory.buy(score_line=75, **_kwargs())
    assert evaluate_expiry(op, op.valid_until) is not None
    assert evaluate_expiry(op, TODAY, condition_still_holds=False).reason == "condition_gone"
    assert evaluate_expiry(op, TODAY) is None


def test_render_output_appends_disclaimer():
    out = render_output("算力芯片行业扫描：入围 23 分")
    assert out.rstrip().endswith(DISCLAIMER)


def test_disclaimer_cannot_be_tampered():
    with pytest.raises(DisclaimerRemovalError):
        render_output(f"{DISCLAIMER}\n然后又加了别的内容在后面")  # 声明不在末尾=篡改
