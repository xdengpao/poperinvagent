"""S1 六因子行业闸门引擎（任务 9.1/9.2，需求 R7）。

输入：行业×因子证据（档位 + 近似标 + 缺口标）+ 裁决集 + 规则库；
输出：industry_score（带 A4 三元组）+ 入围/敏感性判定。
只读快照数据；缺证据计 0 并产 Gap（S1.FACTOR.GAP_ZERO）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.types import Gap
from ..data.quality import GapLedger
from ..rules.engine import RuleRepository


@dataclass(frozen=True)
class FactorEvidence:
    industry: str
    factor: str
    grade: int
    approx: bool = False
    gap: bool = False
    evidence_id: str = ""


@dataclass(frozen=True)
class Ruling:
    ruling_id: str
    industry_scope: tuple[str, ...]
    factor: str
    grade_from: int
    grade_to: int


@dataclass
class IndustryScore:
    industry: str
    total: int
    factor_grades: dict[str, int]
    qualified: bool
    pending_ruling: bool  # 近似分敏感性 → 待裁决（R7.3）
    applied_rulings: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    snapshot_id: str = ""
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class S1Result:
    scores: list[IndustryScore]
    qualified: list[str]
    gaps: list[Gap]


class S1Engine:
    def __init__(self, rules: RuleRepository, gap_ledger: GapLedger | None = None):
        self._rules = rules
        self._gaps = gap_ledger or GapLedger()

    def run(
        self,
        evidence: list[FactorEvidence],
        rulings: list[Ruling],
        snapshot_id: str,
    ) -> S1Result:
        gate = self._rules.get("S1.GATE.LINE")
        gap_rule = self._rules.get("S1.FACTOR.GAP_ZERO")
        approx_rule = self._rules.get("S1.APPROX.SENSITIVITY")
        rng = self._rules.get("S1.FACTOR.GRADE_RANGE")
        line = int(gate.params["gate_line"])
        top_n = int(gate.params["top_n"])

        by_industry: dict[str, list[FactorEvidence]] = {}
        for ev in evidence:
            by_industry.setdefault(ev.industry, []).append(ev)

        scores: list[IndustryScore] = []
        for industry, evs in by_industry.items():
            if len(evs) != int(rng.params["factor_count"]):
                raise ValueError(f"{industry}: 因子数 {len(evs)} ≠ {rng.params['factor_count']}")
            grades: dict[str, int] = {}
            applied: list[str] = []
            eids: list[str] = []
            for ev in sorted(evs, key=lambda e: e.factor):
                if not (int(rng.params["min"]) <= ev.grade <= int(rng.params["max"])):
                    raise ValueError(f"{industry}/{ev.factor}: 档位越界 {ev.grade}")
                grade = ev.grade
                if ev.gap:
                    grade = int(gap_rule.params["on_gap"])  # 缺失计 0（R7.1）
                    self._gaps.register(Gap(ev.factor, industry, degradation="因子证据缺失计 0（F 3.1）"))
                for r in rulings:
                    if industry in r.industry_scope and r.factor == ev.factor and grade == r.grade_from:
                        grade = r.grade_to
                        applied.append(r.ruling_id)
                grades[ev.factor] = grade
                if ev.evidence_id:
                    eids.append(ev.evidence_id)
            total = sum(grades.values())

            # 近似口径敏感性（R7.3）：≈ 档位按 ±1 档不确定性计；
            # 依赖近似分压线 → 待裁决；本期已有裁决覆盖 → 视为已人工裁定。
            notches = int(approx_rule.params["approx_uncertainty_notches"])
            n_approx = sum(1 for ev in evs if ev.approx)
            sensitive = bool(
                approx_rule.params["approx_may_not_push_over_line"]
                and n_approx > 0
                and total >= line
                and (total - n_approx * notches) < line
            )
            if sensitive and approx_rule.params["ruling_clears_sensitivity"] and applied:
                sensitive = False
            scores.append(
                IndustryScore(
                    industry=industry,
                    total=total,
                    factor_grades=grades,
                    qualified=False,  # 统一在排名阶段判定
                    pending_ruling=sensitive,
                    applied_rulings=applied,
                    rule_ids=[gate.rule_id, gap_rule.rule_id, approx_rule.rule_id],
                    snapshot_id=snapshot_id,
                    evidence_ids=eids,
                )
            )

        # 入围：总分 ≥ 线 且 前 N；达标不足不凑数（R7.2）；待裁决不入围
        ranked = sorted(scores, key=lambda s: s.total, reverse=True)
        qualified: list[str] = []
        for s in ranked:
            if s.total >= line and not s.pending_ruling and len(qualified) < top_n:
                s.qualified = True
                qualified.append(s.industry)
        return S1Result(scores=ranked, qualified=qualified, gaps=list(self._gaps.gaps))
