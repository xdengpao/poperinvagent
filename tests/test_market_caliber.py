"""非美披露源 O-03 口径框架（P3 任务 25，需求 R8.3）。"""

import json
from pathlib import Path

from invest_assistant.data.market_caliber import (
    MARKET_CALIBERS,
    ReportFrequency,
    classify_by_market,
    unlock_candidates,
)

ROOT = Path(__file__).resolve().parent.parent


def _b_group() -> list[dict]:
    doc = json.loads((ROOT / "fixtures" / "golden" / "s2" / "inputs.json").read_text())
    return [c for c in doc["candidates"] if c.get("data_scope") == "non-US"]


def test_all_b_group_markets_have_caliber():
    """B 组全部市场均有 O-03 口径登记（否则解锁判定会漏）。"""
    markets = {c["market"] for c in _b_group()}
    for m in markets:
        assert m in MARKET_CALIBERS, f"市场 {m} 缺 O-03 口径登记"


def test_semiannual_period_folding():
    """半年报市场：连续 8 季折算为 4 个半年期（同 ADR 口径，O-03 标注）。"""
    jp = MARKET_CALIBERS["JP"]
    assert jp.cashflow_frequency is ReportFrequency.SEMIANNUAL
    n, note = jp.consecutive_quarters_to_periods(8)
    assert n == 4 and "半年期" in note
    # 季度市场不折算
    tw = MARKET_CALIBERS["TW"]
    assert tw.consecutive_quarters_to_periods(8) == (8, "8 季（季度口径）")


def test_item3_mode_by_market():
    """质押率为 A 股特有；其余市场项 3 走内部人减持单独判定（R9.4/O-03）。"""
    assert MARKET_CALIBERS["CN"].item3_mode() == "pledge_and_sales"
    assert MARKET_CALIBERS["KR"].item3_mode() == "insider_sales_only"
    assert MARKET_CALIBERS["JP"].item3_mode() == "insider_sales_only"


def test_b_group_all_deferred_before_source_connected():
    """B 组市场披露源未接入 → 全部缓评（当前 US/CN 已接入，B 组无这两市场）。"""
    result = unlock_candidates(_b_group())
    assert not result.unlocked                 # 无 B 组标的当前可解锁
    assert len(result.still_deferred) == len(_b_group())
    assert all("缓评" in reason or "待扩展" in reason for _, reason in result.still_deferred)


def test_unlock_when_market_source_connected():
    """接入某市场披露源后，该市场 B 组标的解锁进入 S3。"""
    from dataclasses import replace
    calibers = dict(MARKET_CALIBERS)
    calibers["KR"] = replace(MARKET_CALIBERS["KR"], source_connected=True)  # 接入 DART
    result = unlock_candidates(_b_group(), calibers)
    kr_symbols = [c["symbol"] for c in _b_group() if c["market"] == "KR"]
    assert set(kr_symbols) <= set(result.unlocked)
    # 其余市场仍缓评
    assert any(c["market"] == "JP" for c in _b_group())
    jp_deferred = {s for s, _ in result.still_deferred}
    assert any(c["symbol"] in jp_deferred for c in _b_group() if c["market"] == "JP")


def test_classify_by_market():
    groups = classify_by_market(_b_group())
    assert "000660.KS" in groups["KR"] and "8035.T" in groups["JP"]
    assert len(groups["TW"]) == 5
