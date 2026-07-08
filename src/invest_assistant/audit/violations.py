"""违规台账（任务 21.5，需求 R18.3；设计方案 §3.9）。

七类事件由风控断言层/冻结矩阵/S8 校验在拒绝或告警处调用 register()；
未闭环条目自动注入月/季复盘议程。append-only 语义（更正=新版本追加）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from enum import StrEnum


class ViolationCategory(StrEnum):
    """违规台账七类事件（R18.3）。"""

    REDLINE_TOUCH = "红线触碰"
    COOLOFF_BREACH = "冷静期违规"
    COUNT_EXCEEDED = "次数超限"
    REVIEW_OVERDUE = "复盘逾期"
    FALSIFY_DELAY = "证伪拖延"
    RECEIPT_MISSING = "回执缺失"
    PARAM_OVERREACH = "参数越权尝试"


@dataclass(frozen=True)
class ViolationEntry:
    entry_id: int
    category: ViolationCategory
    subject: str
    detail: str
    created_on: str  # 账务日 ISO
    closed: bool = False


@dataclass(frozen=True)
class AgendaItem:
    category: ViolationCategory
    subject: str
    detail: str
    entry_id: int


class ViolationLedger:
    """违规台账（append-only：闭环以新版本追加，原条目不改）。"""

    def __init__(self) -> None:
        self._entries: list[ViolationEntry] = []
        self._seq = 0

    def register(self, category: ViolationCategory, subject: str, detail: str,
                 created_on: str) -> ViolationEntry:
        self._seq += 1
        entry = ViolationEntry(self._seq, category, subject, detail, created_on)
        self._entries.append(entry)
        return entry

    def close(self, entry_id: int, closed_on: str) -> ViolationEntry:
        """闭环 = 追加 closed 版本（append-only，原条目保留）。"""
        original = next((e for e in self._entries if e.entry_id == entry_id and not e.closed), None)
        if original is None:
            raise KeyError(f"无未闭环台账条目 {entry_id}")
        self._seq += 1
        closed_entry = ViolationEntry(self._seq, original.category, original.subject,
                                      f"[闭环 #{entry_id}] {original.detail}", closed_on, closed=True)
        self._entries.append(closed_entry)
        return closed_entry

    def open_entries(self) -> list[ViolationEntry]:
        """未闭环条目：登记后未被 close 版本引用的。"""
        closed_ids = {int(e.detail.split("#")[1].split("]")[0])
                      for e in self._entries if e.closed and "#" in e.detail}
        return [e for e in self._entries if not e.closed and e.entry_id not in closed_ids]

    def review_agenda(self) -> list[AgendaItem]:
        """未闭环条目自动注入复盘议程（R18.3）。"""
        return [AgendaItem(e.category, e.subject, e.detail, e.entry_id) for e in self.open_entries()]

    def all_entries(self) -> list[ViolationEntry]:
        return list(self._entries)


def today_iso() -> str:  # pragma: no cover
    return _dt.date.today().isoformat()


# 供风控断言层/冻结矩阵/S8 校验直接调用的类别别名（对齐各层触发点）
CATEGORY_BY_SOURCE: dict[str, ViolationCategory] = {
    "redline": ViolationCategory.REDLINE_TOUCH,
    "cooloff": ViolationCategory.COOLOFF_BREACH,
    "counts": ViolationCategory.COUNT_EXCEEDED,
    "review": ViolationCategory.REVIEW_OVERDUE,
    "falsify": ViolationCategory.FALSIFY_DELAY,
    "receipt": ViolationCategory.RECEIPT_MISSING,
    "param": ViolationCategory.PARAM_OVERREACH,
}
