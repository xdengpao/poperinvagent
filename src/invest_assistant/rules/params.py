"""参数管理（任务 8.3，需求 R5.2）。

取值链：生效覆盖参数（param_override，带有效期）→ 签署值 → 附录 E 默认值。
修改校验：只可更严（方向登记在参数定义上）；留痕；不溯及；仅在无未决交易时生效。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import StrEnum


class Direction(StrEnum):
    """"更严"方向：判定线只升、仓位上限只降、现金下限只升、红线不可调。"""

    UP_ONLY = "up_only"
    DOWN_ONLY = "down_only"
    FIXED = "fixed"


@dataclass(frozen=True)
class ParamDef:
    name: str
    default: float
    direction: Direction
    source: str  # 框架出处
    unit: str = ""


@dataclass
class Override:
    """临时覆盖参数（R14.6/R14.8/R15.2 产生），带有效期与来源规则。"""

    name: str
    value: float
    valid_from: _dt.date
    valid_to: _dt.date | None
    origin_rule: str


@dataclass
class ParamChange:
    name: str
    old: float
    new: float
    signed_at: _dt.date
    note: str = ""


class StricterOnlyViolation(ValueError):
    pass


@dataclass
class ParamStore:
    """P0 内存实现；持久化实现挂 param_signed / param_override 表（同一接口）。"""

    defs: dict[str, ParamDef]
    signed: dict[str, float] = field(default_factory=dict)
    overrides: list[Override] = field(default_factory=list)
    changelog: list[ParamChange] = field(default_factory=list)

    def resolve(self, name: str, on: _dt.date) -> float:
        """取值链：生效覆盖 → 签署值 → 默认值（R5.2）。多覆盖取最严。"""
        d = self._def(name)
        candidates = [
            o.value
            for o in self.overrides
            if o.name == name and o.valid_from <= on and (o.valid_to is None or on <= o.valid_to)
        ]
        base = self.signed.get(name, d.default)
        if not candidates:
            return base
        if d.direction is Direction.UP_ONLY:
            return max([base, *candidates])
        if d.direction is Direction.DOWN_ONLY:
            return min([base, *candidates])
        raise StricterOnlyViolation(f"{name} 为红线参数，不接受覆盖")

    def sign(self, name: str, value: float, on: _dt.date, *, pending_trades: bool, note: str = "") -> None:
        """签署修改：方向校验 + 留痕 + 无未决交易方可生效（R5.2）。"""
        if pending_trades:
            raise StricterOnlyViolation(f"{name}: 存在未决交易，参数修改不生效（R5.2）")
        d = self._def(name)
        old = self.signed.get(name, d.default)
        if d.direction is Direction.FIXED and value != d.default:
            raise StricterOnlyViolation(f"{name} 为红线参数（{d.source}），不可调")
        if d.direction is Direction.UP_ONLY and value < old:
            raise StricterOnlyViolation(f"{name} 只可上调（{d.source}）：{old} → {value} 被拒")
        if d.direction is Direction.DOWN_ONLY and value > old:
            raise StricterOnlyViolation(f"{name} 只可下调（{d.source}）：{old} → {value} 被拒")
        self.signed[name] = value
        self.changelog.append(ParamChange(name, old, value, on, note))

    def add_override(self, o: Override) -> None:
        d = self._def(o.name)
        base = self.signed.get(o.name, d.default)
        if d.direction is Direction.UP_ONLY and o.value < base:
            raise StricterOnlyViolation(f"覆盖参数 {o.name} 方向违规（只可更严）")
        if d.direction is Direction.DOWN_ONLY and o.value > base:
            raise StricterOnlyViolation(f"覆盖参数 {o.name} 方向违规（只可更严）")
        if d.direction is Direction.FIXED:
            raise StricterOnlyViolation(f"{o.name} 为红线参数，不接受覆盖")
        self.overrides.append(o)

    def _def(self, name: str) -> ParamDef:
        if name not in self.defs:
            raise KeyError(f"未登记参数：{name}（规则库外无参数，FR-R-01）")
        return self.defs[name]
