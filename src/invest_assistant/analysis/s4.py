"""S4 八维评分引擎与三情景（任务 12，需求 R10）。

- 量化维自动计分（档位+档内分值须落在锚点区间）；定性维（护城河/管理层）
  代理初评 → 挂起待签认，未签认分不参与判定线运算（R10.1）；
- 任一维缺失 → 低档下限 + 标注；缺失 ≥2 维 → 不得进入推荐（R10.2）；
- 三情景：基准 ≥+100% 校验、熊市 <-30% 一票拦截、PEG 交叉验证（R10.3）；
- 管理层维装配器：S3 记录注入 + 指引兑现率（冷启动中档下限，R2.8/R10.5）；
- 季度重打分跌档处置指令（R10.4，出处=框架 3.4）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from ..core.types import Gap
from ..rules.engine import RuleRepository
from ..rules.params import ParamStore


@dataclass(frozen=True)
class DimensionInput:
    name: str
    band: str | None = None  # high | mid | low；None=证据缺失
    points: float | None = None  # 档内分值（须落在锚点区间）
    gap: bool = False
    evidence_ids: tuple[str, ...] = ()
    agent_draft: bool = False  # 定性维代理初评标记


@dataclass
class DimensionScore:
    name: str
    points: float
    band: str
    kind: str
    counted: bool  # 是否计入判定线（定性维未签认=False）
    gap: bool = False
    note: str = ""
    evidence_ids: tuple[str, ...] = ()


@dataclass
class ScoreCard:
    symbol: str
    dimensions: list[DimensionScore]
    total_counted: float
    pending_signoff: list[str]
    gaps: list[Gap]
    eligible_for_reco: bool
    judgement: str  # heavy | trial | discard | blocked_pending_signoff | blocked_gaps
    rule_ids: list[str] = field(default_factory=list)
    snapshot_id: str = ""


@dataclass
class ScenarioResult:
    bear: float
    base: float
    bull: float
    base_line_ok: bool
    bear_intercept: bool  # True=熊市 <-30%，无论总分不得给出买入意见
    peg_cross_check_ok: bool | None = None
    rule_ids: list[str] = field(default_factory=list)


class S4Engine:
    def __init__(self, rules: RuleRepository, params: ParamStore):
        self._rules = rules
        self._params = params

    # ---------- 评分 ----------

    def score(self, symbol: str, inputs: list[DimensionInput], *, signed_off: set[str],
              snapshot_id: str = "", on: _dt.date | None = None) -> ScoreCard:
        dims_rule = self._rules.get("S4.DIMENSIONS")
        gap_rule = self._rules.get("S4.GAP.LOWFLOOR")
        judge_rule = self._rules.get("S4.JUDGE.LINES")
        registry: dict[str, dict] = dims_rule.params["dimensions"]

        provided = {i.name for i in inputs}
        missing_dims = set(registry) - provided
        if missing_dims:
            raise ValueError(f"{symbol}: 缺维度输入 {sorted(missing_dims)}（八维必须全量提交，缺证据用 gap 标记）")

        scores: list[DimensionScore] = []
        gaps: list[Gap] = []
        pending: list[str] = []
        for inp in inputs:
            spec = registry[inp.name]
            bands: dict[str, list[float]] = spec["bands"]
            if inp.gap or inp.band is None:
                floor = bands["low"][0]  # 低档下限（R10.2）
                scores.append(DimensionScore(inp.name, floor, "low", spec["kind"], counted=True,
                                             gap=True, note="证据缺失 → 低档下限（F 3.3）"))
                gaps.append(Gap(inp.name, symbol, degradation="维度证据缺失计低档下限"))
                continue
            lo, hi = bands[inp.band]
            pts = inp.points if inp.points is not None else lo
            if not (lo <= pts <= hi):
                raise ValueError(f"{symbol}/{inp.name}: 分值 {pts} 不在 {inp.band} 档区间 [{lo},{hi}]")
            counted = True
            note = ""
            if spec["kind"] == "qual" and inp.name not in signed_off:
                counted = False  # 未签认定性分不参与判定线（R10.1）
                pending.append(inp.name)
                note = "定性维初评待签认"
            scores.append(DimensionScore(inp.name, pts, inp.band, spec["kind"], counted,
                                         note=note, evidence_ids=inp.evidence_ids))

        total_counted = sum(s.points for s in scores if s.counted)
        n_gaps = sum(1 for s in scores if s.gap)
        eligible = n_gaps <= int(gap_rule.params["max_gaps_for_reco"])

        on = on or _dt.date.today()
        heavy = self._params.resolve(str(judge_rule.params["heavy_param"]), on)
        trial = self._params.resolve(str(judge_rule.params["trial_param"]), on)
        if pending:
            judgement = "blocked_pending_signoff"  # 挂起：签认前不得进入判定（规格书 §5.1 S4）
        elif not eligible:
            judgement = "blocked_gaps"  # 缺失 ≥2 维不得进入推荐（R10.2）
        elif total_counted >= heavy:
            judgement = "heavy"
        elif total_counted >= trial:
            judgement = "trial"
        else:
            judgement = "discard"
        return ScoreCard(symbol, scores, total_counted, pending, gaps, eligible, judgement,
                         rule_ids=[dims_rule.rule_id, gap_rule.rule_id, judge_rule.rule_id],
                         snapshot_id=snapshot_id)

    # ---------- 三情景（R10.3） ----------

    def scenarios(self, bear: float, base: float, bull: float, peg: float | None = None) -> ScenarioResult:
        rule = self._rules.get("S4.SCENARIO.LINES")
        p = rule.params
        return ScenarioResult(
            bear=bear, base=base, bull=bull,
            base_line_ok=base >= float(p["base_min_return"]),
            bear_intercept=bear < float(p["bear_min_return"]),
            peg_cross_check_ok=None if peg is None else peg < float(p["peg_max_high_band"]),
            rule_ids=[rule.rule_id],
        )

    # ---------- 管理层维装配器（R10.5/R2.8） ----------

    def assemble_management(self, symbol: str, *, guidance_quarters: int,
                            hit_ratio: float | None, s3_records: list[str]) -> DimensionInput:
        rule = self._rules.get("S4.MGMT.ASSEMBLER")
        p = rule.params
        dims = self._rules.get("S4.DIMENSIONS").params["dimensions"]["管理层"]
        if guidance_quarters < int(p["cold_start_min_quarters"]):
            # 冷启动：中档下限 + 标签（签署决策 2，A2 唯一显式例外）
            return DimensionInput(name="管理层", band="mid", points=dims["bands"]["mid"][0],
                                  agent_draft=True,
                                  evidence_ids=tuple(s3_records) or ("guidance:cold_start",))
        if hit_ratio is None:
            return DimensionInput(name="管理层", gap=True)
        if hit_ratio >= float(p["hit_ratio_bands"]["high"]):
            band = "high"
        elif hit_ratio >= float(p["hit_ratio_bands"]["mid"]):
            band = "mid"
        else:
            band = "low"
        return DimensionInput(name="管理层", band=band, points=dims["bands"][band][0],
                              agent_draft=True, evidence_ids=tuple(s3_records))

    # ---------- 季度重打分跌档处置（R10.4） ----------

    def requalify_directive(self, card: ScoreCard, *, is_held: bool, on: _dt.date | None = None) -> str | None:
        if not is_held or card.judgement == "blocked_pending_signoff":
            return None
        rule = self._rules.get("S4.REQUALIFY.DOWNGRADE")
        judge_rule = self._rules.get("S4.JUDGE.LINES")
        on = on or _dt.date.today()
        heavy = self._params.resolve(str(judge_rule.params["heavy_param"]), on)
        trial = self._params.resolve(str(judge_rule.params["trial_param"]), on)
        if card.total_counted < trial:
            return str(rule.params["below_trial_action"])  # sell（F 3.4 → 第十章流程）
        if card.total_counted < heavy:
            return str(rule.params["to_trial_band_action"])  # reduce_to_trial_cap
        return None
