"""端到端场景断言（任务 22，需求 R20.5 + R17.4 + R18.4）。

跨模块驱动真实编排路径，验证铁律级不变量。个别场景在各自模块也有单测，
本文件从系统边界再断言一次（集成层）。
"""

import datetime as dt

import pytest

from invest_assistant.analysis.monitor import (
    Metrics62,
    PositionSignalMonitor,
    ReevalConclusion,
    detect_62,
)
from invest_assistant.analysis.s3 import CaseInput, ItemInput, S3Engine
from invest_assistant.analysis.s4 import DimensionInput, S4Engine
from invest_assistant.analysis.thesis import (
    Hypothesis,
    ScenarioTriple,
    ThesisCard,
)
from invest_assistant.calendar import CalendarService
from invest_assistant.opinion.factory import (
    EvidenceItem,
    OpinionFactory,
    OpinionStatus,
    PositionPlan,
    ScenarioTargets,
    ScoreDetail,
    StopLossCheck,
)
from invest_assistant.orchestration.freeze import (
    ActionClass,
    FreezeFlags,
    freeze_gate,
)
from invest_assistant.orchestration.s8_guard import (
    DecisionAction,
    DecisionDraft,
    OpinionRecord,
    check_s8_admission,
)
from invest_assistant.risk import redline
from invest_assistant.risk.discipline import DisciplineEngine
from invest_assistant.rules import load_params, load_rules

TODAY = dt.date(2026, 7, 8)


# ==================== 22.1 冻结期止损放行（A5）====================


def test_stop_loss_passes_under_all_freezes():
    """所有冻结标志同时生效下，防守类止损减半仍放行（A5 断言层短路）。"""
    flags = FreezeFlags()
    flags.set_global("review_overdue", note="复盘逾期")
    flags.set_global("drawdown_15pct", note="回撤-15%")
    flags.mark_reeval_48h_overdue("AMD", note="48h 逾期")
    # 扩权类被全阻断
    assert not freeze_gate("new_open", ActionClass.EXPANSIVE, flags).allowed
    # 防守类止损减半：A5 第一分支放行
    assert freeze_gate("stop_loss_halve", ActionClass.DEFENSIVE, flags).allowed
    assert freeze_gate("falsification_liquidate", ActionClass.DEFENSIVE, flags).allowed


# ==================== 22.1 观察期内信号直清仓 ====================


def test_observation_period_direct_liquidation_e2e():
    from invest_assistant.orchestration.triggers import TriggerBus

    rules = load_rules()
    mon = PositionSignalMonitor(rules, TriggerBus(), CalendarService(), holdings=["AMD"])
    now = dt.datetime(2026, 7, 8, 15, 0)
    # 首次 6.2 命中 → 48h → 不能确认 → 减半 + 观察期
    mon.process_62(detect_62("AMD", Metrics62(insider_sell_cluster=True), rules), now, "2026-07-08")
    d = mon.conclude_reeval("AMD", ReevalConclusion.UNCONFIRMED, now)
    assert d.observation is not None
    # 观察期内再命中任一 6.2 → 直接清仓（跳过 48h，防守类不受冻结）
    d2 = mon.process_62(detect_62("AMD", Metrics62(key_customer_loss=True), rules), now, "2026-07-09")
    assert d2 is not None and d2.action.value == "清仓"
    assert d2.skip_48h_reeval and d2.freeze_exempt


# ==================== 22.2 拆单避冷静期被拒 ====================


def test_split_orders_cannot_evade_cooloff_e2e():
    eng = DisciplineEngine(load_rules(), load_params())
    # 三笔各 2pp（单笔 <5），24h 内聚合 6pp > 5pp → 冷静期不可避
    dur = eng.cooloff_required([2.0, 2.0, 2.0], emotion_score=0)
    assert dur is not None and dur.amount == 24


# ==================== 22.2 红线绕过被拒 ====================


def test_redline_bypass_rejected_e2e():
    params = load_params()
    from invest_assistant.rules.params import StricterOnlyViolation
    # 参数系统层：红线不可放宽
    with pytest.raises(StricterOnlyViolation):
        params.sign("position.redline_pct", 30, TODAY, pending_trades=False)
    # 断言层：即使绕过参数，硬编码断言仍拦截
    with pytest.raises(redline.RedlineViolation):
        redline.check_redlines(positions={"BIG": 25.01}, leverage=0)
    with pytest.raises(redline.RedlineViolation):
        redline.check_redlines(positions={"OK": 20}, leverage=0.5)


# ==================== 22.3 四态拒绝：S8 未签认论点卡/无有效意见 ====================


def _opinion(factory, symbol="AMD"):
    return factory.buy(
        symbol=symbol, market="US", basis_clauses=("3.4", "10.1"),
        evidence=(EvidenceItem(field_name="AI收入占比", value="45%", source="EDGAR", as_of=TODAY),),
        score=ScoreDetail(total=80, dimensions={"AI收入真实性": 18}),
        scenarios=ScenarioTargets(bear="-20%", base="+120%", bull="+300%"),
        position_plan=PositionPlan(target_low_pct=0, target_high_pct=8, batch_plan="分3批"),
        stop_loss=StopLossCheck(stop_level="-18%", risk_budget_check="首批×距离≤2%"),
        risk_alert="无信号命中",
    )


def _thesis(signed: bool) -> ThesisCard:
    hyps = tuple(
        Hypothesis(f"h{i}", f"假设{i}", metric_target="收入 2026 增 50%",
                   deadline=dt.date(2026, 12, 31), strengthen_criterion="连续超预期",
                   weaken_criterion="低于预期", is_core=(i == 1))
        for i in range(1, 4)
    )
    card = ThesisCard(card_id="tc1", symbol="AMD", one_liner="AI算力瓶颈受益",
                      hypotheses=hyps, scenarios=ScenarioTriple("-20%", "+120%", "+300%"),
                      falsification="核心假设单季❌→清仓", sell_plan="分批",
                      linked_opinion_id="#2026-07-001")
    if signed:
        card.sign("so1")
    return card


def test_s8_rejects_buy_without_signed_thesis():
    factory = OpinionFactory(now=lambda: dt.datetime(2026, 7, 8, 12, 0))
    op = _opinion(factory)
    draft = DecisionDraft("AMD", DecisionAction.BUY, opinion_id=op.opinion_id)
    # 未签认论点卡 → 买入拒绝
    v = check_s8_admission(draft, opinions={op.opinion_id: OpinionRecord(op)},
                           thesis_cards={"AMD": _thesis(signed=False)}, on_date=TODAY)
    assert not v.admitted and v.reasons
    # 已签认 → 放行
    v2 = check_s8_admission(draft, opinions={op.opinion_id: OpinionRecord(op)},
                            thesis_cards={"AMD": _thesis(signed=True)}, on_date=TODAY)
    assert v2.admitted


def test_s8_rejects_buy_without_valid_opinion():
    factory = OpinionFactory(now=lambda: dt.datetime(2026, 7, 8, 12, 0))
    op = _opinion(factory)
    draft = DecisionDraft("AMD", DecisionAction.BUY, opinion_id=op.opinion_id)
    # 意见已作废 → 拒绝
    v = check_s8_admission(draft, opinions={op.opinion_id: OpinionRecord(op, OpinionStatus.EXPIRED)},
                           thesis_cards={"AMD": _thesis(signed=True)}, on_date=TODAY)
    assert not v.admitted
    # 无关联意见编号 → 拒绝
    v2 = check_s8_admission(DecisionDraft("AMD", DecisionAction.BUY),
                            opinions={}, thesis_cards={"AMD": _thesis(signed=True)}, on_date=TODAY)
    assert not v2.admitted


def test_s8_defensive_not_blocked_by_unsigned_thesis():
    """防守类（清仓）不受论点卡签认约束（A5）——但须有触发来源。"""
    draft = DecisionDraft("AMD", DecisionAction.LIQUIDATE, framework_trigger="6.2 证伪清仓")
    v = check_s8_admission(draft, opinions={}, thesis_cards={"AMD": _thesis(signed=False)}, on_date=TODAY)
    assert v.admitted


# ==================== 22.3 四态拒绝：S4 未签认定性维挂起 ====================


def test_s4_unsigned_qualitative_blocks_judgement():
    engine = S4Engine(load_rules(), load_params())
    dims = [
        DimensionInput("产业链位置", band="high", points=14),
        DimensionInput("AI收入真实性", band="high", points=18),
        DimensionInput("市场空间", band="mid", points=7),
        DimensionInput("护城河", band="high", points=13, agent_draft=True),
        DimensionInput("管理层", band="mid", points=7, agent_draft=True),
        DimensionInput("估值", band="mid", points=10),
        DimensionInput("财务健康", band="high", points=9),
        DimensionInput("拥挤度", band="mid", points=3),
    ]
    card = engine.score("AMD", dims, signed_off=set(), on=TODAY)
    assert card.judgement == "blocked_pending_signoff"  # 未签认定性维 → 判定挂起


# ==================== 22.3 四态拒绝：S3 未决裁决冻结 ====================


def test_s3_edge_generates_ruling_freeze():
    engine = S3Engine(load_rules())
    case = CaseInput(
        symbol="NVDA", market="US",
        items={
            "item1": ItemInput({"regulatory_fraud_case": True, "audit_opinion_adverse": None,
                                "auditor_changes_5y_ge2": None, "restatement_8k402": None}),
            "item2": ItemInput({"cash_conversion_lt_0_7_8q": False, "ar_growth_gt_revenue_20pp_4q": False},
                               concluded_not_hit=True),
            "item3": ItemInput({}, concluded_not_hit=True),
            "item4": ItemInput({"ma_revenue_contribution_gt_50pct_3y": False, "margin_below_pre_merger": None},
                               concluded_not_hit=True),
            "item5": ItemInput({"per_share_metrics_no_growth_3y": None, "revenue_growth_via_dilution": False},
                               concluded_not_hit=True),
        },
    )
    v = engine.judge(case)
    assert v.verdict == "edge" and v.ruling_needed  # 待裁决 → 冻结扩权


# ==================== 22.4 S0 不适用情形硬拒绝 ====================


def test_s0_hard_reject_learning_only():
    from invest_assistant.ui.queues import s0_admission
    for fund in ("杠杆资金", "借贷资金", "限期翻倍资金", "期货期权投机资金"):
        d = s0_admission([fund])
        assert not d.admitted and d.mode == "learning_only"


# ==================== 22.5 一跳回溯：A4 三元组完整性 ====================


def test_one_hop_traceability_triple_ref():
    """判定产物携带规则 ID + 快照 ID + 证据 ID 三元组（A4 全链可回溯）。"""
    # S3 判定
    engine = S3Engine(load_rules())
    case = CaseInput("AMD", "US", items={
        f"item{i}": ItemInput({}, concluded_not_hit=True) for i in range(1, 6)})
    v = engine.judge(case, snapshot_id="SNAP-M-2026-07-07-001")
    assert v.rule_ids and v.snapshot_id == "SNAP-M-2026-07-07-001"
    # S4 判定同样携带三元组
    s4 = S4Engine(load_rules(), load_params())
    dims = [DimensionInput("产业链位置", band="high", points=14),
            DimensionInput("AI收入真实性", band="high", points=18),
            DimensionInput("市场空间", band="mid", points=7),
            DimensionInput("护城河", band="high", points=13),
            DimensionInput("管理层", band="mid", points=7),
            DimensionInput("估值", band="mid", points=10),
            DimensionInput("财务健康", band="high", points=9),
            DimensionInput("拥挤度", band="mid", points=3)]
    card = s4.score("AMD", dims, signed_off={"护城河", "管理层"}, snapshot_id="SNAP-X", on=TODAY)
    assert card.rule_ids and card.snapshot_id == "SNAP-X"
