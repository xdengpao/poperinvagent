"""其余适配器（任务 4.3-4.6）：AkShare 备源、一致预期降级源、行业指数、产业高频、IBKR。

AkShare/yfinance 为库依赖（可选 extras: sources），懒加载——未安装或无网时
probe 如实报不可用，不阻塞其他适配器。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os

from .base import BaseAdapter, ProbeResult, UpstreamDown
from .http import HttpTransport, UrllibTransport


class AkshareAdapter(BaseAdapter):
    """ADP-AKSHARE：A 股备源 + 市场广度原始数据（两市涨跌/新高新低家数）。"""

    name = "ADP-AKSHARE"
    required_in_production = True

    def probe(self) -> ProbeResult:
        try:
            import akshare  # noqa: F401  懒加载
        except ImportError:
            return ProbeResult(adapter=self.name, ok=False, error="akshare 未安装（extras: sources）")
        try:
            import akshare as ak

            df = ak.stock_zh_index_spot_em(symbol="上证系列指数")
            return ProbeResult(adapter=self.name, ok=len(df) > 0, authenticated=True)
        except Exception as e:  # noqa: BLE001 —— 上游异常类型不可枚举，如实落矩阵
            return ProbeResult(adapter=self.name, ok=False, error=f"上游不可达：{e}")

    def fetch(self, request: dict) -> list[dict]:
        import akshare as ak

        fn = getattr(ak, request["function"], None)
        if fn is None:
            raise ValueError(f"akshare 无此接口：{request['function']}")
        try:
            df = fn(**request.get("params", {}))
        except Exception as e:  # noqa: BLE001
            raise UpstreamDown(f"akshare {request['function']}: {e}") from e
        return df.to_dict("records")


class ConsensusAdapter(BaseAdapter):
    """ADP-CONSENSUS：D3 一致预期（yfinance 美股主源 + 东方财富 A 股降级源）。

    覆盖率逐期登记（签署决策 1 季度复议数据）；缺失按框架降级（低档/0 分）。
    """

    name = "ADP-CONSENSUS"
    required_in_production = True

    def __init__(self, transport: HttpTransport | None = None):
        self._t = transport or UrllibTransport(self.rate_limit)
        self.coverage_log: list[dict] = []  # 逐期覆盖率登记（R1.3/决策 1）

    def probe(self) -> ProbeResult:
        try:
            import yfinance  # noqa: F401
        except ImportError:
            return ProbeResult(adapter=self.name, ok=False, error="yfinance 未安装（extras: sources）")
        try:
            import yfinance as yf

            t = yf.Ticker("NVDA")
            ok = bool(t.fast_info)
            return ProbeResult(adapter=self.name, ok=ok, authenticated=True)
        except Exception as e:  # noqa: BLE001
            return ProbeResult(adapter=self.name, ok=False, error=f"上游不可达：{e}")

    def fetch(self, request: dict) -> list[dict]:
        import yfinance as yf

        symbol = request["symbol"]
        try:
            tk = yf.Ticker(symbol)
            targets = tk.analyst_price_targets or {}
            estimates = tk.earnings_estimate
            rows = [] if estimates is None else estimates.reset_index().to_dict("records")
        except Exception as e:  # noqa: BLE001
            raise UpstreamDown(f"yfinance {symbol}: {e}") from e
        covered = bool(rows or targets)
        self.coverage_log.append({"symbol": symbol, "covered": covered,
                                  "at": _dt.date.today().isoformat()})
        return [{"symbol": symbol, "price_targets": targets, "earnings_estimates": rows}]


class IndexAdapter(BaseAdapter):
    """ADP-INDEX：D5 行业指数与估值分位（申万经 Tushare/AkShare；美股行业 ETF 代理表）。"""

    name = "ADP-INDEX"
    required_in_production = True

    #: 美股行业 ETF 代理表（配置化，实跑验证：半导体→SOXX/SMH）
    ETF_PROXY: dict[str, list[str]] = {
        "半导体": ["SOXX", "SMH"],
        "软件": ["IGV"],
        "网络安全": ["CIBR", "HACK"],
        "云计算": ["SKYY", "WCLD"],
    }

    def __init__(self, tushare: BaseAdapter | None = None, akshare: BaseAdapter | None = None):
        self._tushare, self._akshare = tushare, akshare

    def probe(self) -> ProbeResult:
        deps_ok = self._tushare is not None and self._akshare is not None
        return ProbeResult(adapter=self.name, ok=deps_ok,
                           coverage={"etf_proxy_sectors": bool(self.ETF_PROXY)},
                           error="" if deps_ok else "依赖 Tushare/AkShare 适配器注入")

    def fetch(self, request: dict) -> list[dict]:
        kind = request["kind"]
        if kind == "sw_index" and self._tushare is not None:
            return self._tushare.fetch({"api_name": "index_dailybasic", "params": request.get("params", {})})
        if kind == "etf_proxy":
            return [{"sector": s, "etfs": e} for s, e in self.ETF_PROXY.items()]
        raise ValueError(f"INDEX 不支持：{kind}")


class IndustryAdapter(BaseAdapter):
    """ADP-INDUSTRY：D7 产业高频（台积电月报/云 capex/GPU 租赁价/推理 API 价）——页面快照归档。"""

    name = "ADP-INDUSTRY"
    required_in_production = True

    #: 月度抓取源清单（页面快照 + 内容哈希归档，evidence 存指针）
    SOURCES = {
        "tsmc_monthly_revenue": "https://pr.tsmc.com/english/latest-news",
        "gpu_rental_price": "https://cloud.google.com/compute/gpus-pricing",
    }

    def __init__(self, transport: HttpTransport | None = None):
        self._t = transport or UrllibTransport(self.rate_limit)

    def probe(self) -> ProbeResult:
        try:
            r = self._t.request("GET", next(iter(self.SOURCES.values())), timeout=10)
            return ProbeResult(adapter=self.name, ok=r.status == 200,
                               error="" if r.status == 200 else f"HTTP {r.status}")
        except UpstreamDown as e:
            return ProbeResult(adapter=self.name, ok=False, error=str(e))

    def fetch(self, request: dict) -> list[dict]:
        key = request["source"]
        url = self.SOURCES.get(key) or request.get("url")
        if not url:
            raise ValueError(f"INDUSTRY 未登记源：{key}")
        r = self._t.request("GET", url)
        if r.status != 200:
            raise UpstreamDown(f"{key}: HTTP {r.status}")
        return [{
            "source": key, "url": url,
            "content_hash": hashlib.sha256(r.body).hexdigest(),
            "snapshot_bytes": len(r.body),
            "fetched_at": _dt.date.today().isoformat(),
        }]


class IbkrAdapter(BaseAdapter):
    """ADP-IBKR：D1 行情/90 日均额/D6 主题图谱/前瞻财报日历。

    生产接入经 IBKR 网关（凭据只读，R19.4）；连接器未授权时如实报不可用——
    断连不阻塞非行情节点（R1.5，编排层冻结行情依赖节点）。
    """

    name = "ADP-IBKR"
    required_in_production = True

    def __init__(self, gateway_probe=None):
        self._gateway_probe = gateway_probe  # 生产注入网关探测；缺省=未配置

    def probe(self) -> ProbeResult:
        if self._gateway_probe is None:
            return ProbeResult(adapter=self.name, ok=False, authenticated=False,
                               error="IBKR 网关未配置/连接器未授权（需在 claude.ai 连接器设置重新授权）")
        try:
            ok = bool(self._gateway_probe())
            return ProbeResult(adapter=self.name, ok=ok, authenticated=ok)
        except Exception as e:  # noqa: BLE001
            return ProbeResult(adapter=self.name, ok=False, error=str(e))

    def fetch(self, request: dict) -> list[dict]:
        raise UpstreamDown("IBKR 网关不可用：行情依赖节点应被冻结（R1.5），重连后补拉缺失区间")


class CninfoAdapter(BaseAdapter):
    """ADP-CNINFO：巨潮 D2/D4 原文归档（任务 24 本体，R1.1/R8.3）。

    fetch(kind='announcements')：按股票代码+分类+日期范围查询公告列表，
    每条登记标题/披露时间/PDF 指针+内容哈希（PDF 正文归档为冷层指针，
    evidence 存指针；正文数字须回溯原文二次确认后方可用于否决/评分——G）。
    """

    name = "ADP-CNINFO"
    required_in_production = True
    _QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    _HOST = "http://static.cninfo.com.cn/"

    #: 巨潮公告分类码（D4 否决项证据相关）
    CATEGORY = {
        "audit": "category_shgqigd_szsh",     # 审计/年报口径
        "pledge": "category_gqbg_szsh",        # 股权变动/质押
        "placement": "category_zf_szsh",       # 增发/定增
        "periodic": "category_ndbg_szsh;category_bndbg_szsh",  # 年报/半年报
    }

    def __init__(self, transport: HttpTransport | None = None):
        self._t = transport or UrllibTransport(self.rate_limit)

    def probe(self) -> ProbeResult:
        try:
            r = self._t.request("GET", "http://www.cninfo.com.cn/new/index", timeout=10)
            return ProbeResult(adapter=self.name, ok=r.status == 200,
                               error="" if r.status == 200 else f"HTTP {r.status}")
        except UpstreamDown as e:
            return ProbeResult(adapter=self.name, ok=False, error=str(e))

    def fetch(self, request: dict) -> list[dict]:
        kind = request.get("kind", "announcements")
        if kind != "announcements":
            raise ValueError(f"CNINFO 不支持的请求类型：{kind}")
        from .http import urlencode

        params = {
            "stock": request["stock"],           # 如 "300308,gssz"（代码,板块）
            "tabName": "fulltext",
            "category": self.CATEGORY.get(request.get("category", "periodic"),
                                          request.get("category", "")),
            "seDate": request.get("date_range", ""),
            "pageSize": str(request.get("page_size", 30)),
            "pageNum": str(request.get("page_num", 1)),
        }
        r = self._t.request("POST", self._QUERY_URL,
                            data=urlencode(params).encode(),
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status != 200:
            raise UpstreamDown(f"CNINFO 公告查询 {request['stock']}: HTTP {r.status}")
        doc = r.json()
        out: list[dict] = []
        for ann in doc.get("announcements") or []:
            adjunct = ann.get("adjunctUrl", "")
            out.append({
                "title": ann.get("announcementTitle", ""),
                "announced_at": ann.get("announcementTime"),  # 毫秒时间戳
                "pdf_pointer": (self._HOST + adjunct) if adjunct else "",
                "sec_code": ann.get("secCode"),
                "content_hash": "",  # 正文归档后由归档器回填（PDF 下载在冷层任务）
                "caliber_note": "巨潮原文；关键数字须回溯 PDF 二次确认方可用于否决/评分（G）",
            })
        return out


def build_default_registry(transport: HttpTransport | None = None):
    """默认注册器：§2.3 全清单源（R1.1/R1.2）。"""
    from .base import AdapterRegistry
    from .edgar import EdgarAdapter
    from .fred import FredAdapter
    from .tushare import TushareAdapter

    reg = AdapterRegistry()
    tushare = TushareAdapter(transport)
    akshare = AkshareAdapter()
    for a in (
        EdgarAdapter(transport), FredAdapter(transport), tushare, akshare,
        ConsensusAdapter(transport), IndexAdapter(tushare, akshare),
        IndustryAdapter(transport), IbkrAdapter(gateway_probe=None), CninfoAdapter(transport),
    ):
        reg.register(a)
    return reg


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0", "false")
