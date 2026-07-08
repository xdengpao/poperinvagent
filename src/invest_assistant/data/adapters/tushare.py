"""ADP-TUSHARE：A 股结构化主源（任务 4.3，签署 O-01 决策；D1/D2/D4/D5 + D3 条件位）。

- probe：逐接口探测 token 积分权限并登记覆盖率（R1.3），登记结果纳入决策 1 季度复议；
- fetch：通用 (api_name, params, fields) 透传，限速退避由传输层承担。
"""

from __future__ import annotations

import json
import os

from .base import BaseAdapter, ProbeResult, UpstreamDown
from .http import HttpTransport, UrllibTransport

_API = "https://api.tushare.pro"

#: 权限覆盖率探测清单（R1.3）——含 D3 条件位 report_rc（决策 1 复议统计）
PROBE_INTERFACES = (
    "daily",          # D1 行情
    "fina_indicator",  # D2 财务指标
    "pledge_stat",    # D4 质押（否决项 3）
    "top10_floatholders",  # D4 股东
    "report_rc",      # D3 卖方盈利预测（高积分门槛）
    "index_dailybasic",    # D5 指数估值
    "margin",         # D8 两融（市况信号⑩）
)


class TushareAdapter(BaseAdapter):
    name = "ADP-TUSHARE"
    required_in_production = True

    def __init__(self, transport: HttpTransport | None = None, token: str | None = None):
        self._t = transport or UrllibTransport(self.rate_limit)
        self._token = token or os.environ.get("TUSHARE_TOKEN", "")

    def _call(self, api_name: str, params: dict, fields: str = "") -> dict:
        body = json.dumps({"api_name": api_name, "token": self._token,
                           "params": params, "fields": fields}).encode()
        r = self._t.request("POST", _API, data=body,
                            headers={"Content-Type": "application/json"})
        if r.status != 200:
            raise UpstreamDown(f"tushare {api_name}: HTTP {r.status}")
        doc = r.json()
        if doc.get("code") != 0:
            raise PermissionError(f"tushare {api_name}: {doc.get('msg', 'code!=0')}")
        return doc.get("data") or {}

    def probe(self) -> ProbeResult:
        if not self._token:
            return ProbeResult(adapter=self.name, ok=False, authenticated=False,
                               error="TUSHARE_TOKEN 未配置")
        coverage: dict[str, bool] = {}
        reachable = False
        for api in PROBE_INTERFACES:
            try:
                self._call(api, {"limit": 1})
                coverage[api] = True
                reachable = True
            except PermissionError:
                coverage[api] = False  # 网关可达但积分权限不足 → 登记覆盖率
                reachable = True
            except UpstreamDown:
                coverage[api] = False
        core_ok = reachable and coverage.get("daily", False) and coverage.get("fina_indicator", False)
        return ProbeResult(adapter=self.name, ok=core_ok, authenticated=reachable,
                           coverage=coverage,
                           error="" if core_ok else "核心接口不可用或权限不足")

    def fetch(self, request: dict) -> list[dict]:
        """request: {api_name, params?, fields?} → 行记录列表（fields×items 对齐）。"""
        data = self._call(request["api_name"], request.get("params", {}), request.get("fields", ""))
        cols, rows = data.get("fields", []), data.get("items", [])
        return [dict(zip(cols, row, strict=False)) for row in rows]
