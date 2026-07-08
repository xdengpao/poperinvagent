"""风控联立校验 + 红线断言 + 预期调节 + 回撤阶梯（任务 18，需求 R15）。"""

import datetime as dt

import pytest

from invest_assistant.risk import redline
from invest_assistant.risk.drawdown import DrawdownLadder
from invest_assistant.risk.engine import Holding, PortfolioState, Proposal, RiskEngine
from invest_assistant.risk.expectation import ExpectationAdjuster
from invest_assistant.rules import load_params, load_rules

TODAY = dt.date(2026, 7, 8)


@pytest.fixture()
def engine():
    return RiskEngine(load_rules(), load_params())


# ---------- 联立校验：完整违规清单 ----------


def test_clean_proposal_passes(engine):
    state = PortfolioState(holdings=(Holding("AMD", 8, "算力芯片", "AI算力", True, True),),
                           cash_pct=40, regime="normal")
    p = Proposal("AVGO", 8, 4, 18, "算力芯片", "AI算力", True, True)
    assert engine.validate_all(state, p, on=TODAY) == []


def test_multiple_violations_reported_together(engine):
    """违规逐条报告，不遇错即停（R15.1）。"""
    state = PortfolioState(
        holdings=(Holding("A", 20, "半导体", "AI算力", True, False),),
        cash_pct=10, regime="normal",
    )
    p = Proposal("B", 15, 10, 30, "半导体", "AI算力", True, False)  # 初始超10、风险预算超、子行业超、现金不足
    kinds = {v.kind for v in engine.validate_all(state, p, on=TODAY)}
    assert {"initial_cap", "risk_budget", "cash_floor"} <= kinds


def test_smallcap_initial_cap(engine):
    state = PortfolioState(cash_pct=90, regime="normal")
    p = Proposal("SMALL", 8, 4, 10, small_cap=True)  # 小盘初始上限 5%
    kinds = {v.kind for v in engine.validate_all(state, p, on=TODAY)}
    assert "initial_cap" in kinds


def test_subindustry_theme_ai_caps(engine):
    # 子行业 光模块 20+20=40%>30 违规；主题 AI算力 40%<50 不违规；AI 敞口 40%<85 不违规
    state = PortfolioState(
        holdings=(
            Holding("A", 20, "光模块", "AI算力", True, True),
            Holding("B", 20, "光模块", "AI算力", True, True),
        ),
        cash_pct=60, regime="normal",
    )
    kinds = {v.kind for v in engine.validate_all(state, on=TODAY)}
    assert "subindustry" in kinds and "theme" not in kinds and "ai_exposure" not in kinds
    # 主题超限：三标的同主题合计 55%>50（各 ≤25 免红线）
    state2 = PortfolioState(
        holdings=(Holding("A", 20, "算力芯片", "AI算力"), Holding("B", 20, "设备", "AI算力"),
                  Holding("C", 15, "光模块", "AI算力")),
        cash_pct=45, regime="normal",
    )
    assert "theme" in {v.kind for v in engine.validate_all(state2, on=TODAY)}


def test_shovel_ratio_floor(engine):
    # 卖铲子不足：A=20 淘金者、B=5 卖铲子 → 5/25=20%<40（各 ≤25 免红线）
    state = PortfolioState(
        holdings=(Holding("A", 20, is_shovel=False), Holding("B", 5, is_shovel=True)),
        cash_pct=75, regime="normal",
    )
    assert "shovel_ratio" in {v.kind for v in engine.validate_all(state, on=TODAY)}
    # 卖铲子充足：A=10 淘金者、B=15 卖铲子 → 60%≥40 ok
    ok_state = PortfolioState(
        holdings=(Holding("A", 10, is_shovel=False), Holding("B", 15, is_shovel=True)),
        cash_pct=75, regime="normal",
    )
    assert "shovel_ratio" not in {v.kind for v in engine.validate_all(ok_state, on=TODAY)}


def test_drift_band_is_alert_not_block(engine):
    state = PortfolioState(holdings=(Holding("A", 15),), cash_pct=85,
                           target_weights={"A": 10})  # 漂移 +5pp > 3pp
    drift = [v for v in engine.validate_all(state, on=TODAY) if v.kind == "drift"]
    assert drift and drift[0].severity == "alert"


def test_cash_floor_by_regime(engine):
    state = PortfolioState(holdings=(Holding("A", 75),), cash_pct=25, regime="overheat")
    assert "cash_floor" in {v.kind for v in engine.validate_all(state, on=TODAY)}  # 过热需 ≥30%


# ---------- 红线绕过被拒（R20.5）----------


def test_redline_bypass_rejected(engine):
    """红线绕过被拒：即使经参数覆盖，25% 硬编码断言仍拦截（R20.5）。"""
    # 直接构造超红线持仓；断言层独立于规则库/参数系统
    state = PortfolioState(holdings=(Holding("BIG", 30),), cash_pct=70)
    kinds = {v.kind for v in engine.validate_all(state, on=TODAY)}
    assert "redline" in kinds
    # 断言层独立于规则库：直接调用也拦截
    with pytest.raises(redline.RedlineViolation):
        redline.assert_single_position("BIG", 25.01)
    with pytest.raises(redline.RedlineViolation):
        redline.assert_leverage(0.1)


def test_redline_param_cannot_be_widened():
    """红线参数不可放宽（只可更严方向亦拒——FIXED）。"""
    from invest_assistant.rules.params import StricterOnlyViolation
    params = load_params()
    with pytest.raises(StricterOnlyViolation):
        params.sign("position.redline_pct", 30, TODAY, pending_trades=False)


# ---------- 预期调节（R15.2）----------


def test_expectation_adjuster_triggers_and_expires():
    rules, params = load_rules(), load_params()
    adj = ExpectationAdjuster(rules, params)
    nav = [("2025-07-08", 400000.0), ("2026-07-08", 850000.0)]  # +112.5%
    out = adj.evaluate(nav, on=TODAY)
    assert out.applied and out.overrides
    assert params.resolve("position.initial_max_pct", TODAY) == 8  # 10 - 2pp
    assert params.resolve("position.initial_max_pct", dt.date(2027, 3, 1)) == 10  # 6 月后回落


def test_expectation_below_threshold_no_override():
    rules, params = load_rules(), load_params()
    adj = ExpectationAdjuster(rules, params)
    nav = [("2025-07-08", 400000.0), ("2026-07-08", 500000.0)]  # +25%
    assert not adj.evaluate(nav, on=TODAY).applied


# ---------- 回撤阶梯（R15.3）----------


def test_drawdown_three_tiers():
    ladder = DrawdownLadder(load_rules())
    assert ladder.evaluate([100, 110, 100]).tier == 0     # -9%
    assert ladder.evaluate([100, 120, 100]).tier == 1     # -16.7%
    d2 = ladder.evaluate([100, 125, 99])
    assert d2.tier == 2 and d2.deleverage                 # -20.8%
    d3 = ladder.evaluate([100, 150, 100])
    assert d3.tier == 3 and d3.defensive_floor and d3.halt_months == 1  # -33%


def test_drawdown_high_water_and_survivors():
    ladder = DrawdownLadder(load_rules())
    d = ladder.evaluate([100, 150, 100])
    assert d.high_water == 150
    survivors = ladder.defensive_survivors([("A", 85), ("B", 70), ("C", None)], d)
    assert survivors == ["A"]  # 仅 ≥80 分保留，评分缺失不保留（保守）
