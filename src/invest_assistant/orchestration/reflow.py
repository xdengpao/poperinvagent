"""滞留/裁决回流控制器（任务 13.4，需求 R6.6/R6.7）。

- 滞留登记：S3 证据不足的标的登记 ``stranded_since_period``（design §5.4）；
- 补跑：缺口关闭事件或下期扫描触发补取证重跑（产物为新 attempt、入下一
  版本快照，R3.1/R3.4）；连续滞留达阈值期（默认 3，阈值参数化）转淘汰并
  留痕（R6.6）；
- 裁决回流：裁决完成事件清除对应冻结并生成"重入 S3 以裁决结果重判"指令；
  重跑遇未决裁决时挂起该标的本期流转，裁决请求按幂等键（标的×争点）去重、
  不重复派发（R6.7）。

期次一律用字符串期次号（如 "2026-01"），期次推进按扫描事件计数，
不做日期算术、不引入裸 timedelta（R4.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from invest_assistant.orchestration.freeze import FreezeFlags


class StrandedStatus(StrEnum):
    STRANDED = "stranded"
    RESOLVED = "resolved"
    ELIMINATED = "eliminated"


@dataclass
class StrandedRecord:
    """滞留登记（R6.6）：periods_stranded 为登记后经历的期次扫描数。"""

    subject: str
    stranded_since_period: str
    gap_fields: tuple[str, ...] = ()
    periods_stranded: int = 0
    status: StrandedStatus = StrandedStatus.STRANDED
    note: str = ""


@dataclass(frozen=True)
class RerunRequest:
    """补取证重跑请求（R6.6）：产物以新 attempt 进入下一版本快照（R3.1/R3.4）。"""

    subject: str
    period: str
    cause: str  # "gap_closed" | "next_period_scan"


@dataclass(frozen=True)
class EliminationRecord:
    """连续滞留达阈值转淘汰的留痕（R6.6）。"""

    subject: str
    stranded_since_period: str
    eliminated_period: str
    periods_stranded: int
    reason: str = "连续滞留达阈值，转淘汰（R6.6）"


@dataclass(frozen=True)
class RulingRequest:
    """裁决请求：幂等键 = 标的×争点，重复不再派发（R6.7）。"""

    subject: str
    issue: str
    idempotency_key: str


@dataclass(frozen=True)
class Suspension:
    """未决裁决挂起：该标的本期不流转、不派发新裁决请求（R6.7）。"""

    subject: str
    period: str
    ruling_key: str


@dataclass(frozen=True)
class ReentryInstruction:
    """裁决完成后的回流指令：清除冻结 + 重入 S3 以裁决结果重判（R6.7）。"""

    subject: str
    ruling_id: str
    stage: str = "S3"


@dataclass
class ReflowController:
    """回流控制器（R6.6/R6.7）。

    传入 ``FreezeFlags`` 时联动冻结矩阵：裁决请求派发即置该标的
    ``ruling_pending``（阻断扩权类，防守类不受影响——A5），裁决完成即清除。
    未决裁决期间的期次扫描只产挂起、不计淘汰期次（滞留淘汰针对取证缺口，
    裁决未决的出口是裁决完成事件而非 3 期淘汰）。
    """

    flags: FreezeFlags | None = None
    elimination_threshold: int = 3  # 阈值参数化（R6.6，默认 3 期）
    log: list[object] = field(default_factory=list)  # 淘汰/挂起/派发/重入留痕（A4）
    _stranded: dict[str, StrandedRecord] = field(default_factory=dict)
    _pending_rulings: dict[str, RulingRequest] = field(default_factory=dict)
    _subject_ruling_keys: dict[str, set[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.elimination_threshold < 1:
            raise ValueError("淘汰阈值至少为 1 期（R6.6）")

    # —— 滞留登记与解除 ——
    def register_stranded(
        self, subject: str, period: str, gap_fields: tuple[str, ...] = ()
    ) -> StrandedRecord:
        """登记滞留（幂等：已滞留标的重复登记返回原记录，不重置计数）。"""
        existing = self._stranded.get(subject)
        if existing is not None and existing.status == StrandedStatus.STRANDED:
            return existing
        rec = StrandedRecord(subject=subject, stranded_since_period=period, gap_fields=tuple(gap_fields))
        self._stranded[subject] = rec
        return rec

    def resolve(self, subject: str, period: str, note: str = "") -> None:
        """补跑成功、缺口闭合：解除滞留并留痕（R6.6）。"""
        rec = self._stranded.get(subject)
        if rec is not None and rec.status == StrandedStatus.STRANDED:
            rec.status = StrandedStatus.RESOLVED
            rec.note = f"{period}: {note or '滞留解除'}"

    def stranded_record(self, subject: str) -> StrandedRecord | None:
        return self._stranded.get(subject)

    # —— 补跑触发（R6.6）——
    def on_gap_closed(self, subject: str, period: str) -> RerunRequest | Suspension | None:
        """缺口关闭事件 → 补取证重跑；遇未决裁决则挂起（R6.6/R6.7）。"""
        rec = self._stranded.get(subject)
        if rec is None or rec.status != StrandedStatus.STRANDED:
            return None
        if self._has_pending_ruling(subject):
            return self._suspend(subject, period)
        req = RerunRequest(subject=subject, period=period, cause="gap_closed")
        self.log.append(req)
        return req

    def on_next_period_scan(self, period: str) -> list[RerunRequest | EliminationRecord | Suspension]:
        """下期扫描：滞留标的补跑；达阈值期仍滞留转淘汰；未决裁决挂起（R6.6/R6.7）。"""
        out: list[RerunRequest | EliminationRecord | Suspension] = []
        for rec in self._stranded.values():
            if rec.status != StrandedStatus.STRANDED:
                continue
            if self._has_pending_ruling(rec.subject):
                out.append(self._suspend(rec.subject, period))  # 挂起本期，不计淘汰期次
                continue
            rec.periods_stranded += 1
            if rec.periods_stranded >= self.elimination_threshold:
                rec.status = StrandedStatus.ELIMINATED
                elim = EliminationRecord(
                    subject=rec.subject,
                    stranded_since_period=rec.stranded_since_period,
                    eliminated_period=period,
                    periods_stranded=rec.periods_stranded,
                )
                self.log.append(elim)
                out.append(elim)
            else:
                req = RerunRequest(subject=rec.subject, period=period, cause="next_period_scan")
                self.log.append(req)
                out.append(req)
        return out

    # —— 裁决回流（R6.7）——
    def request_ruling(self, subject: str, issue: str, period: str) -> tuple[Suspension, RulingRequest | None]:
        """重跑遇未决裁决：挂起本期流转 + 按幂等键派发裁决请求（R6.7）。

        同一 标的×争点 已派发过则不重复派发（第二返回值为 None）；
        派发即置该标的 ``ruling_pending`` 冻结标志（扩权类被阻断，A5 除外）。
        """
        key = f"{subject}:{issue}"
        suspension = self._suspend(subject, period, ruling_key=key)
        if key in self._pending_rulings:
            return suspension, None
        req = RulingRequest(subject=subject, issue=issue, idempotency_key=key)
        self._pending_rulings[key] = req
        self._subject_ruling_keys.setdefault(subject, set()).add(key)
        if self.flags is not None:
            self.flags.set_instrument("ruling_pending", subject, note=f"裁决未决：{issue}（R6.7）")
        self.log.append(req)
        return suspension, req

    def on_ruling_completed(self, subject: str, issue: str, ruling_id: str) -> ReentryInstruction:
        """裁决完成事件：清除对应冻结 + 生成重入 S3 指令（R6.7）。

        幂等键随裁决闭环释放：同一标的后续新争点可再次派发。
        """
        key = f"{subject}:{issue}"
        self._pending_rulings.pop(key, None)
        keys = self._subject_ruling_keys.get(subject, set())
        keys.discard(key)
        if self.flags is not None and not keys:
            self.flags.clear_instrument("ruling_pending", subject, note=f"裁决完成：{ruling_id}（R6.7）")
        instruction = ReentryInstruction(subject=subject, ruling_id=ruling_id)
        self.log.append(instruction)
        return instruction

    # —— 内部 ——
    def _has_pending_ruling(self, subject: str) -> bool:
        return bool(self._subject_ruling_keys.get(subject))

    def _suspend(self, subject: str, period: str, ruling_key: str = "") -> Suspension:
        key = ruling_key or min(self._subject_ruling_keys.get(subject, {""}))
        suspension = Suspension(subject=subject, period=period, ruling_key=key)
        self.log.append(suspension)
        return suspension
