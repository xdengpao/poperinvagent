"""触发器总线（任务 13.2，需求 R6.4/R6.5）。

trigger_event 持久化语义的内存实现（生产实现为 append-only 表 + 事务领取，
design §3.3）：登记（register，持久化优先于消费）→ 领取（claim）→ 消费确认
（ack），任何事件不可重复消费；同一账务日收盘批次内按框架 6.6 顺位仲裁，
高位吸收低位——被吸收事件置 ``absorbed_by`` 留痕且不再派发（R6.5）；
日终以"应触发集 vs 已处理集"对账，差异生成告警（R6.4/R19.1）。

账务日一律用字符串日期（R4.2 拼合口径），不引入裸 timedelta（R4.3）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

# 框架 6.6 顺位表（模块数据，R6.5）：索引越小顺位越高，高位吸收低位
PRIORITY_LADDER: tuple[str, ...] = (
    "redline_disposal",  # 顺位 1：红线处置
    "falsified_liquidation",  # 顺位 2：证伪清仓（R13.4）
    "stop_loss_halve",  # 顺位 3：止损减半（R12.6）
    "drawdown_ladder",  # 顺位 4：回撤阶梯（R15.3）
    "re_review_line",  # 顺位 5：重审线（成本 -20% 强制重审，R12.6）
    "over_cap_reduce",  # 顺位 6：超顶降回（R15.4）
    "voluntary_request",  # 顺位 7：主动申请
)

_RANK: dict[str, int] = {kind: i for i, kind in enumerate(PRIORITY_LADDER)}


def priority_rank(kind: str) -> int | None:
    """6.6 顺位（0 为最高）；顺位表外触发源（日历/缺口关闭/裁决完成等）不参与仲裁。"""
    return _RANK.get(kind)


class TriggerStatus(StrEnum):
    """事件生命周期：登记 → 领取 → 确认；或被高位吸收（终态，不再派发）。"""

    REGISTERED = "registered"
    CLAIMED = "claimed"
    ACKED = "acked"
    ABSORBED = "absorbed"


class DuplicateConsumption(Exception):
    """重复领取/重复确认/越权确认被拒——事件不可重复消费（R6.4）。"""


class NotDispatchable(Exception):
    """事件不可派发：不存在，或已被高位吸收（R6.5）。"""


@dataclass
class TriggerEvent:
    """触发器事件（trigger_event 行的内存形态，append-only 语义，R6.4/R18.1）。"""

    event_id: str
    kind: str
    subject: str
    accounting_day: str  # 账务日（R4.2 拼合口径），ISO 日期字符串
    seq: int
    payload: dict = field(default_factory=dict)
    source: str = ""
    status: TriggerStatus = TriggerStatus.REGISTERED
    absorbed_by: str = ""  # 被吸收时指向胜出事件（R6.5 留痕）
    claimed_by: str = ""


@dataclass(frozen=True)
class ReconcileReport:
    """日终对账差异清单（R6.4）：键为 (kind, subject)。"""

    missing: tuple[tuple[str, str], ...]  # 应触发未处理
    unexpected: tuple[tuple[str, str], ...]  # 已处理但不在应触发集

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unexpected

    def alerts(self) -> list[str]:
        """差异告警清单（R6.4/R19.1），空清单即对账通过。"""
        out = [f"对账告警：应触发未处理 {kind}/{subject}（R6.4）" for kind, subject in self.missing]
        out += [f"对账告警：处理了应触发集之外的事件 {kind}/{subject}（R6.4）" for kind, subject in self.unexpected]
        return out


def reconcile(
    expected: Iterable[tuple[str, str]], processed: Iterable[tuple[str, str]]
) -> ReconcileReport:
    """日终对账纯函数（R6.4）：应触发集（从检测器输入独立推导）vs 已处理集。"""
    exp, done = set(expected), set(processed)
    return ReconcileReport(missing=tuple(sorted(exp - done)), unexpected=tuple(sorted(done - exp)))


class TriggerBus:
    """触发器总线：持久化登记 → 批次仲裁 → 事务领取 → 消费确认（R6.4/R6.5）。

    全部状态迁移追加进 ``oplog``（append-only 操作留痕，A4）；崩溃恢复语义
    由持久化表承载（design §2.2），本实现保证同样的迁移约束。
    """

    def __init__(self) -> None:
        self._events: dict[str, TriggerEvent] = {}
        self._seq = 0
        self.oplog: list[dict] = []

    def _append_log(self, op: str, event_id: str, **extra: object) -> None:
        self.oplog.append({"op": op, "event_id": event_id, **extra})

    def register(
        self,
        kind: str,
        subject: str,
        accounting_day: str,
        payload: dict | None = None,
        source: str = "",
    ) -> TriggerEvent:
        """登记事件：持久化优先于消费（R6.4）。"""
        self._seq += 1
        ev = TriggerEvent(
            event_id=f"trg-{accounting_day}-{self._seq:04d}",
            kind=kind,
            subject=subject,
            accounting_day=accounting_day,
            seq=self._seq,
            payload=dict(payload or {}),
            source=source,
        )
        self._events[ev.event_id] = ev
        self._append_log("register", ev.event_id, kind=kind, subject=subject, day=accounting_day)
        return ev

    def arbitrate_batch(self, accounting_day: str) -> list[TriggerEvent]:
        """同账务日收盘批次内按 6.6 顺位仲裁（R6.5）。

        按标的分组：组内最高顺位事件吸收全部更低顺位事件（置 ``absorbed_by``，
        状态 ABSORBED，不再派发）；同顺位并存不互吸；顺位表外触发源不参与仲裁。
        返回本批次仍可派发的事件，按（顺位，登记序）排序。
        """
        pending = [
            e
            for e in self._events.values()
            if e.accounting_day == accounting_day and e.status == TriggerStatus.REGISTERED
        ]
        by_subject: dict[str, list[TriggerEvent]] = {}
        for e in pending:
            if priority_rank(e.kind) is not None:
                by_subject.setdefault(e.subject, []).append(e)
        for group in by_subject.values():
            ranks = {e.event_id: _RANK[e.kind] for e in group}
            best = min(ranks.values())
            winner = min((e for e in group if ranks[e.event_id] == best), key=lambda e: e.seq)
            for e in group:
                if ranks[e.event_id] > best:  # 高位吸收低位（R6.5 留痕）
                    e.status = TriggerStatus.ABSORBED
                    e.absorbed_by = winner.event_id
                    self._append_log("absorb", e.event_id, absorbed_by=winner.event_id)
        survivors = [e for e in pending if e.status == TriggerStatus.REGISTERED]
        survivors.sort(key=lambda e: (_RANK.get(e.kind, len(PRIORITY_LADDER)), e.seq))
        return survivors

    def claim(self, event_id: str, consumer: str) -> TriggerEvent:
        """事务领取：仅 REGISTERED 事件可领取一次（R6.4）。"""
        ev = self._events.get(event_id)
        if ev is None:
            raise NotDispatchable(f"未知事件：{event_id}")
        if ev.status == TriggerStatus.ABSORBED:
            raise NotDispatchable(f"{event_id} 已被 {ev.absorbed_by} 吸收，不再派发（R6.5）")
        if ev.status != TriggerStatus.REGISTERED:
            raise DuplicateConsumption(f"{event_id} 已被领取/消费，拒绝重复消费（R6.4）")
        ev.status = TriggerStatus.CLAIMED
        ev.claimed_by = consumer
        self._append_log("claim", ev.event_id, consumer=consumer)
        return ev

    def ack(self, event_id: str, consumer: str) -> None:
        """消费确认：仅可由领取者确认一次（R6.4）。"""
        ev = self._events.get(event_id)
        if ev is None:
            raise NotDispatchable(f"未知事件：{event_id}")
        if ev.status == TriggerStatus.ABSORBED:
            raise NotDispatchable(f"{event_id} 已被吸收，不存在消费（R6.5）")
        if ev.status == TriggerStatus.ACKED:
            raise DuplicateConsumption(f"{event_id} 已确认，拒绝重复消费（R6.4）")
        if ev.status != TriggerStatus.CLAIMED or ev.claimed_by != consumer:
            raise DuplicateConsumption(f"{event_id} 未由 {consumer} 领取，拒绝确认（R6.4）")
        ev.status = TriggerStatus.ACKED
        self._append_log("ack", ev.event_id, consumer=consumer)

    def processed_keys(self, accounting_day: str) -> set[tuple[str, str]]:
        """当日已处理集：已消费确认，或被高位吸收（吸收即留痕处理，R6.5）。"""
        return {
            (e.kind, e.subject)
            for e in self._events.values()
            if e.accounting_day == accounting_day
            and e.status in (TriggerStatus.ACKED, TriggerStatus.ABSORBED)
        }

    def reconcile_day(
        self, accounting_day: str, expected: Iterable[tuple[str, str]]
    ) -> ReconcileReport:
        """日终对账（R6.4）：应触发集 vs 当日已处理集，差异见 ``ReconcileReport.alerts``。"""
        return reconcile(expected, self.processed_keys(accounting_day))
