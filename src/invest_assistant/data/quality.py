"""数据质量控制管道（任务 5，需求 R2.1/R2.2/R2.3/R2.6）。

入库路径固定四段：原始记录 → 新鲜度打标 → 双源仲裁 → 归一化 → evidence/gap 落库。
本模块承载前两段与缺口协议；归一化见 normalize.py。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.types import Evidence, Freshness, Gap


class FieldClass(StrEnum):
    FINANCIAL = "financial"  # ≤1 报告期
    MARKET = "market"  # ≤1 交易日
    CONSENSUS = "consensus"  # ≤1 个月


#: 新鲜度窗口（R2.1）——自然日近似值；行情类由日历服务精确判定
_FRESHNESS_DAYS = {FieldClass.FINANCIAL: 100, FieldClass.MARKET: 4, FieldClass.CONSENSUS: 31}


def stamp_freshness(ev: Evidence, field_class: FieldClass, today: _dt.date) -> Evidence:
    """新鲜度打标：过期 = STALE = 判定中视同缺失（R2.1）。"""
    as_of = _dt.date.fromisoformat(ev.as_of)
    fresh = (today - as_of).days <= _FRESHNESS_DAYS[field_class]
    return Evidence(
        **{**ev.__dict__, "freshness": Freshness.FRESH if fresh else Freshness.STALE}
    )


class Conservative(StrEnum):
    """字段字典登记的保守方向：对判定最不利的一侧。"""

    HIGHER = "higher"  # 取高（如质押率）
    LOWER = "lower"  # 取低（如现金转化率）


@dataclass(frozen=True)
class FieldDictEntry:
    """字段字典条目（R2.2）：字段 → 保守方向（可按消费场景细分）。"""

    field_name: str
    direction: Conservative
    per_scenario: dict[str, Conservative] = field(default_factory=dict)

    def direction_for(self, scenario: str | None) -> Conservative:
        if scenario and scenario in self.per_scenario:
            return self.per_scenario[scenario]
        return self.direction


@dataclass
class Discrepancy:
    field_name: str
    subject: str
    values: dict[str, float]  # source → value
    chosen: float
    scenario: str | None = None


@dataclass
class ArbitrationOutcome:
    evidence: Evidence | None = None
    gap: Gap | None = None
    discrepancy: Discrepancy | None = None
    to_ruling_queue: bool = False


def arbitrate_numeric(
    a: Evidence, b: Evidence, entry: FieldDictEntry, scenario: str | None = None
) -> ArbitrationOutcome:
    """数值字段双源仲裁（R2.2）：任何分歧（无数值门槛）按保守方向取值 + 落分歧记录。"""
    va, vb = float(a.value), float(b.value)  # type: ignore[arg-type]
    if va == vb:
        return ArbitrationOutcome(evidence=a)
    direction = entry.direction_for(scenario)
    chosen_ev = (a if va > vb else b) if direction is Conservative.HIGHER else (a if va < vb else b)
    return ArbitrationOutcome(
        evidence=chosen_ev,
        discrepancy=Discrepancy(
            field_name=entry.field_name,
            subject=a.subject,
            values={a.source: va, b.source: vb},
            chosen=float(chosen_ev.value),  # type: ignore[arg-type]
            scenario=scenario,
        ),
    )


def arbitrate_categorical(a: Evidence, b: Evidence) -> ArbitrationOutcome:
    """分类/文本字段冲突：产 Gap + 裁决队列，禁止择一（R2.3）。"""
    if a.value == b.value:
        return ArbitrationOutcome(evidence=a)
    gap = Gap(
        field_name=a.field_name,
        subject=a.subject,
        attempted_paths=(a.source, b.source),
        degradation="分类字段双源冲突 → 裁决队列（R2.3）",
    )
    return ArbitrationOutcome(gap=gap, to_ruling_queue=True)


@dataclass
class GapLedger:
    """缺口台账：零静默 + 月度缺口率汇总（R2.6）。"""

    gaps: list[Gap] = field(default_factory=list)

    def register(self, gap: Gap) -> Gap:
        self.gaps.append(gap)
        return gap

    def monthly_rate(self, total_nodes: int) -> float:
        if total_nodes <= 0:
            raise ValueError("节点总数必须为正")
        return len(self.gaps) / total_nodes
