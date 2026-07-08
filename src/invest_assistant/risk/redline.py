"""红线断言层（任务 18.2，需求 R15.1 红线部分）。

防御纵深：单票 ≤25% 与杠杆=0 是**独立于规则库与参数系统的硬编码断言**——
即使 risk_limits.yaml/params 被误改或被覆盖参数绕过，本层仍拦截（铁律：
红线不可配置）。规则库中另有 RISK.REDLINE 条目仅供追溯，判定不依赖其可变值。
"""

from __future__ import annotations

# 硬编码红线（框架附录 D-3/D-1，不可配置——改这两个常量需双人复核仓库保护）
_SINGLE_POSITION_REDLINE_PCT = 25.0
_LEVERAGE_MAX = 0.0


class RedlineViolation(Exception):
    """红线触碰：单票 >25% 或杠杆 >0。由风控引擎与 S8 守卫在放行前调用。"""


def assert_single_position(symbol: str, weight_pct: float) -> None:
    """单票绝对红线（≤组合净值 25%，不可调）。"""
    if weight_pct > _SINGLE_POSITION_REDLINE_PCT:
        raise RedlineViolation(
            f"红线触碰：{symbol} 仓位 {weight_pct:.2f}% > {_SINGLE_POSITION_REDLINE_PCT}%（附录 D-3，不可配置）"
        )


def assert_leverage(leverage: float) -> None:
    """杠杆红线（=0，不可调）。"""
    if leverage > _LEVERAGE_MAX:
        raise RedlineViolation(f"红线触碰：杠杆 {leverage} > {_LEVERAGE_MAX}（附录 D-1，不可配置）")


def check_redlines(*, positions: dict[str, float], leverage: float = 0.0) -> None:
    """组合级红线扫描：任一触碰即抛 RedlineViolation（在任何放行动作前调用）。"""
    assert_leverage(leverage)
    for symbol, weight in positions.items():
        assert_single_position(symbol, weight)


def single_position_redline_pct() -> float:
    """供展示/对账；判定仍走硬编码断言，不读此返回值改写行为。"""
    return _SINGLE_POSITION_REDLINE_PCT
