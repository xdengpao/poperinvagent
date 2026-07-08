"""S8 决策关联校验（任务 13.6/14，需求 R15.7；附录 D-4 可执行化）。"""

import datetime as dt

import pytest

from invest_assistant.analysis.thesis import Hypothesis, Mark, ScenarioTriple, ThesisCard
from invest_assistant.opinion.factory import (
    EvidenceItem,
    OpinionFactory,
    OpinionStatus,
    PositionPlan,
    ScenarioTargets,
    ScoreDetail,
    StopLossCheck,
)
from invest_assistant.orchestration.s8_guard import (
    DecisionAction,
    DecisionDraft,
    OpinionRecord,
    S8Verdict,
    check_s8_admission,
)

TODAY = dt.date(2026, 7, 8)
NOW = dt.datetime(2026, 7, 8, 10, 0, tzinfo=dt.UTC)


def _opinion(symbol: str = "NVDA"):
    return OpinionFactory(now=lambda: NOW).reduce(
        symbol=symbol,
        market="US",
        basis_clauses=("7.3",),
        evidence=(EvidenceItem(field_name="权重", value="12%", source="持仓库", as_of=TODAY),),
        score=ScoreDetail(total=70.0, dimensions={"成长": 7.0}),
        scenarios=ScenarioTargets(bear="-30%", base="+100%", bull="+180%"),
        position_plan=PositionPlan(target_low_pct=4.0, target_high_pct=5.0, batch_plan="分 2 批"),
        stop_loss=StopLossCheck(stop_level="成本 -15%", risk_budget_check="通过"),
        risk_alert="6.2 无命中；6.3 无命中",
    )


def _card(symbol: str = "NVDA", signed: bool = True) -> ThesisCard:
    card = ThesisCard(
        card_id="TC-1",
        symbol=symbol,
        one_liner="推理需求驱动收入翻倍",
        hypotheses=tuple(
            Hypothesis(
                hypothesis_id=f"H{i}",
                statement=f"H{i} 假设",
                metric_target="收入 YoY ≥40%",
                deadline=dt.date(2027, 6, 30),
                strengthen_criterion="连续 2 期超预期",
                weaken_criterion="低于预期未达证伪线",
                is_core=i == 0,
            )
            for i in range(3)
        ),
        scenarios=ScenarioTriple(bear="-30%", base="+100%", bull="+180%"),
        falsification="核心 ❌ → 清仓",
        sell_plan="牛市目标分批兑现",
        linked_opinion_id="#2026-07-001",
    )
    if signed:
        card.sign("SO-1")
    return card


def _check(draft: DecisionDraft, *, opinions=None, cards=None, on_date: dt.date = TODAY) -> S8Verdict:
    return check_s8_admission(
        draft, opinions=opinions or {}, thesis_cards=cards or {}, on_date=on_date
    )


# ---------------------------------------------------------------- 买入/加仓类（扩权，R15.7+R13.2）


def test_buy_admitted_with_active_opinion_and_signed_thesis():
    op = _opinion()
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.BUY, opinion_id=op.opinion_id),
        opinions={op.opinion_id: OpinionRecord(op)},
        cards={"NVDA": _card()},
    )
    assert verdict.admitted and verdict.reasons == ()


def test_buy_rejected_when_thesis_unsigned():
    """未签认论点卡 → S8 买入拒绝（R13.2/R15.7）。"""
    op = _opinion()
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.BUY, opinion_id=op.opinion_id),
        opinions={op.opinion_id: OpinionRecord(op)},
        cards={"NVDA": _card(signed=False)},
    )
    assert not verdict.admitted
    assert any("论点卡未签认" in r for r in verdict.reasons)


def test_buy_rejected_when_thesis_missing():
    op = _opinion()
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.ADD, opinion_id=op.opinion_id),
        opinions={op.opinion_id: OpinionRecord(op)},
    )
    assert not verdict.admitted and any("无论点卡" in r for r in verdict.reasons)


def test_buy_rejected_when_opinion_id_unknown():
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.BUY, opinion_id="#2026-07-999"),
        cards={"NVDA": _card()},
    )
    assert not verdict.admitted and any("不存在" in r for r in verdict.reasons)


def test_buy_rejected_when_opinion_expired():
    op = _opinion()
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.BUY, opinion_id=op.opinion_id),
        opinions={op.opinion_id: OpinionRecord(op)},
        cards={"NVDA": _card()},
        on_date=dt.date(2026, 10, 7),  # 超过 2026-10-06 有效期
    )
    assert not verdict.admitted and any("已过有效期" in r for r in verdict.reasons)


def test_buy_rejected_when_opinion_not_active():
    op = _opinion()
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.BUY, opinion_id=op.opinion_id),
        opinions={op.opinion_id: OpinionRecord(op, status=OpinionStatus.EXPIRED)},
        cards={"NVDA": _card()},
    )
    assert not verdict.admitted and any("非 active" in r for r in verdict.reasons)


def test_rejection_reports_all_reasons():
    """拒绝对象含完整理由清单（逐条报告，design §3.6）。"""
    verdict = _check(DecisionDraft(symbol="NVDA", action=DecisionAction.BUY, opinion_id="#2026-07-999"))
    assert not verdict.admitted
    assert len(verdict.reasons) == 2  # 意见不存在 + 无论点卡


def test_buy_without_opinion_id_rejected():
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.BUY), cards={"NVDA": _card()}
    )
    assert not verdict.admitted and any("必须关联系统意见编号" in r for r in verdict.reasons)


def test_opinion_symbol_mismatch_rejected():
    op = _opinion(symbol="AMD")
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.BUY, opinion_id=op.opinion_id),
        opinions={op.opinion_id: OpinionRecord(op)},
        cards={"NVDA": _card()},
    )
    assert not verdict.admitted and any("不一致" in r for r in verdict.reasons)


# ---------------------------------------------------------------- 防守类（A5 方向，R15.7）


@pytest.mark.parametrize(
    "action", [DecisionAction.REDUCE, DecisionAction.SELL, DecisionAction.LIQUIDATE]
)
def test_defensive_not_bound_by_thesis_signoff(action):
    """防守类不受论点卡签认约束，但仍须关联有效意见。"""
    op = _opinion()
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=action, opinion_id=op.opinion_id),
        opinions={op.opinion_id: OpinionRecord(op)},
        cards={"NVDA": _card(signed=False)},  # 论点卡未签认也放行
    )
    assert verdict.admitted


def test_defensive_admitted_with_framework_trigger_only():
    """框架触发来源（如证伪清仓 6.6 顺位第 2 级）可替代意见关联。"""
    card = _card()
    directive = card.record_quarter_marks(
        "2026Q3", {"H0": Mark.FAIL, "H1": Mark.OK, "H2": Mark.OK}, "SO-2"
    )
    assert directive is not None and directive.freeze_exempt
    verdict = _check(
        DecisionDraft(
            symbol="NVDA", action=DecisionAction.LIQUIDATE, framework_trigger="TE-1:证伪清仓(6.6-2)"
        ),
        cards={"NVDA": card},
    )
    assert verdict.admitted


def test_defensive_without_any_linkage_rejected():
    verdict = _check(DecisionDraft(symbol="NVDA", action=DecisionAction.SELL))
    assert not verdict.admitted
    assert any("防守类决策仍须关联" in r for r in verdict.reasons)


def test_defensive_with_expired_opinion_rejected():
    op = _opinion()
    verdict = _check(
        DecisionDraft(symbol="NVDA", action=DecisionAction.REDUCE, opinion_id=op.opinion_id),
        opinions={op.opinion_id: OpinionRecord(op)},
        on_date=dt.date(2026, 10, 7),
    )
    assert not verdict.admitted and any("已过有效期" in r for r in verdict.reasons)
