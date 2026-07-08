"""数据质量管道（任务 5，需求 R2.1/R2.2/R2.3/R2.6）——含缺口协议演示。"""

import datetime as dt

import pytest

from invest_assistant.core.types import Evidence, Freshness, Gap, Result
from invest_assistant.data.quality import (
    Conservative,
    FieldClass,
    FieldDictEntry,
    GapLedger,
    arbitrate_categorical,
    arbitrate_numeric,
    stamp_freshness,
)

TODAY = dt.date(2026, 7, 8)


def _ev(field, value, source, as_of="2026-07-01"):
    return Evidence(
        evidence_id=f"EV-{source}-{field}", subject="TEST", field_name=field,
        value=value, source=source, as_of=as_of,
    )


def test_freshness_stale_equals_missing():
    fresh = stamp_freshness(_ev("pe", 30, "a", "2026-07-07"), FieldClass.MARKET, TODAY)
    stale = stamp_freshness(_ev("pe", 30, "a", "2026-06-01"), FieldClass.MARKET, TODAY)
    assert fresh.freshness is Freshness.FRESH
    assert stale.freshness is Freshness.STALE  # 判定中视同缺失（R2.1）


def test_numeric_arbitration_no_threshold():
    """任何分歧（无 1% 门槛）按字段字典保守方向取值 + 落分歧记录（R2.2）。"""
    entry = FieldDictEntry("pledge_ratio", Conservative.HIGHER)
    out = arbitrate_numeric(_ev("pledge_ratio", 50.2, "tushare"), _ev("pledge_ratio", 50.1, "akshare"), entry)
    assert out.evidence.value == 50.2  # 质押率取高
    assert out.discrepancy is not None and out.discrepancy.values == {"tushare": 50.2, "akshare": 50.1}


def test_numeric_arbitration_per_scenario():
    """同字段多消费场景取各自最不利读数（R2.2）。"""
    entry = FieldDictEntry(
        "cash_conversion", Conservative.LOWER, per_scenario={"score_bonus": Conservative.LOWER}
    )
    a, b = _ev("cash_conversion", 1.2, "edgar"), _ev("cash_conversion", 1.5, "fmp")
    assert arbitrate_numeric(a, b, entry, scenario="veto_item2").evidence.value == 1.2


def test_categorical_conflict_to_ruling_queue():
    """分类字段冲突 → Gap + 裁决队列，禁止择一（R2.3）。"""
    out = arbitrate_categorical(_ev("audit_opinion", "unqualified", "edgar"),
                                _ev("audit_opinion", "qualified", "fmp"))
    assert out.evidence is None and out.gap is not None and out.to_ruling_queue


def test_gap_protocol_demo():
    """缺口协议演示（P0 验收项）：断源 → Gap 显式登记 → A2 禁止读值 → 月度缺口率。"""
    ledger = GapLedger()
    gap = ledger.register(Gap(
        field_name="consensus_eps", subject="ALAB",
        attempted_paths=("yfinance", "eastmoney"),
        degradation="一致预期缺失 → 估值维交叉验证按低档计【F 3.3】",
    ))
    r: Result[float] = Result.gap(gap)
    assert r.missing
    with pytest.raises(LookupError):
        _ = r.value  # 类型层禁止把缺口当数值消费（A2）
    assert ledger.monthly_rate(total_nodes=50) == pytest.approx(0.02)
