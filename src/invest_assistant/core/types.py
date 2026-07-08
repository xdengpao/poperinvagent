"""核心类型：Gap（缺口对象）与 Result（A2 的类型层落法）。

计算节点的输入/输出统一用 ``Result``：要么携带值，要么携带 Gap。
任何节点收到 Gap 只能向下传播 Gap 或执行规则库登记的降级动作——
不存在把 Gap 变成插值数字的构造路径（需求 R2.6，铁律 A2）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class Freshness(StrEnum):
    """新鲜度标签（R2.1）。STALE 在判定中视同缺失。"""

    FRESH = "fresh"
    STALE = "stale"


class Confidence(StrEnum):
    """证据置信标签——由校验器置位，禁止人工/代理直写（A1）。"""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Gap:
    """缺口对象（R2.6 / FR-D-05）：字段、标的/行业、尝试路径、降级动作。"""

    field_name: str
    subject: str
    attempted_paths: tuple[str, ...] = ()
    degradation: str = ""

    def child(self, note: str) -> Gap:
        return Gap(self.field_name, self.subject, self.attempted_paths, f"{self.degradation};{note}")


@dataclass(frozen=True)
class Result(Generic[T]):
    """Either[Gap, T]。用 ``ok()``/``gap()`` 构造，用 ``value``/``missing`` 消费。"""

    _value: T | None = None
    _gap: Gap | None = None

    @staticmethod
    def ok(value: T) -> Result[T]:
        return Result(_value=value)

    @staticmethod
    def gap(g: Gap) -> Result[T]:
        return Result(_gap=g)

    @property
    def missing(self) -> bool:
        return self._gap is not None

    @property
    def value(self) -> T:
        if self._gap is not None:
            raise LookupError(f"A2: 读取缺失值被拒绝（{self._gap.field_name}/{self._gap.subject}）")
        assert self._value is not None or self._value == 0 or self._value is False or self._value == ""
        return self._value  # type: ignore[return-value]

    @property
    def gap_obj(self) -> Gap:
        if self._gap is None:
            raise LookupError("非缺失结果没有 Gap")
        return self._gap


@dataclass(frozen=True)
class Evidence:
    """证据对象（最小 P0 形态）：值 + 来源 + 口径 + 新鲜度 + 置信。"""

    evidence_id: str
    subject: str
    field_name: str
    value: object
    unit: str = ""
    source: str = ""
    as_of: str = ""  # ISO 日期
    freshness: Freshness = Freshness.FRESH
    confidence: Confidence = Confidence.VERIFIED
    approx: bool = False  # ≈ 近似口径标（G 级降级）
    provenance: str = ""


@dataclass
class TraceRef:
    """判定三元组（A4）：规则条目 + 快照 + 证据集。"""

    rule_ids: list[str] = field(default_factory=list)
    snapshot_id: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    def assert_complete(self) -> None:
        if not self.rule_ids or not self.snapshot_id:
            raise ValueError("A4: 判定产物缺少规则/快照引用")
