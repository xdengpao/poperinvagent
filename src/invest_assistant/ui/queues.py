"""签认队列与裁决队列（任务 14.2/14.3，需求 R17.1/R17.2/R17.4；design §3.8 交互层）。

- 待签认队列（R17.1）：定性初评 / 论点卡起草稿 / 参数变更 / 复盘结论四类对象，
  动作 approve / reject（必须记理由）/ modify（经 tighten-only 校验钩子把关，
  与 analysis/thesis.py 的 R13.2、rules/params.py 的 R5.2 同向：只可更保守）；
- 待裁决队列（R17.1）：条目必须含争点、正反证据、缺省处置、复核条件四要素，
  分类/文本冲突等"不得自行择一"的争议只能在此由用户裁决（R2.3/R5.3）；
- 全部动作产 signoff 留痕记录（append-only 语义存储，可注入，R18.1）；
- 驳回率统计 + 连续 N 期复盘驳回率为 0 → 盲从风险提示（R17.2，N 默认 4 只可更严）；
- S0 不适用四类资金申报 → 硬拒绝进入投资流程、仅开放学习模式（R17.4）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

DEFAULT_BLIND_FOLLOW_PERIODS = 4
"""盲从风险提示的连续零驳回期数默认值（R17.2：实现级参数，默认 4，只可更严=只可调小）。"""


class QueueError(Exception):
    """队列操作被拒绝的基类。"""


class UnknownItemError(QueueError):
    """条目不存在或已处理完毕。"""


class ReasonRequired(QueueError):
    """驳回动作必须记录理由（R17.2）。"""


class TightenOnlyRejected(QueueError):
    """modify 动作未通过 tighten-only 校验钩子——只可更保守方向（R13.2/R5.2 同向）。"""


class StricterOnlyViolation(QueueError):
    """实现级参数只可更严方向修改被拒（R17.2/附录 E 保守方向约束）。"""


class SignoffItemType(StrEnum):
    """待签认对象四类（R17.1）。"""

    QUALITATIVE_REVIEW = "qualitative_review"  # 定性初评（R10.1）
    THESIS_DRAFT = "thesis_draft"  # 论点卡起草稿（R13.1/R13.2）
    PARAM_CHANGE = "param_change"  # 参数变更（R5.2）
    REVIEW_CONCLUSION = "review_conclusion"  # 复盘结论（R6.8 月/季/年复盘）


class QueueAction(StrEnum):
    """签认队列动作（R17.1）。"""

    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"


class RulingDecision(StrEnum):
    """裁决队列动作（R17.1）：采纳争点判断 / 驳回 / 采用缺省处置。"""

    UPHOLD = "uphold"  # 裁定命中/成立
    REJECT = "reject"  # 裁定不成立
    DEFAULT_DISPOSITION = "default_disposition"  # 采用条目登记的缺省处置


@dataclass(frozen=True)
class SignoffItem:
    """待签认条目（R17.1）。payload 为待签认对象的语义内容（如初评分/论点卡草稿）。"""

    item_id: str
    item_type: SignoffItemType
    subject: str
    payload: Mapping[str, object]
    period: str = ""  # 所属复盘期次（如 "2026Q2"），供驳回率统计（R17.2）


@dataclass(frozen=True)
class RulingItem:
    """待裁决条目（R17.1）：争点、正反证据、缺省处置、复核条件四要素必备。"""

    item_id: str
    subject: str
    issue: str  # 争点
    evidence_pro: tuple[str, ...]  # 正方证据 ID 集
    evidence_con: tuple[str, ...]  # 反方证据 ID 集
    default_disposition: str  # 缺省处置（未裁决期间的保守动作）
    review_condition: str  # 复核条件（R5.6 条件化复核触发）

    def __post_init__(self) -> None:
        if not (self.issue and self.default_disposition and self.review_condition):
            raise ValueError("裁决条目必须含争点/缺省处置/复核条件（R17.1）")


@dataclass(frozen=True)
class SignoffRecord:
    """signoff 留痕记录（R17.1 全部动作留痕；R18.1 append-only 语义）。"""

    record_id: str
    queue: str  # "signoff" | "ruling"
    item_id: str
    item_type: str
    subject: str
    action: str
    actor: str
    reason: str
    period: str
    changes: Mapping[str, object]
    recorded_at: str  # ISO 时间戳（注入时钟产生）


class RecordStore(Protocol):
    """留痕存储协议：仅 append 与只读遍历（append-only，R18.1）。"""

    def append(self, record: SignoffRecord) -> None: ...

    def all(self) -> Sequence[SignoffRecord]: ...


class InMemoryRecordStore:
    """内存留痕存储（默认实现；生产替换为 PostgreSQL append-only 表，design §3.9）。"""

    def __init__(self) -> None:
        self._records: list[SignoffRecord] = []

    def append(self, record: SignoffRecord) -> None:
        self._records.append(record)

    def all(self) -> Sequence[SignoffRecord]:
        return tuple(self._records)


TightenHook = Callable[[Mapping[str, object], Mapping[str, object]], bool]
"""tighten-only 校验钩子：``(原 payload, 修改内容) -> 是否更保守``（R13.2/R5.2 同向）。"""

Clock = Callable[[], dt.datetime]


def _default_clock() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class SignoffQueue:
    """待签认队列（R17.1/R17.2）。"""

    def __init__(self, store: RecordStore | None = None, clock: Clock | None = None) -> None:
        self._store = store or InMemoryRecordStore()
        self._clock = clock or _default_clock
        self._pending: dict[str, SignoffItem] = {}
        self._tighten_hooks: dict[SignoffItemType, TightenHook] = {}

    # —— 条目管理 ——
    def submit(self, item: SignoffItem) -> None:
        if item.item_id in self._pending:
            raise QueueError(f"条目重复入队：{item.item_id}")
        self._pending[item.item_id] = item

    def pending(self) -> tuple[SignoffItem, ...]:
        return tuple(self._pending.values())

    def register_tighten_hook(self, item_type: SignoffItemType, hook: TightenHook) -> None:
        """按对象类型挂 tighten-only 校验钩子（论点卡→R13.2；参数→R5.2）。"""
        self._tighten_hooks[item_type] = hook

    # —— 动作 ——
    def act(
        self,
        item_id: str,
        action: QueueAction,
        *,
        actor: str,
        reason: str = "",
        changes: Mapping[str, object] | None = None,
    ) -> SignoffRecord:
        """执行签认动作并留痕（R17.1）。

        - reject 必须携带理由并入回看统计（R17.2）；
        - modify 必须通过该类型登记的 tighten-only 钩子（只可更保守）；
        - approve/modify 使条目出队；reject 同样出队（对象回上游重做）。
        """
        item = self._pending.get(item_id)
        if item is None:
            raise UnknownItemError(f"待签认条目不存在：{item_id}")
        if action is QueueAction.REJECT and not reason.strip():
            raise ReasonRequired("驳回必须记录理由（R17.2）")
        changes = dict(changes or {})
        if action is QueueAction.MODIFY:
            hook = self._tighten_hooks.get(item.item_type)
            if hook is None:
                raise TightenOnlyRejected(
                    f"{item.item_type} 未登记 tighten-only 校验钩子，modify 不放行（R13.2/R5.2）"
                )
            if not hook(item.payload, changes):
                raise TightenOnlyRejected(f"条目 {item_id} 的修改不满足只可更保守方向（R13.2/R5.2）")
        record = SignoffRecord(
            record_id=uuid.uuid4().hex,
            queue="signoff",
            item_id=item.item_id,
            item_type=item.item_type.value,
            subject=item.subject,
            action=action.value,
            actor=actor,
            reason=reason,
            period=item.period,
            changes=changes,
            recorded_at=self._clock().isoformat(timespec="seconds"),
        )
        self._store.append(record)
        del self._pending[item_id]
        return record

    # —— 驳回率统计与盲从提示（R17.2） ——
    def rejection_stats(self) -> dict[str, dict[str, float]]:
        """按复盘期次统计驳回率：期次 → {total, rejected, rate}。"""
        stats: dict[str, dict[str, float]] = {}
        for r in self._store.all():
            if r.queue != "signoff" or not r.period:
                continue
            s = stats.setdefault(r.period, {"total": 0.0, "rejected": 0.0, "rate": 0.0})
            s["total"] += 1
            if r.action == QueueAction.REJECT.value:
                s["rejected"] += 1
        for s in stats.values():
            s["rate"] = s["rejected"] / s["total"] if s["total"] else 0.0
        return stats

    def blind_follow_alert(
        self,
        ordered_periods: Sequence[str],
        *,
        required_zero_periods: int = DEFAULT_BLIND_FOLLOW_PERIODS,
    ) -> BlindFollowAlert | None:
        """连续 N 期复盘期驳回率均为 0 → 盲从风险提示对象（R17.2）。

        ``ordered_periods`` 为按时间升序的复盘期次序列（如季度键）；
        ``required_zero_periods`` 默认 4，只可更严（只可调小——更早提示）。
        """
        if required_zero_periods > DEFAULT_BLIND_FOLLOW_PERIODS:
            raise StricterOnlyViolation(
                f"盲从提示期数只可更严（≤{DEFAULT_BLIND_FOLLOW_PERIODS}），拒绝 {required_zero_periods}（R17.2）"
            )
        if required_zero_periods < 1:
            raise ValueError("期数必须 ≥1")
        if len(ordered_periods) < required_zero_periods:
            return None
        window = list(ordered_periods)[-required_zero_periods:]
        stats = self.rejection_stats()
        for p in window:
            s = stats.get(p)
            if s is None or s["total"] == 0 or s["rejected"] > 0:
                return None
        return BlindFollowAlert(
            periods=tuple(window),
            message=(
                f"连续 {required_zero_periods} 期复盘驳回率为 0：存在盲从系统输出的风险，"
                "请在复盘中核对签认动作是否经独立判断（R17.2）"
            ),
        )


@dataclass(frozen=True)
class BlindFollowAlert:
    """盲从风险提示对象（R17.2）：注入复盘报告。"""

    periods: tuple[str, ...]
    message: str


class RulingQueue:
    """待裁决队列（R17.1）：争点/正反证据/缺省处置/复核条件；全部裁决动作留痕。"""

    def __init__(self, store: RecordStore | None = None, clock: Clock | None = None) -> None:
        self._store = store or InMemoryRecordStore()
        self._clock = clock or _default_clock
        self._pending: dict[str, RulingItem] = {}

    def submit(self, item: RulingItem) -> None:
        if item.item_id in self._pending:
            raise QueueError(f"裁决条目重复入队：{item.item_id}")
        self._pending[item.item_id] = item

    def pending(self) -> tuple[RulingItem, ...]:
        return tuple(self._pending.values())

    def decide(
        self, item_id: str, decision: RulingDecision, *, actor: str, rationale: str
    ) -> SignoffRecord:
        """登记裁决结果并留痕；裁决完成事件由编排层消费清除冻结（R6.7）。"""
        item = self._pending.get(item_id)
        if item is None:
            raise UnknownItemError(f"待裁决条目不存在：{item_id}")
        if not rationale.strip():
            raise ReasonRequired("裁决必须记录理由（R17.1 全部动作留痕）")
        record = SignoffRecord(
            record_id=uuid.uuid4().hex,
            queue="ruling",
            item_id=item.item_id,
            item_type="ruling",
            subject=item.subject,
            action=decision.value,
            actor=actor,
            reason=rationale,
            period="",
            changes={
                "issue": item.issue,
                "default_disposition": item.default_disposition,
                "review_condition": item.review_condition,
            },
            recorded_at=self._clock().isoformat(timespec="seconds"),
        )
        self._store.append(record)
        del self._pending[item_id]
        return record


# ————— S0 不适用情形硬拒绝（R17.4） —————


class ExcludedFundType(StrEnum):
    """框架文档信息表四类不适用资金枚举（R17.4）。"""

    LEVERAGED = "杠杆资金"
    BORROWED = "借贷资金"
    DEADLINE_DOUBLING = "限期翻倍资金"
    DERIVATIVES_SPECULATION = "期货期权投机资金"


@dataclass(frozen=True)
class AdmissionDecision:
    """S0 准入判定（R17.4）：命中任一不适用情形 → 硬拒绝，仅开放学习模式。"""

    admitted: bool
    mode: str  # "invest" | "learning_only"
    hit_types: tuple[str, ...]
    reason: str


def s0_admission(declared: Iterable[str | ExcludedFundType]) -> AdmissionDecision:
    """S0 资金性质申报校验（R17.4）：四类不适用情形命中即硬拒绝进入投资流程。"""
    valid = {t.value for t in ExcludedFundType}
    hits: list[str] = []
    for d in declared:
        v = d.value if isinstance(d, ExcludedFundType) else str(d)
        if v in valid and v not in hits:
            hits.append(v)
    if hits:
        return AdmissionDecision(
            admitted=False,
            mode="learning_only",
            hit_types=tuple(hits),
            reason=f"申报命中不适用情形 {hits}：硬拒绝进入投资流程，仅开放学习模式（R17.4）",
        )
    return AdmissionDecision(admitted=True, mode="invest", hit_types=(), reason="")
