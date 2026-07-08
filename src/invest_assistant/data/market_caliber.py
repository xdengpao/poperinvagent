"""非美市场披露口径框架（P3 任务 25，需求 R8.3；设计方案 O-03）。

B 组（非美上市）标的在对应市场披露源接入前缓评。本模块登记各市场披露口径
差异（财报/现金流频率、否决项子标准适用性、内部人数据源），并据"哪些市场
披露源已接入"判定哪些 B 组标的可解锁进入 S3。

口径差异直接影响否决项计算：
- 半年报市场（部分日/欧）：框架"连续 8 季"折算为连续 4 个半年期（同 ARM/TSM ADR 口径）；
- 质押率为 A 股特有字段，其余市场项 3 以内部人减持记录单独判定并标注口径差异；
- 内部人数据源随市场不同（美 Form 4、日 EDINET、韩 DART、台 MOPS、欧各国监管）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ReportFrequency(StrEnum):
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"


@dataclass(frozen=True)
class MarketCaliber:
    """一个市场的披露口径（O-03）。"""

    market: str                       # KR | JP | NL | TW | CH | HK | IL/US ...
    disclosure_source: str            # 披露源名（接入后填）
    report_frequency: ReportFrequency
    cashflow_frequency: ReportFrequency
    has_pledge_field: bool            # 质押率字段（A 股特有；其余 False）
    insider_source: str               # 内部人减持数据源
    source_connected: bool = False    # 该市场披露解析源是否已接入（M12 扩展）

    def consecutive_quarters_to_periods(self, quarters: int) -> tuple[int, str]:
        """框架"连续 N 季"在本市场口径的期数折算（半年报 → N/2 个半年期）。"""
        if self.cashflow_frequency is ReportFrequency.SEMIANNUAL:
            half = (quarters + 1) // 2
            return half, f"{quarters} 季 → {half} 个半年期（半年现金流折算，O-03 标注）"
        return quarters, f"{quarters} 季（季度口径）"

    def item3_mode(self) -> str:
        """否决项 3 口径：A 股走质押+减持"且"；其余走内部人减持单独判定。"""
        return "pledge_and_sales" if self.has_pledge_field else "insider_sales_only"


#: 各市场披露口径登记表（O-03；source_connected 初始为 False——待 M12 逐市场接入）
MARKET_CALIBERS: dict[str, MarketCaliber] = {
    "CN": MarketCaliber("CN", "巨潮/交易所+Tushare", ReportFrequency.QUARTERLY,
                        ReportFrequency.QUARTERLY, has_pledge_field=True,
                        insider_source="交易所股东增减持公告", source_connected=True),
    "US": MarketCaliber("US", "SEC EDGAR", ReportFrequency.QUARTERLY,
                        ReportFrequency.QUARTERLY, has_pledge_field=False,
                        insider_source="Form 4", source_connected=True),
    "KR": MarketCaliber("KR", "DART", ReportFrequency.QUARTERLY, ReportFrequency.QUARTERLY,
                        has_pledge_field=False, insider_source="DART 지분공시"),
    "JP": MarketCaliber("JP", "EDINET/TDnet", ReportFrequency.QUARTERLY, ReportFrequency.SEMIANNUAL,
                        has_pledge_field=False, insider_source="大量保有報告書（EDINET）"),
    "TW": MarketCaliber("TW", "公開資訊觀測站 MOPS", ReportFrequency.QUARTERLY,
                        ReportFrequency.QUARTERLY, has_pledge_field=False,
                        insider_source="MOPS 內部人持股異動"),
    "NL": MarketCaliber("NL", "AFM/年报", ReportFrequency.SEMIANNUAL, ReportFrequency.SEMIANNUAL,
                        has_pledge_field=False, insider_source="AFM 内部人交易登记"),
    "CH": MarketCaliber("CH", "SIX/年报", ReportFrequency.SEMIANNUAL, ReportFrequency.SEMIANNUAL,
                        has_pledge_field=False, insider_source="SIX 管理层交易披露"),
    "HK": MarketCaliber("HK", "披露易 HKEX", ReportFrequency.SEMIANNUAL, ReportFrequency.SEMIANNUAL,
                        has_pledge_field=False, insider_source="披露易权益披露"),
    "IL/US": MarketCaliber("IL/US", "SEC EDGAR（双重上市主体）", ReportFrequency.QUARTERLY,
                           ReportFrequency.QUARTERLY, has_pledge_field=False,
                           insider_source="Form 4（NASDAQ 主体）"),
}


@dataclass
class UnlockResult:
    unlocked: list[str] = field(default_factory=list)
    still_deferred: list[tuple[str, str]] = field(default_factory=list)  # (symbol, reason)


def classify_by_market(candidates: list[dict]) -> dict[str, list[str]]:
    """B 组候选按市场分组（键为 market，值为 symbol 列表）。"""
    out: dict[str, list[str]] = {}
    for c in candidates:
        out.setdefault(c["market"], []).append(c["symbol"])
    return out


def unlock_candidates(candidates: list[dict], calibers: dict[str, MarketCaliber] | None = None) -> UnlockResult:
    """据各市场披露源接入状态判定 B 组解锁（R8.3）。

    市场披露源已接入 → 该市场标的解锁进入 S3（以市场口径）；未接入 → 保持缓评
    并注明缺哪个市场的披露源（数据不足不推荐，A2）。
    """
    table = calibers or MARKET_CALIBERS
    result = UnlockResult()
    for c in candidates:
        market = c["market"]
        cal = table.get(market)
        if cal is None:
            result.still_deferred.append((c["symbol"], f"未登记市场口径：{market}（O-03 待扩展）"))
        elif cal.source_connected:
            result.unlocked.append(c["symbol"])
        else:
            result.still_deferred.append(
                (c["symbol"], f"{market} 披露源（{cal.disclosure_source}）未接入，缓评（A2/O-03）"))
    return result
