"""持仓信号监测器（任务 20，需求 R12），含 R20.5 观察期内信号直清仓。"""

import datetime as dt

import pytest

from invest_assistant.analysis.monitor import (
    Metrics62,
    Metrics63,
    MonitorAction,
    PositionPricing,
    PositionSignalMonitor,
    ReevalConclusion,
    detect_62,
    detect_63,
)
from invest_assistant.calendar import CalendarService
from invest_assistant.orchestration.freeze import FreezeFlags, freeze_gate
from invest_assistant.orchestration.triggers import TriggerBus
from invest_assistant.rules import load_rules

NOW = dt.datetime(2026, 7, 8, 15, 0)
DAY = "2026-07-08"


@pytest.fixture()
def rules():
    return load_rules()


@pytest.fixture()
def mon(rules):
    return PositionSignalMonitor(rules, TriggerBus(), CalendarService(), holdings=["AMD", "NVDA"])


# ---------- 6.2 检测 ----------


def test_detect_62_hit_and_gap(rules):
    """收入连续两季低于指引 ≥10% → 命中；缺口指标不计命中并产 Gap（A2）。"""
    m = Metrics62(revenue_guidance_deviation_pct=12, prior_revenue_guidance_deviation_pct=11)
    rep = detect_62("AMD", m, rules)
    assert "SIG62.REV_GUIDANCE" in rep.hit_ids
    assert rep.gaps  # 其余未提供的指标产缺口，零静默


def test_detect_62_margin_seasonal_not_hit(rules):
    """毛利率下滑达线但季节性可解释 → 不命中。"""
    m = Metrics62(gross_margin_decline_yoy_pp=4, gross_margin_decline_qoq_pp=4,
                  margin_decline_seasonal=True)
    rep = detect_62("AMD", m, rules)
    assert "SIG62.GROSS_MARGIN" not in rep.hit_ids


# ---------- 48h 二选一 + 观察期 ----------


def test_48h_countdown_and_confirmed_liquidate(mon, rules):
    m = Metrics62(insider_sell_cluster=True, executive_departure=False, audit_anomaly=False)
    rep = detect_62("AMD", m, rules)
    assert mon.process_62(rep, NOW, DAY) is None       # 命中 → 启动倒计时，无即时指令
    assert "AMD" in mon.countdowns
    d = mon.conclude_reeval("AMD", ReevalConclusion.CONFIRMED, NOW)
    assert d.action is MonitorAction.LIQUIDATE and d.defensive and d.freeze_exempt
    assert "AMD" not in mon.countdowns


def test_48h_unconfirmed_halve_sets_observation(mon, rules):
    m = Metrics62(insider_sell_cluster=True)
    mon.process_62(detect_62("AMD", m, rules), NOW, DAY)
    d = mon.conclude_reeval("AMD", ReevalConclusion.UNCONFIRMED, NOW)
    assert d.action is MonitorAction.HALVE and d.observation is not None
    assert (d.observation.end - d.observation.start).days == 90    # 一个季度=90 自然日
    assert "AMD" in mon.observations


def test_observation_rehit_direct_liquidate(mon, rules):
    """R20.5：观察期内任一 6.2 信号再命中 → 直接清仓（跳过 48h，防守类不受冻结）。"""
    mon.process_62(detect_62("AMD", Metrics62(insider_sell_cluster=True), rules), NOW, DAY)
    mon.conclude_reeval("AMD", ReevalConclusion.UNCONFIRMED, NOW)  # 进入观察期
    rehit = detect_62("AMD", Metrics62(key_customer_loss=True), rules)
    d = mon.process_62(rehit, NOW, DAY)
    assert d is not None and d.action is MonitorAction.LIQUIDATE
    assert d.skip_48h_reeval and d.defensive and d.freeze_exempt
    assert "AMD" not in mon.observations  # 清仓后观察期解除


def test_observation_expiry_auto_release(mon, rules):
    mon.process_62(detect_62("AMD", Metrics62(insider_sell_cluster=True), rules), NOW, DAY)
    obs = mon.conclude_reeval("AMD", ReevalConclusion.UNCONFIRMED, NOW).observation
    released = mon.sweep_observations(obs.end)
    assert released and released[0].subject == "AMD" and "AMD" not in mon.observations


# ---------- 48h 逾期双冻结 + A5 放行 ----------


def test_48h_overdue_double_freeze_but_defensive_passes(mon, rules):
    """逾期 → 标的+全局新开仓双冻结；但止损减半（防守类）仍放行（A5，R20.5）。"""
    mon.process_62(detect_62("AMD", Metrics62(audit_anomaly=True), rules), NOW, DAY)
    advices = mon.sweep_overdue(NOW + dt.timedelta(hours=49))
    assert advices and advices[0].freeze_global_new_open
    flags = FreezeFlags()
    advices[0].apply_to(flags)
    assert flags.global_new_open_frozen                            # 组合新开仓冻结（property）
    # 防守类止损减半在全冻结下仍放行（A5 断言层）
    from invest_assistant.orchestration.freeze import ActionClass
    assert freeze_gate("stop_loss_halve", ActionClass.DEFENSIVE, flags).allowed


# ---------- 6.3 阈值 ----------


def test_detect_63_thresholds(mon, rules):
    two = Metrics63(ps=35, peg=4, revenue_growth_yoy_pct=20, valuation_persist_quarters=2,
                    implied_tam_share_pct=60)  # PS/PEG + TAM = 2 项
    d2 = mon.evaluate_63(detect_63("NVDA", two, rules), DAY)
    assert d2.action is MonitorAction.REDUCE_EVAL
    three = Metrics63(ps=35, peg=4, revenue_growth_yoy_pct=20, valuation_persist_quarters=2,
                      implied_tam_share_pct=60, price_gain_12m_pct=80, earnings_revision_12m_pct=20)
    d3 = mon.evaluate_63(detect_63("NVDA", three, rules), DAY)
    assert d3.action is MonitorAction.STAGED_TAKE_PROFIT


# ---------- 价格触发 R12.6 ----------


def test_price_triggers(mon):
    pos = PositionPricing("AMD", "US", close=80, stop_loss=85, cost_basis=110, bull_target=200)
    directives, gaps = mon.check_price_triggers(pos, NOW, DAY)
    actions = {d.action for d in directives}
    assert MonitorAction.UNCONDITIONAL_HALVE in actions   # 跌破止损
    assert MonitorAction.FORCED_REREVIEW in actions       # 80 ≤ 110×0.8=88 重审线
    assert not gaps


def test_price_trigger_missing_level_is_gap(mon):
    pos = PositionPricing("AMD", "US", close=80, stop_loss=None, cost_basis=None, bull_target=None)
    directives, gaps = mon.check_price_triggers(pos, NOW, DAY)
    assert not directives and len(gaps) == 3              # 三个阈值缺失全产 Gap（A2）


# ---------- 颠覆联动 R12.7 ----------


def test_disruption_forces_reeval_for_holding(mon):
    d = mon.on_disruption_listed("AMD", DAY)
    assert d is not None and d.action is MonitorAction.FORCED_REEVAL
    assert mon.on_disruption_listed("TSLA", DAY) is None  # 非持仓不联动


# ---------- 重启恢复 ----------


def test_countdown_restart_recovery(mon, rules):
    mon.process_62(detect_62("AMD", Metrics62(audit_anomaly=True), rules), NOW, DAY)
    saved = list(mon.countdowns.values())
    fresh = PositionSignalMonitor(rules, TriggerBus(), CalendarService(), holdings=["AMD"])
    fresh.restore_countdowns(saved)
    assert fresh.countdowns["AMD"].deadline == saved[0].deadline  # 沿用存储 deadline，不重算
