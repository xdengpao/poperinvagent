"""S4 分数上界剪枝（token 成本优化 L2，见 docs/token成本优化方案.md）。

量化维（六维，机械零成本）先算 → 上界 = 量化分 + 定性维（护城河 15 + 管理层 10）
满分。**上界 < 试探线（签署 65，只可上调）→ 定性维取证必然不改变判定（放弃），
跳过派发**——branch-and-bound，与全量取证的最终判定恒等，无损。

同理，三情景熊市拦截（R10.3）可在定性取证前先算：熊市 <-30% 的标的无论总分
不得给出买入意见，定性维取证只需在其进入持仓复评语境时补做。

剪枝决策全部留痕（A4）；被剪标的判定归档为 discard（上界不达线）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from ..rules.engine import RuleRepository
from ..rules.params import ParamStore


@dataclass(frozen=True)
class BoundsDecision:
    symbol: str
    quant_total: float
    upper_bound: float          # 量化分 + 定性维满分
    trial_line: float
    dispatch_qualitative: bool  # False=剪枝：定性取证不可能改变判定
    bear_intercepted: bool = False
    note: str = ""


def qualitative_max(rules: RuleRepository) -> float:
    """定性维满分合计（从 S4.DIMENSIONS 注册表取，非硬编码）。"""
    dims = rules.get("S4.DIMENSIONS").params["dimensions"]
    return sum(spec["max"] for spec in dims.values() if spec["kind"] == "qual")


def decide_qualitative_dispatch(
    symbol: str,
    quant_scores: dict[str, float],
    rules: RuleRepository,
    params: ParamStore,
    *,
    bear_return: float | None = None,
    on: _dt.date | None = None,
) -> BoundsDecision:
    """定性维取证派发决策（L2 上界剪枝）。

    - 上界 < 试探线 → 不派发（判定必然 discard，取证无增益）；
    - 熊市 <-30% 且非持仓复评语境 → 可延迟（买入意见已被拦截）；
      此处只标注 bear_intercepted，是否延迟由编排层按语境决定（保守：仍派发）。
    """
    on = on or _dt.date.today()
    dims = rules.get("S4.DIMENSIONS").params["dimensions"]
    quant_dims = {k for k, v in dims.items() if v["kind"] == "quant"}
    unknown = set(quant_scores) - quant_dims
    if unknown:
        raise KeyError(f"非量化维分数：{sorted(unknown)}")

    quant_total = sum(quant_scores.values())
    upper = quant_total + qualitative_max(rules)
    trial = params.resolve("score.trial_line", on)

    bear_rule = rules.get("S4.SCENARIO.LINES")
    intercepted = bear_return is not None and bear_return < float(bear_rule.params["bear_min_return"])

    if upper < trial:
        return BoundsDecision(
            symbol=symbol, quant_total=quant_total, upper_bound=upper, trial_line=trial,
            dispatch_qualitative=False, bear_intercepted=intercepted,
            note=f"上界 {upper:g} < 试探线 {trial:g}：判定必然为放弃，定性取证跳过（L2 剪枝留痕）",
        )
    return BoundsDecision(
        symbol=symbol, quant_total=quant_total, upper_bound=upper, trial_line=trial,
        dispatch_qualitative=True, bear_intercepted=intercepted,
        note="上界达线，派发定性维取证" + ("；熊市拦截已成立（买入类意见被拦）" if intercepted else ""),
    )
