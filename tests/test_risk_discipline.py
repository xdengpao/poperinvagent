"""批次纪律 + 冷静期 + 次数预算（任务 18.5，需求 R15.4/R15.5/R15.6）。"""

import datetime as dt

import pytest

from invest_assistant.calendar import DurationKind
from invest_assistant.risk.discipline import DisciplineEngine
from invest_assistant.rules import load_params, load_rules

TODAY = dt.date(2026, 7, 8)


@pytest.fixture()
def eng():
    return DisciplineEngine(load_rules(), load_params())


def test_entry_batches(eng):
    assert eng.check_entry_batches(10, [4, 3, 3]).ok       # 3 批，首批 4 ≤ 5
    assert not eng.check_entry_batches(10, [6, 4]).ok      # 首批 6 > 一半
    assert not eng.check_entry_batches(10, [10]).ok        # 1 批 < 最小 2


def test_cap_reduce_batch(eng):
    assert eng.check_cap_reduce_batch(9, 3).ok             # 每批 ≥ 超额 1/3=3
    assert not eng.check_cap_reduce_batch(9, 2).ok


def test_take_profit_fraction_and_interval(eng):
    assert eng.check_take_profit(30, 10, None, TODAY, "US").ok          # 1/3、无上次
    assert not eng.check_take_profit(30, 15, None, TODAY, "US").ok      # >1/3
    recent = dt.date(2026, 7, 6)                                        # 距今不足 5 交易日
    assert not eng.check_take_profit(30, 10, recent, TODAY, "US").ok


# ---------- 拆单避冷静期被拒（R20.5）----------


def test_split_orders_cannot_evade_cooloff(eng):
    """拆单避冷静期被拒：同一标的 24h 内多笔小额加仓聚合 >5pp 仍触发冷静期（R20.5）。"""
    # 单笔均 <5pp，但聚合 2+2+2=6pp > 5pp
    dur = eng.cooloff_required([2.0, 2.0, 2.0], emotion_score=0)
    assert dur is not None and dur.amount == 24 and dur.kind is DurationKind.WALL_CLOCK_HOURS


def test_cooloff_none_when_under_threshold(eng):
    assert eng.cooloff_required([2.0, 2.0], emotion_score=0) is None    # 4pp ≤ 5pp


def test_emotion_extends_cooloff(eng):
    dur = eng.cooloff_required([1.0], emotion_score=4)                  # 情绪 ≥4 → 48h 即使加仓量小
    assert dur is not None and dur.amount == 48


def test_cooloff_until_wall_clock(eng):
    dur = eng.cooloff_required([6.0], emotion_score=0)
    until = eng.cooloff_until(dt.datetime(2026, 7, 8, 10, 0), dur)
    assert until == dt.datetime(2026, 7, 9, 10, 0)


# ---------- 次数预算（R15.6）----------


def test_counts_budget(eng):
    assert eng.counts_ok(5, 5, on=TODAY, kind="trade").ok
    assert not eng.counts_ok(6, 0, on=TODAY, kind="trade").ok           # 月主动达 6
    assert not eng.counts_ok(0, 6, on=TODAY, kind="swap").ok            # 年换股达 6
