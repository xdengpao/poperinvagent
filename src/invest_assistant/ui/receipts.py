"""成交回执录入（任务 14.6，需求 R6.3；规格书 §3.3 trade_record 补充表，R18.1 加严 append-only）。

- ``record_receipt(decision_record_id, ...)`` 落 trade_record 语义对象（S9 产物）；
- 缺回执检测：已录入且待回执的决策记录无对应 trade_record → 产出"标的冻结标志建议"
  （建议置 :class:`~invest_assistant.orchestration.freeze.FreezeFlags` 的
  ``receipt_missing`` 标的标志，阻断该标的扩权类动作，R6.3）；
- 防守类除外：决策动作经 orchestration/freeze.py 的 :class:`ActionClass` 语义归类，
  防守类动作本就不被任何冻结阻断（铁律 A5），其缺回执如实登记但**不产冻结建议**——
  本模块只引用该语义，不改写 freeze.py（其为红线断言层，双人复核保护）；
- 缺回执同时是违规台账七类事件之一（回执缺失，R18.3）：经可注入钩子回调登记。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..orchestration.freeze import ActionClass, ActionClassifier


class ReceiptError(Exception):
    """回执录入被拒绝。"""


@dataclass(frozen=True)
class TradeRecord:
    """成交回执（trade_record 语义对象，S9 产物；append-only，R18.1 加严）。"""

    trade_id: str
    decision_record_id: str
    symbol: str
    action: str  # 规范动作名（与 freeze.ActionClassifier 口径一致）
    executed_at: str  # 成交时间（ISO，券商回执口径）
    quantity: float
    price: float
    fees: float = 0.0
    venue: str = ""
    note: str = ""
    recorded_at: str = ""  # 录入时间（注入时钟产生）

    def __post_init__(self) -> None:
        if not (self.decision_record_id and self.symbol and self.action and self.executed_at):
            raise ReceiptError("回执必备字段缺失：decision_record_id/symbol/action/executed_at（R6.3）")
        if self.quantity <= 0 or self.price < 0:
            raise ReceiptError("回执数量必须 >0 且价格 ≥0")


class TradeStore(Protocol):
    """trade_record 存储协议：仅 append 与只读遍历（append-only，R18.1 加严）。"""

    def append(self, record: TradeRecord) -> None: ...

    def all(self) -> Sequence[TradeRecord]: ...


class InMemoryTradeStore:
    def __init__(self) -> None:
        self._records: list[TradeRecord] = []

    def append(self, record: TradeRecord) -> None:
        self._records.append(record)

    def all(self) -> Sequence[TradeRecord]:
        return tuple(self._records)


@dataclass(frozen=True)
class DecisionRef:
    """待核对的决策记录视图（S8 产物的最小引用）。"""

    decision_record_id: str
    symbol: str
    action: str  # 规范动作名（freeze.ActionClassifier 口径）
    executed: bool = True  # 用户声明已执行（S9 待回执）；未执行的决策不核回执


@dataclass(frozen=True)
class FreezeAdvice:
    """标的冻结标志建议（R6.3）：建议对 subject 置 ``receipt_missing`` 标的冻结标志。

    仅为建议对象——实际置位由编排层调用 FreezeFlags.set_instrument 执行并留痕；
    该冻结只阻断扩权类动作，防守类动作不受阻断（A5，见 freeze.freeze_gate）。
    """

    subject: str
    flag: str
    decision_record_id: str
    action_class: str
    note: str


@dataclass(frozen=True)
class MissingReceiptReport:
    """缺回执检测报告：冻结建议（非防守类）+ 防守类缺回执登记（不建议冻结，A5）。"""

    freeze_advices: tuple[FreezeAdvice, ...]
    defensive_missing: tuple[str, ...]  # 防守类缺回执的 decision_record_id（如实登记）


ViolationHook = Callable[[str, str], None]
"""违规台账钩子：``(subject, decision_record_id)``——登记"回执缺失"事件（R18.3）。"""

Clock = Callable[[], dt.datetime]


class ReceiptService:
    """成交回执录入与缺回执检测服务（任务 14.6）。"""

    def __init__(
        self,
        store: TradeStore | None = None,
        clock: Clock | None = None,
        classifier: ActionClassifier | None = None,
        violation_hook: ViolationHook | None = None,
    ) -> None:
        self._store = store or InMemoryTradeStore()
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._classifier = classifier or ActionClassifier()
        self._violation_hook = violation_hook

    def record_receipt(
        self,
        decision_record_id: str,
        *,
        symbol: str,
        action: str,
        executed_at: str,
        quantity: float,
        price: float,
        fees: float = 0.0,
        venue: str = "",
        note: str = "",
    ) -> TradeRecord:
        """录入成交回执 → trade_record（S9）；同一决策可多批次回执（分批纪律，R15.4）。"""
        record = TradeRecord(
            trade_id=uuid.uuid4().hex,
            decision_record_id=decision_record_id,
            symbol=symbol,
            action=action,
            executed_at=executed_at,
            quantity=quantity,
            price=price,
            fees=fees,
            venue=venue,
            note=note,
            recorded_at=self._clock().isoformat(timespec="seconds"),
        )
        self._store.append(record)
        return record

    def receipts(self) -> Sequence[TradeRecord]:
        return self._store.all()

    def receipts_for(self, decision_record_id: str) -> tuple[TradeRecord, ...]:
        return tuple(r for r in self._store.all() if r.decision_record_id == decision_record_id)

    def detect_missing(self, decisions: Iterable[DecisionRef]) -> MissingReceiptReport:
        """缺回执检测（R6.3）：已执行决策无 trade_record → 标的冻结标志建议。

        防守类决策（freeze.ActionClass 语义）除外：其缺回执如实登记并走违规台账
        （R18.3 回执缺失事件），但不产冻结建议——A5 下冻结对防守类无阻断意义，
        且不得以回执流程反向卡防守动作。
        """
        with_receipt = {r.decision_record_id for r in self._store.all()}
        advices: list[FreezeAdvice] = []
        defensive: list[str] = []
        for d in decisions:
            if not d.executed or d.decision_record_id in with_receipt:
                continue
            klass = self._classifier.classify(d.action)
            if self._violation_hook is not None:
                self._violation_hook(d.symbol, d.decision_record_id)
            if klass is ActionClass.DEFENSIVE:
                defensive.append(d.decision_record_id)
                continue
            advices.append(
                FreezeAdvice(
                    subject=d.symbol,
                    flag="receipt_missing",
                    decision_record_id=d.decision_record_id,
                    action_class=klass.value,
                    note=(
                        f"决策 {d.decision_record_id} 已执行但无成交回执：建议置标的冻结标志 "
                        "receipt_missing，阻断该标的扩权类动作（R6.3；防守类不受阻断，A5）"
                    ),
                )
            )
        return MissingReceiptReport(freeze_advices=tuple(advices), defensive_missing=tuple(defensive))
