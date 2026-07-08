"""冻结×动作放行矩阵（任务 13.3，需求 R6.3/R12.2，铁律 A5）。

A5（防守类动作不被冻结阻断）是横切不变量而非可配置规则：

- ``freeze_gate`` 的第一条分支即 ``DEFENSIVE → Allow``，写死在代码里
  （红线断言层，FR-R-01 允许的唯一硬编码位置，见 design §1 A5 行）；
- 防守类核心动作的**归类本身**同样固化在 ``DEFENSIVE_CORE``（模块级
  frozenset，不读任何配置）——否则改动作分类字典即可令 A5 失效；
- 分类字典只用于其余动作，且仅允许把动作改判为**更保守**类别（R6.3）。

本模块代码变更需双人复核（tasks.md 执行纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar


class ActionClass(StrEnum):
    """动作三分类（R6.3 冻结×动作放行矩阵的行坐标）。"""

    DEFENSIVE = "defensive"  # 防守类：清仓/减半/止损等，任何冻结不得阻断（A5）
    EXPANSIVE = "expansive"  # 扩权类：新开仓/加仓等
    NEUTRAL = "neutral"  # 中性：持有确认/报告输出等


# 保守序：值越大越保守；分类字典只允许沿该序向"更保守"方向改判（R6.3）
_CONSERVATISM: dict[ActionClass, int] = {
    ActionClass.EXPANSIVE: 0,
    ActionClass.NEUTRAL: 1,
    ActionClass.DEFENSIVE: 2,
}

# ————— 红线断言层（A5，不读任何配置）—————
# 框架 6.6 顺位前三级 + 回撤阶梯（R15.3）动作的 DEFENSIVE 归类硬编码。
DEFENSIVE_CORE: frozenset[str] = frozenset({
    "redline_disposal",  # 6.6 顺位 1：红线处置
    "falsified_liquidation",  # 6.6 顺位 2：证伪清仓（R13.4）
    "stop_loss_halve",  # 6.6 顺位 3：止损减半（R12.6）
    "drawdown_freeze_review",  # 回撤 -15%：冻结加仓 + 复盘（R15.3）
    "drawdown_deleverage",  # 回撤 -20%：降权益（R15.3）
    "drawdown_defensive_floor",  # 回撤 -30%：防守底仓 + 停手（R15.3）
})

# 扩权类动作规范名（全局冻结的阻断口径：新开仓 + 加仓，R6.3）
ACTION_NEW_OPEN = "new_position_open"
ACTION_ADD = "position_add"

# 非核心动作的缺省分类（可版本化字典的初始内容；核心动作绝不入典）
_DEFAULT_CLASSES: dict[str, ActionClass] = {
    ACTION_NEW_OPEN: ActionClass.EXPANSIVE,
    ACTION_ADD: ActionClass.EXPANSIVE,
    "hold_confirm": ActionClass.NEUTRAL,  # 月度持仓持有确认（R6.8）
    "report_output": ActionClass.NEUTRAL,
    "trim_position": ActionClass.DEFENSIVE,  # 一般减仓（非断言层核心，走字典）
    "staged_take_profit": ActionClass.DEFENSIVE,  # 分批兑现（R12.5）
}


class AssertionLayerViolation(Exception):
    """试图配置/改写红线断言层固化归类——A5 断言层不可配置，一律拒绝。"""


class ReclassifyRejected(Exception):
    """分类字典只可向更保守类别改判（R6.3），放宽方向的修改被拒。"""


@dataclass
class ActionClassifier:
    """可版本化的动作分类字典（design §4-5：配置即数据，随规则库版本化）。

    ``DEFENSIVE_CORE`` 内的动作**不经过本字典**：``classify`` 对其直接返回
    DEFENSIVE；构造与改判均拒绝携带核心动作（断言层不可配置，A5）。
    未登记动作按最受限的 EXPANSIVE 处理（保守缺省，R5.3 同级取严精神）。
    """

    version: str = "v1"
    _classes: dict[str, ActionClass] = field(default_factory=lambda: dict(_DEFAULT_CLASSES))
    changelog: list[tuple[str, ActionClass | None, ActionClass]] = field(default_factory=list)

    def __post_init__(self) -> None:
        poisoned = DEFENSIVE_CORE & self._classes.keys()
        if poisoned:
            raise AssertionLayerViolation(
                f"分类字典不得包含断言层核心动作：{sorted(poisoned)}（A5 不可配置）"
            )

    def classify(self, action: str) -> ActionClass:
        """动作→分类。核心防守动作走断言层硬编码，不读字典（A5）。"""
        if action in DEFENSIVE_CORE:
            return ActionClass.DEFENSIVE
        return self._classes.get(action, ActionClass.EXPANSIVE)

    def reclassify(self, action: str, new_class: ActionClass, note: str = "") -> None:
        """改判动作分类：仅允许更保守方向（R6.3），核心动作一律拒绝（A5）。"""
        if action in DEFENSIVE_CORE:
            raise AssertionLayerViolation(f"{action} 的归类固化于红线断言层，不可配置（A5）")
        current = self._classes.get(action)
        effective = current if current is not None else ActionClass.EXPANSIVE
        if _CONSERVATISM[new_class] < _CONSERVATISM[effective]:
            raise ReclassifyRejected(
                f"{action}: {effective.value} → {new_class.value} 为放宽方向，拒绝（R6.3 只可更保守）"
            )
        self._classes[action] = new_class
        self.changelog.append((action, current, new_class))


@dataclass
class FreezeFlags:
    """冻结标志位集合（R6.3），置位/清除全部留事件痕（design §4-4 事件溯源）。

    全局标志：复盘逾期 / 回撤 -15%（R15.3 第一档）/ 存在逾期未处理的防守类意见。
    标的标志：回执缺失（S9）/ 裁决未决（R5.3、R6.7）/ 48h 复评逾期（R12.2）。
    48h 复评逾期同时冻结该标的扩权类动作与组合级新开仓
    （R12.2 双冻结，经 ``global_new_open_frozen`` 派生，复评完成同时清除）。
    """

    GLOBAL_FLAGS: ClassVar[tuple[str, ...]] = (
        "review_overdue",
        "drawdown_15pct",
        "overdue_defensive_opinion",
    )
    INSTRUMENT_FLAGS: ClassVar[tuple[str, ...]] = (
        "receipt_missing",
        "ruling_pending",
        "reeval_48h_overdue",
    )

    review_overdue: bool = False
    drawdown_15pct: bool = False
    overdue_defensive_opinion: bool = False
    receipt_missing: set[str] = field(default_factory=set)
    ruling_pending: set[str] = field(default_factory=set)
    reeval_48h_overdue: set[str] = field(default_factory=set)
    events: list[dict[str, str]] = field(default_factory=list)  # 置位/清除留痕（A4）

    def _log(self, op: str, flag: str, subject: str = "", note: str = "") -> None:
        self.events.append({"op": op, "flag": flag, "subject": subject, "note": note})

    # —— 全局标志 ——
    def set_global(self, flag: str, note: str = "") -> None:
        if flag not in self.GLOBAL_FLAGS:
            raise ValueError(f"未知全局冻结标志：{flag}")
        setattr(self, flag, True)
        self._log("set", flag, note=note)

    def clear_global(self, flag: str, note: str = "") -> None:
        if flag not in self.GLOBAL_FLAGS:
            raise ValueError(f"未知全局冻结标志：{flag}")
        setattr(self, flag, False)
        self._log("clear", flag, note=note)

    # —— 标的标志 ——
    def set_instrument(self, flag: str, subject: str, note: str = "") -> None:
        if flag not in self.INSTRUMENT_FLAGS:
            raise ValueError(f"未知标的冻结标志：{flag}")
        getattr(self, flag).add(subject)
        self._log("set", flag, subject=subject, note=note)

    def clear_instrument(self, flag: str, subject: str, note: str = "") -> None:
        if flag not in self.INSTRUMENT_FLAGS:
            raise ValueError(f"未知标的冻结标志：{flag}")
        getattr(self, flag).discard(subject)
        self._log("clear", flag, subject=subject, note=note)

    def mark_reeval_48h_overdue(self, subject: str, note: str = "") -> None:
        """48h 复评逾期：标的冻结 + 组合级新开仓冻结同时生效（R12.2）。"""
        self.set_instrument("reeval_48h_overdue", subject, note=note or "48h 复评逾期，双冻结（R12.2）")

    def clear_reeval_48h_overdue(self, subject: str, note: str = "") -> None:
        """复评完成：同时解除标的冻结与组合级新开仓冻结（R12.2）。"""
        self.clear_instrument("reeval_48h_overdue", subject, note=note or "复评完成，双冻结解除（R12.2）")

    # —— 派生只读视图 ——
    @property
    def global_frozen(self) -> bool:
        """全局冻结生效：阻断新开仓与加仓（R6.3）。"""
        return self.review_overdue or self.drawdown_15pct or self.overdue_defensive_opinion

    @property
    def global_new_open_frozen(self) -> bool:
        """组合级新开仓冻结：全局冻结之外，任一标的 48h 复评逾期也触发（R12.2）。"""
        return self.global_frozen or bool(self.reeval_48h_overdue)

    def instrument_freeze_reasons(self, subject: str) -> tuple[str, ...]:
        return tuple(f for f in self.INSTRUMENT_FLAGS if subject in getattr(self, f))

    def instrument_frozen(self, subject: str) -> bool:
        return bool(self.instrument_freeze_reasons(subject))


@dataclass(frozen=True)
class GateDecision:
    """放行判定：结论 + 理由（拒绝留痕供违规台账/告警使用，R6.3/R18.3）。"""

    allowed: bool
    reason: str


def freeze_gate(
    action: str, action_class: ActionClass, flags: FreezeFlags, *, subject: str = ""
) -> GateDecision:
    """冻结×动作放行矩阵纯函数（R6.3）。

    第一条分支为 A5 短路：``DEFENSIVE → Allow``，位于一切冻结检查之前，
    不读配置、不可禁用（红线断言层）。R20.5"冻结期止损放行"回归用例锚定此分支。

    - 全局冻结（复盘逾期/回撤 -15%/逾期未处理防守类意见）阻断新开仓与加仓
      （当前扩权类动作全集；其余扩权类动作按同级取严一并阻断，R5.3）；
    - 标的冻结（回执缺失/裁决未决/48h 复评逾期）阻断该标的全部扩权类动作；
    - 48h 复评逾期额外触发组合级新开仓冻结（R12.2）；
    - 中性动作不在阻断范围。
    """
    if action_class == ActionClass.DEFENSIVE:  # A5：防守类动作不被任何冻结阻断（第一短路分支）
        return GateDecision(True, "A5: 防守类动作不受冻结阻断（红线断言层短路）")
    if action_class == ActionClass.NEUTRAL:
        return GateDecision(True, "中性动作不在冻结阻断范围（R6.3）")
    # 以下均为扩权类
    if subject:
        reasons = flags.instrument_freeze_reasons(subject)
        if reasons:
            return GateDecision(
                False, f"标的冻结阻断扩权类动作：{subject} [{', '.join(reasons)}]（R6.3）"
            )
    if flags.global_frozen:
        return GateDecision(False, "全局冻结阻断新开仓与加仓（R6.3；扩权类同级取严一并阻断）")
    if action == ACTION_NEW_OPEN and flags.global_new_open_frozen:
        return GateDecision(False, "存在 48h 复评逾期标的：组合级新开仓冻结（R12.2）")
    return GateDecision(True, "无生效冻结标志")
