"""证据缓存与新鲜度复用（token 成本优化 L4，见 docs/token成本优化方案.md）。

新鲜度窗口内（R2.1：财务 ≤1 报告期、行情 ≤1 交易日、一致预期 ≤1 月）证据
跨期次复用——不重取、不重派代理。窗口 = 框架已定义的新鲜度窗口，窗口内重取
得到同一份披露，复用无损；过期照旧 STALE=缺失（A2 不放宽）。

慢变字段（审计师连任史、IPO/增发史、上市地等结构性事实）单列 365 天窗口
（G 级留痕：SLOW_CHANGING_FIELDS 注册表）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from ..core.types import Evidence
from .quality import _FRESHNESS_DAYS, FieldClass

#: 慢变字段注册表（G 级：365 天复用窗口，季度复盘可复核）
SLOW_CHANGING_FIELDS: frozenset[str] = frozenset({
    "auditor_tenure", "auditor_changes_5y", "ipo_history", "offering_history",
    "listing_market", "fiscal_year_end",
})
_SLOW_DAYS = 365


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    expired: int = 0

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses + self.expired
        return self.hits / total if total else None


@dataclass
class EvidenceCache:
    """按 (symbol, field_name) 缓存最新证据；新鲜窗口内命中即复用（L4）。

    生产实现由 evidence 表承载（append-only，取最新有效行）；本类为进程内
    读路径，语义与持久层一致。
    """

    _store: dict[tuple[str, str], Evidence] = field(default_factory=dict)
    stats: CacheStats = field(default_factory=CacheStats)

    def _window_days(self, field_name: str, field_class: FieldClass) -> int:
        if field_name in SLOW_CHANGING_FIELDS:
            return _SLOW_DAYS
        return _FRESHNESS_DAYS[field_class]

    def get(self, symbol: str, field_name: str, field_class: FieldClass,
            today: _dt.date) -> Evidence | None:
        """窗口内命中返回证据（免重取）；过期/未命中返回 None（走正常取证）。"""
        ev = self._store.get((symbol, field_name))
        if ev is None:
            self.stats.misses += 1
            return None
        age = (today - _dt.date.fromisoformat(ev.as_of)).days
        if age <= self._window_days(field_name, field_class):
            self.stats.hits += 1
            return ev
        self.stats.expired += 1  # 过期=STALE=缺失（R2.1，不放宽）
        return None

    def put(self, ev: Evidence) -> None:
        self._store[(ev.subject, ev.field_name)] = ev
