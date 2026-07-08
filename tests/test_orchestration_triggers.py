"""触发器总线（任务 13.2，需求 R6.4/R6.5/R20.5）。"""

import pytest

from invest_assistant.orchestration.triggers import (
    PRIORITY_LADDER,
    DuplicateConsumption,
    NotDispatchable,
    TriggerBus,
    TriggerStatus,
    priority_rank,
    reconcile,
)

DAY = "2026-07-08"  # 账务日（R4.2 拼合口径）


def test_priority_ladder_is_module_data():
    """R6.5：6.6 顺位表为模块数据——红线>证伪清仓>止损减半>回撤阶梯>重审线>超顶降回>主动申请。"""
    assert PRIORITY_LADDER == (
        "redline_disposal",
        "falsified_liquidation",
        "stop_loss_halve",
        "drawdown_ladder",
        "re_review_line",
        "over_cap_reduce",
        "voluntary_request",
    )
    assert priority_rank("redline_disposal") == 0
    assert priority_rank("voluntary_request") == 6
    assert priority_rank("calendar_task") is None  # 顺位表外触发源不参与仲裁


def test_r205_redline_absorbs_stop_loss_in_batch():
    """R20.5：批内红线吸收止损——absorbed_by 置位、被吸收事件不再派发。"""
    bus = TriggerBus()
    stop = bus.register("stop_loss_halve", "NVDA", DAY)
    red = bus.register("redline_disposal", "NVDA", DAY)
    survivors = bus.arbitrate_batch(DAY)
    assert [e.event_id for e in survivors] == [red.event_id]
    assert stop.status == TriggerStatus.ABSORBED
    assert stop.absorbed_by == red.event_id  # R6.5 高位吸收低位留痕
    with pytest.raises(NotDispatchable):
        bus.claim(stop.event_id, "scheduler")  # 被吸收事件不再派发


def test_absorption_scoped_to_same_subject_and_ladder_kinds():
    """R6.5：吸收只发生在同一标的的顺位表事件之间。"""
    bus = TriggerBus()
    bus.register("redline_disposal", "NVDA", DAY)
    other = bus.register("stop_loss_halve", "AMD", DAY)  # 他标的不被吸收
    calendar = bus.register("calendar_task", "NVDA", DAY)  # 顺位表外不参与仲裁
    survivors = bus.arbitrate_batch(DAY)
    assert other.status == TriggerStatus.REGISTERED
    assert calendar.status == TriggerStatus.REGISTERED
    assert {e.event_id for e in survivors} >= {other.event_id, calendar.event_id}


def test_highest_rank_absorbs_all_lower_ranks():
    """R6.5：批内多事件并发时最高顺位吸收全部更低顺位。"""
    bus = TriggerBus()
    low = bus.register("voluntary_request", "NVDA", DAY)
    mid = bus.register("drawdown_ladder", "NVDA", DAY)
    win = bus.register("falsified_liquidation", "NVDA", DAY)
    survivors = bus.arbitrate_batch(DAY)
    assert [e.event_id for e in survivors] == [win.event_id]
    assert low.absorbed_by == win.event_id and mid.absorbed_by == win.event_id


def test_r205_duplicate_consumption_rejected():
    """R20.5：重复消费拒绝——重复领取与重复确认均报错（R6.4）。"""
    bus = TriggerBus()
    ev = bus.register("stop_loss_halve", "NVDA", DAY)
    bus.arbitrate_batch(DAY)
    bus.claim(ev.event_id, "worker-1")
    with pytest.raises(DuplicateConsumption):
        bus.claim(ev.event_id, "worker-2")  # 已领取不可再领取
    bus.ack(ev.event_id, "worker-1")
    with pytest.raises(DuplicateConsumption):
        bus.ack(ev.event_id, "worker-1")  # 已确认不可再确认
    with pytest.raises(DuplicateConsumption):
        bus.claim(ev.event_id, "worker-1")  # 已消费不可再领取


def test_ack_requires_prior_claim_by_same_consumer():
    """R6.4：未领取先确认、他人越权确认均被拒。"""
    bus = TriggerBus()
    ev = bus.register("re_review_line", "NVDA", DAY)
    with pytest.raises(DuplicateConsumption):
        bus.ack(ev.event_id, "worker-1")
    bus.claim(ev.event_id, "worker-1")
    with pytest.raises(DuplicateConsumption):
        bus.ack(ev.event_id, "worker-2")


def test_r205_reconcile_diff_alerts():
    """R20.5：日终对账差异告警——应触发未处理与超集处理均入差异清单（R6.4）。"""
    bus = TriggerBus()
    done = bus.register("stop_loss_halve", "NVDA", DAY)
    bus.register("re_review_line", "AMD", DAY)  # 登记后未消费 → 差异
    extra = bus.register("voluntary_request", "MSFT", DAY)  # 应触发集外的处理 → 差异
    bus.arbitrate_batch(DAY)
    for ev in (done, extra):
        bus.claim(ev.event_id, "w")
        bus.ack(ev.event_id, "w")
    expected = {("stop_loss_halve", "NVDA"), ("re_review_line", "AMD")}
    report = bus.reconcile_day(DAY, expected)
    assert not report.ok
    assert report.missing == (("re_review_line", "AMD"),)
    assert report.unexpected == (("voluntary_request", "MSFT"),)
    assert len(report.alerts()) == 2 and all("对账告警" in a for a in report.alerts())


def test_reconcile_pure_function_and_absorbed_counts_as_processed():
    """R6.4/R6.5：reconcile(应触发集, 已处理集) 纯函数；被吸收事件视同已处理。"""
    assert reconcile({("a", "X")}, {("a", "X")}).ok
    bus = TriggerBus()
    bus.register("stop_loss_halve", "NVDA", DAY)
    red = bus.register("redline_disposal", "NVDA", DAY)
    bus.arbitrate_batch(DAY)
    bus.claim(red.event_id, "w")
    bus.ack(red.event_id, "w")
    expected = {("stop_loss_halve", "NVDA"), ("redline_disposal", "NVDA")}
    assert bus.reconcile_day(DAY, expected).ok  # 吸收即留痕处理，不产生假差异


def test_registration_persisted_before_consumption():
    """R6.4：登记（持久化）先于消费，全部迁移入 append-only 操作留痕。"""
    bus = TriggerBus()
    ev = bus.register("over_cap_reduce", "NVDA", DAY, payload={"excess_pp": 4}, source="risk")
    bus.claim(ev.event_id, "w")
    bus.ack(ev.event_id, "w")
    assert [op["op"] for op in bus.oplog] == ["register", "claim", "ack"]
    assert ev.payload == {"excess_pp": 4}
