"""参数解析链与只可更严校验（任务 8.3，需求 R5.2/R15.2）。"""

import datetime as dt

import pytest

from invest_assistant.rules import Override, StricterOnlyViolation, load_params

TODAY = dt.date(2026, 7, 8)


def test_signed_values_loaded():
    p = load_params()
    assert p.resolve("score.heavy_line", TODAY) == 75
    assert p.resolve("position.initial_max_pct", TODAY) == 10
    assert p.resolve("position.redline_pct", TODAY) == 25
    assert p.resolve("leverage", TODAY) == 0


def test_stricter_only_direction():
    p = load_params()
    p.sign("score.heavy_line", 80, TODAY, pending_trades=False)  # 判定线只升：OK
    with pytest.raises(StricterOnlyViolation):
        p.sign("score.heavy_line", 70, TODAY, pending_trades=False)
    with pytest.raises(StricterOnlyViolation):
        p.sign("position.initial_max_pct", 12, TODAY, pending_trades=False)  # 上限只降
    with pytest.raises(StricterOnlyViolation):
        p.sign("position.redline_pct", 20, TODAY, pending_trades=False)  # 红线不可调（更严也不行）


def test_pending_trades_blocks_change():
    p = load_params()
    with pytest.raises(StricterOnlyViolation):
        p.sign("score.heavy_line", 80, TODAY, pending_trades=True)  # 有未决交易不生效（R5.2）


def test_override_chain_expectation_adjuster():
    """预期调节（R15.2）：+100% → 单票初始上限 -2pp 覆盖 6 个月，S5/S7 引用覆盖值。"""
    p = load_params()
    p.add_override(Override(
        name="position.initial_max_pct", value=8,
        valid_from=dt.date(2026, 7, 1), valid_to=dt.date(2027, 1, 1),
        origin_rule="F 1.3",
    ))
    assert p.resolve("position.initial_max_pct", TODAY) == 8      # 覆盖生效
    assert p.resolve("position.initial_max_pct", dt.date(2027, 2, 1)) == 10  # 到期回落
    with pytest.raises(StricterOnlyViolation):
        p.add_override(Override(  # 放宽方向的覆盖被拒
            name="position.initial_max_pct", value=15,
            valid_from=TODAY, valid_to=None, origin_rule="X",
        ))


def test_changelog_traceability():
    p = load_params()
    p.sign("cash.floor_normal_pct", 20, TODAY, pending_trades=False, note="用户主动上调")
    assert p.changelog[-1].old == 15 and p.changelog[-1].new == 20  # 留痕（R5.2）
