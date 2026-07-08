"""最小意见构造器（任务 14.7，需求 R14.1/R14.2/R14.9；框架 10.2）。"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from invest_assistant.opinion.factory import (
    DISCLAIMER,
    EvidenceItem,
    Opinion,
    OpinionFactory,
    OpinionNumberIssuer,
    OpinionType,
    PositionPlan,
    ScenarioTargets,
    ScoreDetail,
    StopLossCheck,
)

NOW = dt.datetime(2026, 7, 8, 10, 0, tzinfo=dt.UTC)


def _content_kwargs() -> dict:
    """框架 10.2 前十段内容字段（构造器入参）。"""
    return {
        "symbol": "NVDA",
        "market": "US",
        "basis_clauses": ("7.2", "7.3", "6.6"),
        "evidence": (
            EvidenceItem(
                field_name="数据中心收入 YoY", value="+18%", source="10-Q/EDGAR", as_of=dt.date(2026, 6, 30)
            ),
        ),
        "score": ScoreDetail(total=62.0, dimensions={"管理层": 6.0, "成长": 8.0}),
        "scenarios": ScenarioTargets(bear="-30%", base="+100%", bull="+180%"),
        "position_plan": PositionPlan(target_low_pct=0.0, target_high_pct=0.0, batch_plan="即时清仓"),
        "stop_loss": StopLossCheck(stop_level="不适用——清仓指令", risk_budget_check="通过"),
        "risk_alert": "6.2 命中：核心假设 ❌；6.3 无命中",
    }


def _factory() -> OpinionFactory:
    return OpinionFactory(now=lambda: NOW)


def _full_model_kwargs() -> dict:
    """Opinion 模型全字段（含工厂盖章的编号/生成时间/有效期）。"""
    return _content_kwargs() | {
        "opinion_type": OpinionType.LIQUIDATE,
        "valid_until": dt.date(2026, 10, 6),
        "generated_at": NOW,
        "opinion_id": "#2026-07-001",
    }


# ---------------------------------------------------------------- 十项字段断言（R14.1）

REQUIRED_FIELDS = sorted(set(_full_model_kwargs()))


@pytest.mark.parametrize("missing", REQUIRED_FIELDS)
def test_missing_any_required_field_fails(missing):
    """框架 10.2：「缺任一字段的意见无效，系统不得输出」——模型层缺一构造失败。"""
    kwargs = _full_model_kwargs()
    del kwargs[missing]
    with pytest.raises(ValidationError):
        Opinion(**kwargs)


def test_full_construction_succeeds():
    op = Opinion(**_full_model_kwargs())
    assert op.disclaimer == DISCLAIMER  # 恒附声明（R14.9）


@pytest.mark.parametrize(
    "override",
    [
        {"basis_clauses": ()},  # 依据条文清单空
        {"basis_clauses": ("7.2", " ")},  # 空条目
        {"evidence": ()},  # 数据证据空
        {"opinion_id": "2026-07-001"},  # 编号缺 # 前缀
        {"opinion_id": "#2026-7-1"},  # 编号非 #YYYY-MM-NNN
        {"risk_alert": ""},  # 风险提示空
    ],
)
def test_degenerate_field_values_rejected(override):
    with pytest.raises(ValidationError):
        Opinion(**(_full_model_kwargs() | override))


# ---------------------------------------------------------------- 声明不可移除/不可改（R14.9）


def test_disclaimer_cannot_be_replaced_at_construction():
    with pytest.raises(ValidationError):
        Opinion(**_full_model_kwargs(), disclaimer="本意见仅供参考")
    with pytest.raises(ValidationError):
        Opinion(**_full_model_kwargs(), disclaimer="")


def test_frozen_fields_raise_on_assignment():
    op = Opinion(**_full_model_kwargs())
    with pytest.raises(ValidationError):
        op.disclaimer = ""  # 声明不可移除（CP-8）
    with pytest.raises(ValidationError):
        op.valid_until = dt.date(2099, 1, 1)  # 意见对象整体 frozen（opinion ★ append-only）


# ---------------------------------------------------------------- 编号发号器（R14.2）


def test_issuer_monotonic_within_month():
    issuer = OpinionNumberIssuer()
    day = dt.date(2026, 7, 8)
    assert [issuer.issue(day) for _ in range(3)] == ["#2026-07-001", "#2026-07-002", "#2026-07-003"]
    assert issuer.issue(dt.date(2026, 8, 1)) == "#2026-08-001"  # 跨月重置
    assert issuer.issue(day) == "#2026-07-004"  # 月内继续单调


def test_issuer_concurrent_unique_within_month():
    issuer = OpinionNumberIssuer()
    day = dt.date(2026, 7, 8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: issuer.issue(day), range(200)))
    assert len(set(ids)) == 200  # 同月并发唯一
    assert max(ids) == "#2026-07-200"


# ---------------------------------------------------------------- 有效期（R14.2）


def test_validity_min_of_earnings_and_90d():
    op = _factory().liquidate(**_content_kwargs(), next_earnings_date=dt.date(2026, 8, 20))
    assert op.valid_until == dt.date(2026, 8, 20)  # 下一财报日更早


def test_validity_capped_at_90_natural_days():
    op = _factory().liquidate(**_content_kwargs(), next_earnings_date=dt.date(2027, 1, 15))
    assert op.valid_until == dt.date(2026, 10, 6)  # 2026-07-08 + 90 自然日


def test_validity_degrades_to_90d_without_earnings_calendar():
    """财报日历不可得（None）→ 退化为生成+90 自然日（R14.2/FR-D-08）。"""
    op = _factory().liquidate(**_content_kwargs())
    assert op.valid_until == dt.date(2026, 10, 6)


# ---------------------------------------------------------------- 防守类三构造器（任务 14.7）


def test_three_defensive_constructors():
    f = _factory()
    kwargs = _content_kwargs()
    reduce_kwargs = kwargs | {
        "position_plan": PositionPlan(target_low_pct=0.0, target_high_pct=5.0, batch_plan="至少减半")
    }
    assert f.reduce(**reduce_kwargs).opinion_type is OpinionType.REDUCE
    assert f.sell(**reduce_kwargs).opinion_type is OpinionType.SELL
    assert f.liquidate(**kwargs).opinion_type is OpinionType.LIQUIDATE


def test_liquidation_requires_zero_target():
    bad = _content_kwargs() | {
        "position_plan": PositionPlan(target_low_pct=0.0, target_high_pct=3.0, batch_plan="减到 3%")
    }
    with pytest.raises(ValueError):
        _factory().liquidate(**bad)


def test_factory_stamps_id_and_time():
    op = _factory().sell(**_content_kwargs())
    assert op.opinion_id == "#2026-07-001" and op.generated_at == NOW
