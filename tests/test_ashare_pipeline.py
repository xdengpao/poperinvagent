"""A 股全量接入（P3 任务 24，需求 R1.1/R1.3/R8.3）。"""

import json

from invest_assistant.analysis.s3 import S3Engine
from invest_assistant.data.adapters.http import HttpResponse
from invest_assistant.data.adapters.misc import CninfoAdapter
from invest_assistant.data.ashare_pipeline import AShareActivator, AShareEvidence
from invest_assistant.data.coverage_review import build_coverage_review
from invest_assistant.rules import load_rules


class FakeTransport:
    def __init__(self, status, body):
        self._status, self._body = status, body

    def request(self, method, url, *, headers=None, data=None, timeout=15.0):
        raw = body if isinstance((body := self._body), bytes) else json.dumps(body).encode()
        return HttpResponse(self._status, raw)


# ---------- CNINFO 适配器 ----------


def test_cninfo_announcement_parse():
    payload = {"announcements": [
        {"announcementTitle": "北方华创2025年年度报告", "announcementTime": 1735660800000,
         "adjunctUrl": "finalpage/2026-03-01/abc.PDF", "secCode": "002371"},
    ]}
    rows = CninfoAdapter(FakeTransport(200, payload)).fetch(
        {"kind": "announcements", "stock": "002371,gssz", "category": "periodic"})
    assert rows[0]["title"] == "北方华创2025年年度报告"
    assert rows[0]["pdf_pointer"].endswith("abc.PDF") and rows[0]["sec_code"] == "002371"
    assert "二次确认" in rows[0]["caliber_note"]  # G 级口径标注


def test_cninfo_rejects_unknown_kind():
    import pytest
    with pytest.raises(ValueError):
        CninfoAdapter(FakeTransport(200, {})).fetch({"kind": "bogus", "stock": "x"})


# ---------- C 组激活管道 ----------


def _activator():
    return AShareActivator(S3Engine(load_rules()))


def test_ashare_defers_on_missing_evidence():
    """证据不全 → 保持缓评 + Gap（A2），不进 S3。"""
    ev = AShareEvidence(symbol="688256", audit_opinion_adverse=None)  # 全缺
    r = _activator().activate(ev)
    assert not r.activated and r.gaps and "缓评" in r.deferred_reason


def test_ashare_activates_and_passes_when_clean():
    """证据齐全且各项未命中 → 进 S3 判 pass（CN 口径）。"""
    ev = AShareEvidence(
        symbol="300308",
        regulatory_fraud_case=False, audit_opinion_adverse=False,
        auditor_changes_5y_ge2=False, restatement=False,
        cash_conversion_lt_0_7_8q=False, ar_growth_gt_revenue_20pp_4q=False,
        top_holder_pledge_gt_50pct=False, top_holder_sales_12m_ge2=False,
        ma_revenue_contribution_gt_50pct_3y=False, margin_below_pre_merger=False,
        per_share_metrics_no_growth_3y=False, revenue_growth_via_dilution=False,
    )
    r = _activator().activate(ev, snapshot_id="SNAP-CN")
    assert r.activated and r.verdict.verdict == "pass"
    assert r.verdict.snapshot_id == "SNAP-CN"


def test_ashare_item3_cn_caliber_both_conditions():
    """项 3 A 股口径：质押 >50% 且 12 月减持 ≥2 次（两条件"且"命中才否决）。"""
    base = dict(
        regulatory_fraud_case=False, audit_opinion_adverse=False,
        auditor_changes_5y_ge2=False, restatement=False,
        cash_conversion_lt_0_7_8q=False, ar_growth_gt_revenue_20pp_4q=False,
        ma_revenue_contribution_gt_50pct_3y=False, margin_below_pre_merger=False,
        per_share_metrics_no_growth_3y=False, revenue_growth_via_dilution=False,
    )
    act = _activator()
    # 仅质押高、无减持 → 不命中（且条件）
    r1 = act.activate(AShareEvidence(symbol="X", top_holder_pledge_gt_50pct=True,
                                     top_holder_sales_12m_ge2=False, **base))
    assert r1.verdict.verdict == "pass"
    # 质押高 + 减持≥2 → 命中否决
    r2 = act.activate(AShareEvidence(symbol="Y", top_holder_pledge_gt_50pct=True,
                                     top_holder_sales_12m_ge2=True, **base))
    assert r2.verdict.verdict == "veto"
    item3 = next(i for i in r2.verdict.items if i.item == "item3")
    assert item3.state == "hit"


# ---------- Tushare 覆盖率复议 ----------


def test_coverage_review_subscription_recommendation():
    r = build_coverage_review(
        "2026Q3", tushare_probe_coverage={"daily": True, "fina_indicator": True, "report_rc": False},
        consensus_covered=3, consensus_total=10, degraded_downgrades=4, guidance_quarters=2)
    assert not r.report_rc_available
    assert r.consensus_coverage_rate == 0.3
    assert r.subscription_recommended       # 无 report_rc + 覆盖<50% + 降档≥3
    assert not r.cold_start_switchable      # 仅 2 季
    assert "建议评估付费源订阅" in r.to_markdown()


def test_coverage_review_cold_start_switch():
    r = build_coverage_review(
        "2027Q2", tushare_probe_coverage={"report_rc": True},
        consensus_covered=8, consensus_total=10, degraded_downgrades=0, guidance_quarters=4)
    assert r.report_rc_available and not r.subscription_recommended
    assert r.cold_start_switchable          # 满 4 季 → 可切换实际兑现率
