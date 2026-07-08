"""市况分级器测试（任务 19，需求 R11；框架 v2.0 §6.4；规格书 §6.4）。"""

import pytest

from invest_assistant.analysis.regime import (
    SIGNAL_IDS,
    RegimeGrader,
    RegimeLevel,
    SignalReading,
)
from invest_assistant.rules import load_rules

DAY = "2026-07-08"
SNAP = "snap-2026-07"


@pytest.fixture(scope="module")
def rules():
    return load_rules()


@pytest.fixture()
def grader(rules):
    return RegimeGrader(rules)


def readings(n_hits: int, gaps: tuple[str, ...] = ()) -> list[SignalReading]:
    """前 n_hits 项命中，gaps 指定项置 None（数据缺口），其余不命中。"""
    out = []
    for i, sid in enumerate(SIGNAL_IDS):
        hit: bool | None = i < n_hits
        if sid in gaps:
            hit = None
        out.append(SignalReading(sid, hit, evidence_id=f"ev-{i}" if hit else ""))
    return out


def assess(grader, n_hits, capex=False, drawdown=-0.05, **kw):
    return grader.assess(
        readings(n_hits), capex_downgrade=capex, index_drawdown=drawdown,
        accounting_day=DAY, snapshot_id=SNAP, **kw,
    )


# —— 注册表（R11.1）——


def test_registry_ten_signals_with_adapter_threshold_frequency(grader):
    """信号池 10 项逐项登记数据源适配器、阈值参数与检测频率（规格书 §6.4〔校验 C-17〕）。"""
    registry = grader.signal_registry()
    assert len(registry) == 10
    assert [e.rule_id for e in registry] == list(SIGNAL_IDS)
    for entry in registry:
        assert entry.params["adapter"], entry.rule_id
        assert entry.params["frequency"], entry.rule_id
        assert len(entry.params) > 2, f"{entry.rule_id}: 缺阈值参数"
        assert entry.source  # 全部挂框架/规格书出处


def test_capex_gate_is_standalone_highest_weight(grader):
    """capex 下修单列最高权重：不在 10 项内、命中即整链警报（F 6.4）。"""
    gate = grader.capex_gate()
    assert gate.rule_id not in SIGNAL_IDS
    assert gate.params["weight"] == "highest"
    assert gate.params["full_chain_alert"] is True
    assert gate.params["mania_with_additional_hits"] == 2


# —— 四级判定边界（R11.2）——


def test_one_hit_is_normal(grader):
    a, _ = assess(grader, 1)
    assert a.level is RegimeLevel.NORMAL
    assert a.hits == 1
    assert a.actions["normal_operation"] is True
    assert a.actions["annual_expectation_band"] == "baseline"


def test_two_hits_is_overheat_and_four_still_overheat(grader):
    a2, _ = assess(grader, 2)
    a4, _ = assess(grader, 4)
    assert a2.level is RegimeLevel.OVERHEAT
    assert a4.level is RegimeLevel.OVERHEAT


def test_five_hits_is_mania(grader):
    a, _ = assess(grader, 5)
    assert a.level is RegimeLevel.MANIA
    assert a.signals_hit == list(SIGNAL_IDS[:5])


def test_capex_downgrade_plus_two_hits_is_mania(grader):
    a, _ = assess(grader, 2, capex=True)
    assert a.level is RegimeLevel.MANIA
    assert a.capex_downgrade is True


def test_freeze_requires_minus30_and_at_most_one_hit(grader):
    a, _ = assess(grader, 1, drawdown=-0.30)
    assert a.level is RegimeLevel.FREEZE
    assert a.actions["intensified_scan"] is True  # 加密扫描（冰点=主要买点来源）
    # 回撤 -30% 但命中 2 项 → 非冰点
    b, _ = assess(grader, 2, drawdown=-0.35)
    assert b.level is RegimeLevel.OVERHEAT
    # 命中 ≤1 但回撤未达 -30% → 常态
    c, _ = assess(grader, 1, drawdown=-0.29)
    assert c.level is RegimeLevel.NORMAL


# —— capex 最高权重语义 ——


def test_capex_highest_weight_semantics(grader):
    """同为 2 项命中：无 capex 仅过热；叠加 capex 直升狂热；capex 单独命中即整链警报。"""
    plain, _ = assess(grader, 2, capex=False)
    boosted, _ = assess(grader, 2, capex=True)
    assert plain.level is RegimeLevel.OVERHEAT
    assert boosted.level is RegimeLevel.MANIA
    alone, _ = assess(grader, 0, capex=True)
    assert alone.level is RegimeLevel.NORMAL  # capex+仅 0/1 项不足狂热
    assert any("整链警报" in x for x in alone.alerts)  # 但命中即整链警报
    assert not plain.alerts


# —— 档位动作（R11.3）——


def test_mania_actions(grader):
    a, _ = assess(grader, 5, holdings=["NVDA", "TSM", "MSFT"])
    assert a.actions["suspend_new_positions"] is True  # 暂停新开仓推荐
    assert a.actions["cash_band_pct"] == [40, 50]  # 现金 40-50%
    assert a.actions["staged_exit_review_all_holdings"] is True
    # 狂热档对全部持仓生成分批兑现评估任务（〔校验 F-09〕）
    assert [(t["task"], t["subject"]) for t in a.tasks] == [
        ("staged_exit_review", "NVDA"),
        ("staged_exit_review", "TSM"),
        ("staged_exit_review", "MSFT"),
    ]
    assert a.action_rule_id == "REGIME.ACTION.MANIA"


def test_overheat_actions(grader):
    a, _ = assess(grader, 3)
    assert a.actions["new_position_double_review"] is True  # 新开仓意见双重复核
    assert a.actions["cash_floor_pct"] == 30  # 现金 ≥30%
    assert a.actions["annual_expectation_band_pct"] == [0, 15]  # 年度预期档输出〔校验 F-03〕
    assert a.tasks == []  # 分批兑现评估任务仅狂热档生成


# —— 缺口协议（A2/R11.2）——


def test_gap_signal_not_counted_and_gap_registered(grader):
    """缺口信号（None）不计命中且逐项产 Gap，零静默。"""
    gap_ids = (SIGNAL_IDS[5], SIGNAL_IDS[6])
    rds = readings(2, gaps=gap_ids)  # 前 2 命中，2 项缺口
    a, _ = grader.assess(rds, capex_downgrade=False, index_drawdown=-0.05,
                         accounting_day=DAY, snapshot_id=SNAP)
    assert a.level is RegimeLevel.OVERHEAT
    assert a.hits == 2  # 缺口不计命中
    assert {g.field_name for g in a.gaps} == set(gap_ids)


def test_missing_signal_and_missing_drawdown_are_gaps_not_hits(grader):
    """未接入的信号与缺失回撤同样走缺口协议；缺回撤不判冰点（保守不放宽）。"""
    partial = readings(1)[:8]  # 后 2 项未接入
    a, _ = grader.assess(partial, capex_downgrade=None, index_drawdown=None,
                         accounting_day=DAY, snapshot_id=SNAP)
    assert a.level is RegimeLevel.NORMAL
    gap_fields = {g.field_name for g in a.gaps}
    assert set(SIGNAL_IDS[8:]) <= gap_fields
    assert "REGIME.CAPEX.DOWNGRADE_GATE" in gap_fields
    assert "index_drawdown" in gap_fields


def test_unknown_signal_rejected(grader):
    with pytest.raises(KeyError):
        grader.assess([SignalReading("REGIME.SIG.NOT_IN_POOL", True)], capex_downgrade=False,
                      index_drawdown=-0.05, accounting_day=DAY, snapshot_id=SNAP)


# —— 未分类重大事件（R11.4）——


def test_unclassified_event_only_manual_channel_zero_auto_actions(grader):
    alert = grader.register_unclassified_event("地缘冲突升级（信号池未覆盖）", DAY)
    assert alert.channel == "O-04"
    assert alert.auto_actions == ()  # 不得自动动作
    assert alert.manual_adjust_direction == "tighten_only"  # 只可人工调严
    assert grader.unclassified_log == [alert]
    # 事件登记不影响市况判定与动作
    a, _ = assess(grader, 1)
    assert a.level is RegimeLevel.NORMAL and a.tasks == []


# —— 级别变更事件（R11.2）——


def test_level_change_emits_event_with_open_interval(grader):
    a1, e1 = assess(grader, 1)  # 首次判定：previous=None → 变更事件
    assert e1 is not None and e1.previous is None and e1.current is RegimeLevel.NORMAL
    assert a1.effective_from == DAY and a1.effective_to is None  # 生效区间开口

    a2, e2 = assess(grader, 3, previous_level=a1.level)  # 常态 → 过热
    assert e2 is not None
    assert (e2.previous, e2.current) == (RegimeLevel.NORMAL, RegimeLevel.OVERHEAT)
    assert e2.kind == "regime_change" and e2.subject == "market"
    assert e2.payload()["current"] == "overheat"

    _, e3 = assess(grader, 3, previous_level=a2.level)  # 级别未变 → 不产事件
    assert e3 is None
