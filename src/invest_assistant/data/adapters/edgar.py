"""ADP-EDGAR：SEC EDGAR 适配器（任务 4.2，需求 R1.6；美股 D2/D4 权威源/仲裁源）。

- XBRL companyfacts / companyconcept（结构化财务，D2）；
- submissions（10-K/Q、20-F/6-K、8-K 4.01/4.02、Form 3/4/144、DEF 14A、SC 13D/G、424B5/S-3 目录，D4）；
- HFIAA 分界（R1.6）：FPI 标的 Form 4 自 2026-03-18 起采集，此前区间返回"豁免期"标注对象（非 Gap）。
SEC 要求显式 User-Agent（含联系方式），经 EDGAR_USER_AGENT 环境变量配置。
"""

from __future__ import annotations

import datetime as _dt
import os

from .base import BaseAdapter, ProbeResult, UpstreamDown
from .http import HttpResponse, HttpTransport, UrllibTransport

HFIAA_EFFECTIVE = _dt.date(2026, 3, 18)
_BASE_DATA = "https://data.sec.gov"

#: D4 关注的披露类型（规格书 §3.1）
WATCHED_FORMS = frozenset({
    "10-K", "10-Q", "20-F", "6-K", "8-K", "3", "4", "144",
    "DEF 14A", "SC 13D", "SC 13G", "424B5", "S-3",
})


class EdgarAdapter(BaseAdapter):
    name = "ADP-EDGAR"
    required_in_production = True

    def __init__(self, transport: HttpTransport | None = None, user_agent: str | None = None):
        self._t = transport or UrllibTransport(self.rate_limit)
        self._ua = user_agent or os.environ.get("EDGAR_USER_AGENT", "")

    def _headers(self) -> dict[str, str]:
        if not self._ua:
            raise UpstreamDown("EDGAR_USER_AGENT 未配置（SEC 要求带联系方式的 UA）")
        return {"User-Agent": self._ua, "Accept-Encoding": "identity"}

    def probe(self) -> ProbeResult:
        try:
            r = self._t.request("GET", f"{_BASE_DATA}/api/xbrl/frames/us-gaap/Assets/USD/CY2025Q4I.json",
                                headers=self._headers(), timeout=10)
            return ProbeResult(adapter=self.name, ok=r.status == 200, authenticated=True,
                               error="" if r.status == 200 else f"HTTP {r.status}")
        except UpstreamDown as e:
            return ProbeResult(adapter=self.name, ok=False, error=str(e))

    def fetch(self, request: dict) -> list[dict]:
        kind = request["kind"]
        if kind == "companyfacts":
            return self._companyfacts(request["cik"])
        if kind == "submissions":
            return self._submissions(request["cik"], request.get("is_fpi", False))
        raise ValueError(f"EDGAR 不支持的请求类型：{kind}")

    def _companyfacts(self, cik: str) -> list[dict]:
        cik10 = str(int(cik)).zfill(10)
        r = self._t.request("GET", f"{_BASE_DATA}/api/xbrl/companyfacts/CIK{cik10}.json",
                            headers=self._headers())
        if r.status != 200:
            raise UpstreamDown(f"companyfacts CIK{cik10}: HTTP {r.status}")
        doc = r.json()
        out: list[dict] = []
        for taxonomy, concepts in doc.get("facts", {}).items():
            for concept, payload in concepts.items():
                for unit, facts in payload.get("units", {}).items():
                    for f in facts:
                        out.append({
                            "field": f"{taxonomy}:{concept}", "unit": unit,
                            "value": f.get("val"), "period_end": f.get("end"),
                            "fy": f.get("fy"), "fp": f.get("fp"), "form": f.get("form"),
                            "filed": f.get("filed"), "frame": f.get("frame"),
                        })
        return out

    def _submissions(self, cik: str, is_fpi: bool) -> list[dict]:
        cik10 = str(int(cik)).zfill(10)
        r = self._t.request("GET", f"{_BASE_DATA}/submissions/CIK{cik10}.json", headers=self._headers())
        if r.status != 200:
            raise UpstreamDown(f"submissions CIK{cik10}: HTTP {r.status}")
        recent = r.json().get("filings", {}).get("recent", {})
        out: list[dict] = []
        for form, date_str, accession in zip(
            recent.get("form", []), recent.get("filingDate", []),
            recent.get("accessionNumber", []), strict=False,
        ):
            if form not in WATCHED_FORMS:
                continue
            entry = {"form": form, "filed": date_str, "accession": accession}
            if form == "4" and is_fpi:
                filed = _dt.date.fromisoformat(date_str)
                if filed < HFIAA_EFFECTIVE:
                    # 豁免期合法无数据（R1.6）：标注对象而非 Gap
                    entry["hfiaa_exempt_period"] = True
            out.append(entry)
        return out


def hfiaa_coverage_note(query_start: _dt.date, is_fpi: bool) -> str | None:
    """FPI Form 4 采集区间口径（R1.6）。"""
    if is_fpi and query_start < HFIAA_EFFECTIVE:
        return f"FPI Form 4 自 {HFIAA_EFFECTIVE.isoformat()} 起可得（HFIAA）；此前区间为豁免期无数据"
    return None
