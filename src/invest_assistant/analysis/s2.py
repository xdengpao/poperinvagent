"""S2 候选扫描引擎（任务 9.3，需求 R8）。

三来源并集 → 主线归属过滤 → 颠覆名单核对 → 流动性门槛（双口径，代理期显式标注）
→ A/B/C 分组（B/C 缓评，不产生任何意见）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..rules.engine import RuleRepository


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str
    market: str  # US / CH / KR / JP / NL / TW / IL-US ...
    mainline: str | None
    sources: tuple[str, ...]
    data_scope: str  # US-full | non-US | A-share
    adv_usd_90d: float | None = None
    adv_cny_20d: float | None = None
    disruption_hit: bool = False
    exclusion_reason: str = ""


@dataclass
class PoolEntry:
    symbol: str
    group: str  # A | B | C
    deferred: bool
    liquidity_note: str = ""


@dataclass
class Exclusion:
    symbol: str
    reason_class: str  # mainline-mismatch | disruption | liquidity | no-source
    detail: str = ""


@dataclass
class S2Result:
    pool: list[PoolEntry]
    excluded: list[Exclusion]
    group: dict[str, list[str]] = field(default_factory=dict)


class S2Engine:
    def __init__(self, rules: RuleRepository):
        self._rules = rules

    def run(self, candidates: list[Candidate], qualified_industries: list[str],
            liquidity_proxy: str = "20d") -> S2Result:
        src_rule = self._rules.get("S2.SOURCES.UNION")
        liq = self._rules.get("S2.LIQUIDITY")
        disruption = self._rules.get("S2.DISRUPTION.REJECT")
        grouping = self._rules.get("S2.SCOPE.GROUPING")
        mainline_rule = self._rules.get("S2.MAINLINE.MATCH")

        if liquidity_proxy not in (liq.params["window"], liq.params["proxy_window_allowed"]):
            raise ValueError(f"流动性口径 {liquidity_proxy} 未获规则许可（R8.2）")
        proxy_note = (
            f"G 级降级：{liq.params['proxy_window_allowed']} 代理口径（框架规定 {liq.params['window']}）"
            if liquidity_proxy != liq.params["window"]
            else ""
        )

        pool: list[PoolEntry] = []
        excluded: list[Exclusion] = []
        groups: dict[str, list[str]] = {"A": [], "B": [], "C": []}

        for c in candidates:
            # 来源合法性（三来源并集，R8.1）：来源标记必须在并集内；
            # 来源证据缺失（空）对进入 S3 的 A 组是硬失败，对缓评组记缺口随组缓评（A2）。
            if not all(s == "seed" or s == "user" or s.startswith("graph") for s in c.sources):
                raise ValueError(f"{c.symbol}: 候选来源不在三来源并集内（{src_rule.rule_id}）")
            source_gap = not c.sources
            if source_gap and c.data_scope == "US-full":
                excluded.append(Exclusion(c.symbol, "no-source", "来源证据缺失（A2：无数据不入 S3）"))
                continue
            # 主线归属（S2 第 1 期排除口径）：
            # - 有归类判定且不在入围主线 → 排除（GOOGL 型）；
            # - 无归类判定但留有排除判定记录（边缘算力归属待重议，AMBA/MBLY 型）→ 排除留痕；
            # - 无归类判定且无排除记录（B/C 组披露源未接入）→ 不排除，随分组缓评（A2：数据不足不判定）。
            if mainline_rule.params["require_mainline_in_qualified"]:
                if c.mainline is not None and c.mainline not in qualified_industries:
                    excluded.append(Exclusion(c.symbol, "mainline-mismatch", c.exclusion_reason))
                    continue
                if c.mainline is None and c.exclusion_reason:
                    excluded.append(Exclusion(c.symbol, "mainline-mismatch", c.exclusion_reason))
                    continue
            # 被颠覆名单（R8.4）
            if disruption.params["reject_on_hit"] and c.disruption_hit:
                excluded.append(Exclusion(c.symbol, "disruption", c.exclusion_reason))
                continue
            # 分组（R8.3）
            group = next(g for g, scope in grouping.params["groups"].items() if scope == c.data_scope)
            deferred = group in grouping.params["deferred_groups"]
            # 流动性（仅对进入 S3 的 A 组强制美元口径；B/C 组待各自市场口径接入）
            if group == "A":
                if c.adv_usd_90d is None:
                    excluded.append(Exclusion(c.symbol, "liquidity", "成交额证据缺失（A2：无数据不入池）"))
                    continue
                if c.adv_usd_90d < float(liq.params["us_min_adv_usd"]):
                    excluded.append(Exclusion(c.symbol, "liquidity", f"90d ADV {c.adv_usd_90d} 低于门槛"))
                    continue
            pool.append(PoolEntry(c.symbol, group, deferred, proxy_note))
            groups[group].append(c.symbol)

        return S2Result(pool=pool, excluded=excluded, group=groups)
