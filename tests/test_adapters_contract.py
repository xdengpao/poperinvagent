"""适配器契约测试（任务 4.2-4.6）：注入 FakeTransport 验证请求构造/解析/降级语义。"""

import datetime as dt
import json

import pytest

from invest_assistant.data.adapters.base import UpstreamDown
from invest_assistant.data.adapters.edgar import HFIAA_EFFECTIVE, EdgarAdapter, hfiaa_coverage_note
from invest_assistant.data.adapters.fred import FredAdapter
from invest_assistant.data.adapters.http import HttpResponse
from invest_assistant.data.adapters.misc import IbkrAdapter, IndexAdapter, IndustryAdapter
from invest_assistant.data.adapters.tushare import TushareAdapter


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses  # url 片段 → (status, dict|bytes)
        self.calls = []

    def request(self, method, url, *, headers=None, data=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "data": data})
        for frag, (status, body) in self.responses.items():
            if frag in url:
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return HttpResponse(status=status, body=raw)
        raise UpstreamDown(f"unmatched {url}")


# ---------- EDGAR ----------


def test_edgar_requires_user_agent():
    a = EdgarAdapter(FakeTransport({}), user_agent="")
    r = a.probe()
    assert not r.ok and "EDGAR_USER_AGENT" in r.error


def test_edgar_companyfacts_parse():
    payload = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"val": 1000, "end": "2026-01-31", "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-03-01"},
    ]}}}}}
    t = FakeTransport({"companyfacts": (200, payload)})
    rows = EdgarAdapter(t, user_agent="test admin@example.com").fetch({"kind": "companyfacts", "cik": "1045810"})
    assert rows == [{"field": "us-gaap:Revenues", "unit": "USD", "value": 1000,
                     "period_end": "2026-01-31", "fy": 2026, "fp": "FY", "form": "10-K",
                     "filed": "2026-03-01", "frame": None}]
    assert "CIK0001045810" in t.calls[0]["url"]  # CIK 十位补零
    assert "User-Agent" in t.calls[0]["headers"]  # SEC UA 纪律


def test_edgar_hfiaa_boundary():
    """R1.6：FPI Form 4 以 2026-03-18 分界；此前豁免期标注而非 Gap。"""
    payload = {"filings": {"recent": {
        "form": ["4", "4", "10-K"],
        "filingDate": ["2026-02-01", "2026-04-01", "2026-01-15"],
        "accessionNumber": ["a1", "a2", "a3"],
    }}}
    rows = EdgarAdapter(FakeTransport({"submissions": (200, payload)}),
                        user_agent="t t@e.com").fetch({"kind": "submissions", "cik": "1", "is_fpi": True})
    by_acc = {r["accession"]: r for r in rows}
    assert by_acc["a1"].get("hfiaa_exempt_period") is True
    assert "hfiaa_exempt_period" not in by_acc["a2"]
    assert hfiaa_coverage_note(dt.date(2026, 1, 1), is_fpi=True) is not None
    assert hfiaa_coverage_note(HFIAA_EFFECTIVE, is_fpi=True) is None


# ---------- FRED ----------


def test_fred_no_key_probe_fails():
    r = FredAdapter(FakeTransport({}), api_key="").probe()
    assert not r.ok and "FRED_API_KEY" in r.error


def test_fred_observations_skip_missing():
    payload = {"observations": [
        {"date": "2026-07-01", "value": "2.25"},
        {"date": "2026-07-02", "value": "."},  # FRED 缺值符号 → 跳过（上层记 Gap）
    ]}
    rows = FredAdapter(FakeTransport({"observations": (200, payload)}), api_key="k").fetch(
        {"series_id": "DFII10"})
    assert rows == [{"date": "2026-07-01", "value": 2.25}]


# ---------- Tushare ----------


def test_tushare_no_token():
    r = TushareAdapter(FakeTransport({}), token="").probe()
    assert not r.ok and "TUSHARE_TOKEN" in r.error


def test_tushare_coverage_registration():
    """R1.3：逐接口探测积分权限并登记覆盖率（report_rc 权限不足如实登记）。"""

    class T(FakeTransport):
        def request(self, method, url, *, headers=None, data=None, timeout=15.0):
            req = json.loads(data)
            if req["api_name"] == "report_rc":
                return HttpResponse(200, json.dumps({"code": -1, "msg": "积分不足"}).encode())
            return HttpResponse(200, json.dumps(
                {"code": 0, "data": {"fields": ["x"], "items": [[1]]}}).encode())

    r = TushareAdapter(T({}), token="tok").probe()
    assert r.ok  # 核心接口（daily/fina_indicator）可用
    assert r.coverage["report_rc"] is False and r.coverage["daily"] is True


def test_tushare_fetch_rows():
    t = FakeTransport({"tushare": (200, {"code": 0, "data": {
        "fields": ["ts_code", "pledge_ratio"], "items": [["600000.SH", 52.1]]}})})
    # FakeTransport 按 url 片段匹配——tushare API 是固定域名
    t.responses = {"api.tushare.pro": t.responses["tushare"]}
    rows = TushareAdapter(t, token="tok").fetch({"api_name": "pledge_stat"})
    assert rows == [{"ts_code": "600000.SH", "pledge_ratio": 52.1}]


# ---------- INDEX / INDUSTRY / IBKR ----------


def test_index_etf_proxy_table():
    rows = IndexAdapter().fetch({"kind": "etf_proxy"})
    assert {"sector": "半导体", "etfs": ["SOXX", "SMH"]} in rows


def test_industry_snapshot_hash():
    t = FakeTransport({"tsmc": (200, b"<html>rev</html>")})
    rows = IndustryAdapter(t).fetch({"source": "tsmc_monthly_revenue"})
    assert rows[0]["content_hash"] and rows[0]["snapshot_bytes"] == 16


def test_ibkr_unauthorized_probe_and_frozen_fetch():
    """R1.5：IBKR 不可用如实落矩阵；fetch 抛 UpstreamDown（编排层冻结行情节点）。"""
    a = IbkrAdapter(gateway_probe=None)
    assert not a.probe().ok
    with pytest.raises(UpstreamDown):
        a.fetch({"kind": "quotes"})
