"""冻结×动作放行矩阵（任务 13.3，需求 R6.3/R12.2/R20.5，铁律 A5）。"""

import pytest

from invest_assistant.orchestration.freeze import (
    ACTION_ADD,
    ACTION_NEW_OPEN,
    DEFENSIVE_CORE,
    ActionClass,
    ActionClassifier,
    AssertionLayerViolation,
    FreezeFlags,
    ReclassifyRejected,
    freeze_gate,
)


def _fully_frozen(subject: str = "NVDA") -> FreezeFlags:
    """全部冻结标志同时生效（三个全局 + 该标的三个标的级）。"""
    flags = FreezeFlags()
    for g in FreezeFlags.GLOBAL_FLAGS:
        flags.set_global(g)
    flags.set_instrument("receipt_missing", subject)
    flags.set_instrument("ruling_pending", subject)
    flags.mark_reeval_48h_overdue(subject)
    return flags


def test_r205_defensive_allowed_under_full_freeze():
    """R20.5 冻结期止损放行：全部冻结标志生效下 DEFENSIVE 动作仍 Allow（A5）。"""
    clf = ActionClassifier()
    flags = _fully_frozen("NVDA")
    assert flags.global_frozen and flags.global_new_open_frozen and flags.instrument_frozen("NVDA")
    for action in sorted(DEFENSIVE_CORE):  # 顺位前三级 + 回撤阶梯全部放行
        decision = freeze_gate(action, clf.classify(action), flags, subject="NVDA")
        assert decision.allowed, f"A5 被破坏：{action} 在冻结期被阻断"
    d = freeze_gate("stop_loss_halve", clf.classify("stop_loss_halve"), flags, subject="NVDA")
    assert "A5" in d.reason  # 放行理由锚定断言层短路分支


def test_global_freeze_blocks_open_and_add():
    """R6.3：全局冻结（任一全局标志）阻断新开仓与加仓，中性动作照常。"""
    clf = ActionClassifier()
    for g in FreezeFlags.GLOBAL_FLAGS:
        flags = FreezeFlags()
        flags.set_global(g)
        assert not freeze_gate(ACTION_NEW_OPEN, clf.classify(ACTION_NEW_OPEN), flags).allowed
        assert not freeze_gate(ACTION_ADD, clf.classify(ACTION_ADD), flags).allowed
        assert freeze_gate("hold_confirm", clf.classify("hold_confirm"), flags).allowed


def test_instrument_freeze_scoped_to_subject():
    """R6.3：标的冻结只阻断该标的扩权类动作，他标的与中性动作不受影响。"""
    flags = FreezeFlags()
    flags.set_instrument("receipt_missing", "NVDA")
    assert not freeze_gate(ACTION_ADD, ActionClass.EXPANSIVE, flags, subject="NVDA").allowed
    assert freeze_gate(ACTION_ADD, ActionClass.EXPANSIVE, flags, subject="AMD").allowed
    assert freeze_gate("hold_confirm", ActionClass.NEUTRAL, flags, subject="NVDA").allowed


def test_r122_48h_overdue_dual_freeze():
    """R20.5/R12.2：48h 复评逾期 → 该标的冻结 + 全局新开仓双冻结，复评完成同时解除。"""
    flags = FreezeFlags()
    flags.mark_reeval_48h_overdue("NVDA")
    assert flags.instrument_frozen("NVDA")
    assert flags.global_new_open_frozen
    assert not flags.global_frozen  # 不触发"新开仓+加仓"级全局冻结
    # 本标的加仓被阻断；组合级任意标的新开仓被阻断；他标的加仓不受影响
    assert not freeze_gate(ACTION_ADD, ActionClass.EXPANSIVE, flags, subject="NVDA").allowed
    assert not freeze_gate(ACTION_NEW_OPEN, ActionClass.EXPANSIVE, flags, subject="AMD").allowed
    assert freeze_gate(ACTION_ADD, ActionClass.EXPANSIVE, flags, subject="AMD").allowed
    flags.clear_reeval_48h_overdue("NVDA")
    assert freeze_gate(ACTION_NEW_OPEN, ActionClass.EXPANSIVE, flags, subject="AMD").allowed


def test_r205_tamper_defensive_core_rejected():
    """R20.5：篡改防守类归类字典被拒——断言层不可配置、不读配置（A5）。"""
    clf = ActionClassifier()
    with pytest.raises(AssertionLayerViolation):
        clf.reclassify("stop_loss_halve", ActionClass.EXPANSIVE)  # 改判核心动作被拒
    with pytest.raises(AssertionLayerViolation):
        clf.reclassify("redline_disposal", ActionClass.DEFENSIVE)  # 同类别写入也被拒
    with pytest.raises(AssertionLayerViolation):
        ActionClassifier(_classes={"falsified_liquidation": ActionClass.EXPANSIVE})  # 构造期投毒被拒
    # 即使内部字典被直接越权改写，classify 也不读它：核心归类硬编码
    clf._classes["stop_loss_halve"] = ActionClass.EXPANSIVE
    assert clf.classify("stop_loss_halve") == ActionClass.DEFENSIVE


def test_reclassify_conservative_direction_only():
    """R6.3：分类字典只允许改判为更保守类别，反向拒绝且改判留痕。"""
    clf = ActionClassifier()
    clf.reclassify(ACTION_ADD, ActionClass.NEUTRAL)  # EXPANSIVE→NEUTRAL 允许
    clf.reclassify(ACTION_ADD, ActionClass.DEFENSIVE)  # NEUTRAL→DEFENSIVE 允许
    with pytest.raises(ReclassifyRejected):
        clf.reclassify(ACTION_ADD, ActionClass.NEUTRAL)  # DEFENSIVE→NEUTRAL 反向拒绝
    with pytest.raises(ReclassifyRejected):
        clf.reclassify("hold_confirm", ActionClass.EXPANSIVE)  # NEUTRAL→EXPANSIVE 反向拒绝
    assert [(a, str(n)) for a, _, n in clf.changelog] == [
        (ACTION_ADD, "neutral"),
        (ACTION_ADD, "defensive"),
    ]


def test_unknown_action_defaults_to_most_restricted():
    """未登记动作按 EXPANSIVE（最受限）处理——保守缺省。"""
    clf = ActionClassifier()
    assert clf.classify("brand_new_action") == ActionClass.EXPANSIVE
    flags = FreezeFlags()
    flags.set_global("review_overdue")
    assert not freeze_gate("brand_new_action", clf.classify("brand_new_action"), flags).allowed


def test_flag_transitions_are_traced():
    """R6.3/设计 §4-4：冻结标志置位/清除为事件，全部留痕。"""
    flags = FreezeFlags()
    flags.set_global("review_overdue", note="月度复盘逾期")
    flags.set_instrument("ruling_pending", "MPWR", note="审计意见口径争议")
    flags.clear_global("review_overdue")
    flags.clear_instrument("ruling_pending", "MPWR")
    assert [(e["op"], e["flag"], e["subject"]) for e in flags.events] == [
        ("set", "review_overdue", ""),
        ("set", "ruling_pending", "MPWR"),
        ("clear", "review_overdue", ""),
        ("clear", "ruling_pending", "MPWR"),
    ]
    with pytest.raises(ValueError):
        flags.set_global("not_a_flag")
    with pytest.raises(ValueError):
        flags.set_instrument("not_a_flag", "NVDA")
