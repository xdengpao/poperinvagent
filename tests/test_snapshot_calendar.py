"""快照冻结/活数据白名单（任务 7，R3）与交易日历（任务 3，R4）。"""

import datetime as dt

import pytest

from invest_assistant.calendar import CalendarService, Duration, DurationKind
from invest_assistant.core.types import Evidence
from invest_assistant.data.snapshot import (
    LiveDataForbidden,
    SnapshotFrozen,
    SnapshotStore,
    read_live,
)


def _ev(eid):
    return Evidence(evidence_id=eid, subject="X", field_name="f", value=1)


def test_monthly_snapshot_freeze_semantics():
    """切齐即冻结；补跑产物进下一版本快照（R3.1）。"""
    store = SnapshotStore()
    s = store.create_monthly(dt.date(2026, 7, 7), "rules-v1", "params-v1", ["裁决A"])
    s.add_evidence(_ev("EV-1"))
    h = s.freeze()
    assert h and s.frozen
    with pytest.raises(SnapshotFrozen):
        s.add_evidence(_ev("EV-2"))
    s2 = store.create_monthly(dt.date(2026, 8, 7), "rules-v1", "params-v1", ["裁决A"])
    s2.add_evidence(_ev("EV-2"))  # 下一版本快照接收补跑产物


def test_snapshot_hash_covers_ruling_set():
    """快照内容哈希覆盖生效裁决集（裁决集不同 → 哈希不同，R3.1）。"""
    store = SnapshotStore()
    a = store.create_monthly(dt.date(2026, 7, 7), "v1", "v1", [])
    b = store.create_monthly(dt.date(2026, 7, 7), "v1", "v1", ["裁决A"])
    assert a.freeze() != b.freeze()


def test_live_data_whitelist():
    """仅信号检测与净值监控可读活数据（R3.5）。"""
    read_live("SignalDetector")
    read_live("NavMonitor")
    with pytest.raises(LiveDataForbidden):
        read_live("S4Engine")


def test_duration_three_kinds():
    """时间量词三口径（R4.3）：交易日/墙钟/自然日，禁止无口径表达。"""
    assert Duration.parse("30td").kind is DurationKind.TRADING_DAYS
    assert Duration.parse("48h").kind is DurationKind.WALL_CLOCK_HOURS
    assert Duration.parse("90d").kind is DurationKind.NATURAL_DAYS
    with pytest.raises(ValueError):
        Duration.parse("30")  # 无口径


def test_trading_day_arithmetic_and_accounting_day():
    cal = CalendarService()
    d = cal.add("US", dt.date(2026, 7, 2), Duration.parse("2td"))
    assert cal.is_trading_day("US", d)
    assert not cal.is_trading_day("US", dt.date(2026, 7, 4))  # 独立日周六
    acc = cal.accounting_day(dt.date(2026, 7, 6))
    assert acc["CN"] <= acc["accounting"]  # A 股取同账务日最近（≤）收盘（R4.2）


def test_wall_clock_cooling_period():
    cal = CalendarService()
    start = dt.datetime(2026, 7, 8, 14, 30)
    assert cal.add_wall_clock(start, Duration.parse("24h")) == dt.datetime(2026, 7, 9, 14, 30)
