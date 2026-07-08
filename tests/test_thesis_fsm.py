"""论点卡与证伪状态机（任务 14，需求 R13.1-R13.5；框架 7.2/附录 A）。

观察期语义（R12.3/R12.4）不在本模块——由持仓信号监测器实现，此处不测。
"""

import datetime as dt

import pytest

from invest_assistant.analysis.thesis import (
    ActionKind,
    Hypothesis,
    Mark,
    ScenarioTriple,
    SupersedeRejected,
    ThesisCard,
    ThesisError,
    ThesisState,
    ThesisStateError,
    TightenOnlyViolation,
    UnsignedMarkError,
    new_thesis,
)

DEADLINE = dt.date(2027, 6, 30)


def _hyp(hid: str, core: bool = False, metric: str = "数据中心收入 YoY ≥40%") -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        statement=f"{hid}：收入拐点假设",
        metric_target=metric,
        deadline=DEADLINE,
        strengthen_criterion="连续 2 期数据超预期",
        weaken_criterion="低于预期但未达证伪线",
        is_core=core,
    )


def _card(card_id: str = "TC-1", symbol: str = "NVDA", n: int = 3, cores: int = 1) -> ThesisCard:
    hyps = tuple(_hyp(f"H{i}", core=i < cores) for i in range(n))
    return ThesisCard(
        card_id=card_id,
        symbol=symbol,
        one_liner="推理需求驱动数据中心收入翻倍",
        hypotheses=hyps,
        scenarios=ScenarioTriple(bear="-30%", base="+100%", bull="+180%"),
        falsification="核心假设 ❌ → 无条件清仓",
        sell_plan="牛市目标价分批兑现，每次 1/3",
        linked_opinion_id="#2026-07-001",
    )


def _all(card: ThesisCard, mark: Mark) -> dict[str, Mark]:
    return {h.hypothesis_id: mark for h in card.hypotheses}


# ---------------------------------------------------------------- 构造断言（R13.1）


def test_hypothesis_count_3_to_5():
    with pytest.raises(ThesisError):
        _card(n=2)
    with pytest.raises(ThesisError):
        _card(n=6, cores=2)
    _card(n=3)
    _card(n=5, cores=2)


def test_core_count_1_to_2():
    with pytest.raises(ThesisError):
        _card(cores=0)
    with pytest.raises(ThesisError):
        _card(n=5, cores=3)
    _card(cores=2)


def test_hypothesis_must_contain_number():
    with pytest.raises(ThesisError):
        _hyp("H9", metric="收入显著增长")  # 无数字 → 拒绝（框架 7.2 带数字与时间）


def test_card_required_fields():
    with pytest.raises(ThesisError):
        ThesisCard(
            card_id="TC-X",
            symbol="NVDA",
            one_liner="x",
            hypotheses=tuple(_hyp(f"H{i}", core=i == 0) for i in range(3)),
            scenarios=ScenarioTriple(bear="-30%", base="+100%", bull="+180%"),
            falsification="",  # 缺证伪条件与动作
            sell_plan="计划",
            linked_opinion_id="#2026-07-001",
        )


# ---------------------------------------------------------------- 签认门与修改（R13.2）


def test_draft_blocks_expansion_until_signed():
    card = _card()
    assert card.state is ThesisState.DRAFT
    assert not card.expansion_allowed  # 签认前不得放行买入/加仓类（R13.2）
    card.sign("SO-1")
    assert card.state is ThesisState.SIGNED
    assert card.expansion_allowed


def test_marking_requires_signed_state():
    card = _card()
    with pytest.raises(ThesisStateError):
        card.record_quarter_marks("2026Q3", _all(card, Mark.OK), "SO-2")


def test_tighten_only_hook():
    card = _card()
    looser = [_hyp("H0", core=True, metric="收入 YoY ≥20%"), _hyp("H1"), _hyp("H2")]
    with pytest.raises(TightenOnlyViolation):
        card.revise_hypotheses(looser, tighten_check=lambda old, new: False)  # 钩子判为放松 → 拒绝
    tighter = [_hyp("H0", core=True, metric="收入 YoY ≥50%"), _hyp("H1"), _hyp("H2")]
    card.revise_hypotheses(tighter, tighten_check=lambda old, new: True)
    assert card.hypotheses[0].metric_target == "收入 YoY ≥50%"


def test_revise_cannot_swap_hypotheses():
    card = _card()
    swapped = [_hyp("H0", core=True), _hyp("H1"), _hyp("H9")]  # 换条目=换逻辑 → 走 new_thesis
    with pytest.raises(TightenOnlyViolation):
        card.revise_hypotheses(swapped, tighten_check=lambda old, new: True)


# ---------------------------------------------------------------- 季度标记（R13.3）


def test_marks_require_signoff():
    card = _card()
    card.sign("SO-1")
    with pytest.raises(UnsignedMarkError):
        card.record_quarter_marks("2026Q3", _all(card, Mark.OK), signoff_id="")


def test_marks_must_cover_all_hypotheses():
    card = _card()
    card.sign("SO-1")
    with pytest.raises(ThesisError):
        card.record_quarter_marks("2026Q3", {"H0": Mark.OK}, "SO-2")


def test_mark_history_and_warn_counter_persisted():
    card = _card()
    card.sign("SO-1")
    marks = _all(card, Mark.OK) | {"H1": Mark.WARN}
    assert card.record_quarter_marks("2026Q3", marks, "SO-2") is None
    assert card.mark_history[0].period == "2026Q3"
    assert card.consecutive_warn["H1"] == 1 and card.consecutive_warn["H0"] == 0


def test_duplicate_period_rejected():
    card = _card()
    card.sign("SO-1")
    card.record_quarter_marks("2026Q3", _all(card, Mark.OK), "SO-2")
    with pytest.raises(ThesisError):
        card.record_quarter_marks("2026Q3", _all(card, Mark.OK), "SO-3")


# ---------------------------------------------------------------- 动作判定（R13.4）


def test_core_fail_single_quarter_unconditional_liquidation():
    """核心 ❌ 单季 → 无条件清仓；标注 6.6 顺位第 2 级、防守类不受冻结（A5）。"""
    card = _card()
    card.sign("SO-1")
    directive = card.record_quarter_marks("2026Q3", _all(card, Mark.OK) | {"H0": Mark.FAIL}, "SO-2")
    assert directive is not None
    assert directive.kind is ActionKind.LIQUIDATE
    assert directive.unconditional  # 不看价格（框架 7.2）
    assert directive.priority_66 == 2  # 6.6 顺位第 2 级证伪清仓
    assert directive.defensive and directive.freeze_exempt  # 防守类不受冻结阻断（A5）
    assert card.state is ThesisState.INVALIDATED
    assert card.pending_exit is directive


def test_two_consecutive_warns_at_least_halve():
    card = _card()
    card.sign("SO-1")
    warn = _all(card, Mark.OK) | {"H1": Mark.WARN}
    assert card.record_quarter_marks("2026Q3", warn, "SO-2") is None
    directive = card.record_quarter_marks("2026Q4", warn, "SO-3")
    assert directive is not None and directive.kind is ActionKind.HALVE_AT_LEAST
    assert directive.defensive
    assert card.state is ThesisState.SIGNED  # 减半后论点仍存续，进入第三季观察


def test_third_consecutive_warn_liquidation():
    card = _card()
    card.sign("SO-1")
    warn = _all(card, Mark.OK) | {"H1": Mark.WARN}
    card.record_quarter_marks("2026Q3", warn, "SO-2")
    card.record_quarter_marks("2026Q4", warn, "SO-3")
    directive = card.record_quarter_marks("2027Q1", warn, "SO-4")
    assert directive is not None and directive.kind is ActionKind.LIQUIDATE
    assert directive.priority_66 == 2
    assert card.state is ThesisState.INVALIDATED


def test_ok_quarter_resets_warn_streak():
    card = _card()
    card.sign("SO-1")
    warn = _all(card, Mark.OK) | {"H1": Mark.WARN}
    card.record_quarter_marks("2026Q3", warn, "SO-2")
    card.record_quarter_marks("2026Q4", _all(card, Mark.OK), "SO-3")  # ✅ 归零
    assert card.consecutive_warn["H1"] == 0
    assert card.record_quarter_marks("2027Q1", warn, "SO-4") is None  # 重新计 1，不触发


def test_warns_on_different_hypotheses_do_not_chain():
    """连续 ⚠️ 按同一假设计数（框架 7.2"第三季仍 ⚠️"语义）。"""
    card = _card()
    card.sign("SO-1")
    card.record_quarter_marks("2026Q3", _all(card, Mark.OK) | {"H1": Mark.WARN}, "SO-2")
    assert card.record_quarter_marks("2026Q4", _all(card, Mark.OK) | {"H2": Mark.WARN}, "SO-3") is None


# ---------------------------------------------------------------- 换故事续命防御（R13.5）


def _invalidated_card() -> ThesisCard:
    card = _card()
    card.sign("SO-1")
    card.record_quarter_marks("2026Q3", _all(card, Mark.OK) | {"H0": Mark.FAIL}, "SO-2")
    return card


def test_new_thesis_rejected_before_exit_executed():
    old = _invalidated_card()
    draft = _card(card_id="TC-2")
    with pytest.raises(SupersedeRejected):
        new_thesis(old, draft, s3_verdict_id="VV-99")  # 原卡失效动作未完成 → 拒绝


def test_new_thesis_rejected_on_live_card():
    old = _card()
    old.sign("SO-1")
    with pytest.raises(SupersedeRejected):
        new_thesis(old, _card(card_id="TC-2"), s3_verdict_id="VV-99")  # 原论点未失效不许替换


def test_new_thesis_requires_s3_rerun():
    old = _invalidated_card()
    old.confirm_exit_executed("TR-1")
    with pytest.raises(SupersedeRejected):
        new_thesis(old, _card(card_id="TC-2"), s3_verdict_id="")  # 未重走 S3 → 拒绝


def test_new_thesis_after_exit_and_s3():
    old = _invalidated_card()
    old.confirm_exit_executed("TR-1")
    draft = new_thesis(old, _card(card_id="TC-2"), s3_verdict_id="VV-99")
    assert draft.supersedes == "TC-1" and draft.s3_verdict_id == "VV-99"
    assert draft.state is ThesisState.DRAFT  # 新卡仍须走签认（R13.2）
