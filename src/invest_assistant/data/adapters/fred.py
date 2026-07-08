"""ADP-FRED：宏观与流动性（任务 4.5，D8 域——DFII10 实际利率、信用利差等）。"""

from __future__ import annotations

import os

from .base import BaseAdapter, ProbeResult, UpstreamDown
from .http import HttpTransport, UrllibTransport, urlencode

_BASE = "https://api.stlouisfed.org/fred"


class FredAdapter(BaseAdapter):
    name = "ADP-FRED"
    required_in_production = True

    def __init__(self, transport: HttpTransport | None = None, api_key: str | None = None):
        self._t = transport or UrllibTransport(self.rate_limit)
        self._key = api_key or os.environ.get("FRED_API_KEY", "")

    def probe(self) -> ProbeResult:
        if not self._key:
            return ProbeResult(adapter=self.name, ok=False, authenticated=False,
                               error="FRED_API_KEY 未配置")
        try:
            r = self._t.request("GET", f"{_BASE}/series?" + urlencode(
                {"series_id": "DFII10", "api_key": self._key, "file_type": "json"}), timeout=10)
            return ProbeResult(adapter=self.name, ok=r.status == 200, authenticated=r.status != 403,
                               error="" if r.status == 200 else f"HTTP {r.status}")
        except UpstreamDown as e:
            return ProbeResult(adapter=self.name, ok=False, error=str(e))

    def fetch(self, request: dict) -> list[dict]:
        """request: {series_id, start?, end?} → 观测值列表；FRED 缺值符号 '.' 显式跳过（上层记 Gap）。"""
        params = {"series_id": request["series_id"], "api_key": self._key, "file_type": "json"}
        if request.get("start"):
            params["observation_start"] = request["start"]
        if request.get("end"):
            params["observation_end"] = request["end"]
        r = self._t.request("GET", f"{_BASE}/series/observations?" + urlencode(params))
        if r.status != 200:
            raise UpstreamDown(f"FRED {request['series_id']}: HTTP {r.status}")
        return [
            {"date": o["date"], "value": float(o["value"])}
            for o in r.json().get("observations", [])
            if o.get("value") not in (".", "", None)
        ]
