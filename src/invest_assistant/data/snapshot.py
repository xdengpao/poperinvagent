"""快照管理器（任务 7，需求 R3）。

快照 = 元数据（类型/截止时点/规则版本/参数版本/生效裁决集/内容哈希）+ 证据 ID 集。
月度快照切齐即冻结（补跑产物入下一版本）；事件快照按需生成；
活数据白名单：仅信号检测与净值监控可读非快照数据，其输出只能是触发事件（R3.5）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field

from ..core.types import Evidence


class SnapshotFrozen(Exception):
    pass


class LiveDataForbidden(Exception):
    pass


@dataclass
class Snapshot:
    snapshot_id: str
    kind: str  # monthly | event
    cutoff: str  # ISO 日期（收盘切齐）
    rule_version: str
    param_version: str
    active_rulings: list[str] = field(default_factory=list)
    evidence_ids: set[str] = field(default_factory=set)
    frozen: bool = False
    content_hash: str = ""

    def add_evidence(self, ev: Evidence) -> None:
        if self.frozen:
            raise SnapshotFrozen(f"{self.snapshot_id} 已冻结：补跑产物应进下一版本快照（R3.1）")
        self.evidence_ids.add(ev.evidence_id)

    def freeze(self) -> str:
        payload = json.dumps(
            {
                "kind": self.kind,
                "cutoff": self.cutoff,
                "rule_version": self.rule_version,
                "param_version": self.param_version,
                "rulings": sorted(self.active_rulings),
                "evidence": sorted(self.evidence_ids),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.content_hash = hashlib.sha256(payload.encode()).hexdigest()
        self.frozen = True
        return self.content_hash


#: 活数据白名单（R3.5）：调用方标识 → 允许读活数据
LIVE_READERS = frozenset({"SignalDetector", "NavMonitor"})


def read_live(caller: str) -> None:
    """活数据访问闸门：白名单外一律拒绝（判定必须走快照）。"""
    if caller not in LIVE_READERS:
        raise LiveDataForbidden(f"{caller} 不在活数据白名单（R3.5）——判定输入必须引用快照")


@dataclass
class SnapshotStore:
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    _seq: int = 0

    def create_monthly(self, cutoff: _dt.date, rule_version: str, param_version: str,
                       rulings: list[str]) -> Snapshot:
        self._seq += 1
        s = Snapshot(
            snapshot_id=f"SNAP-M-{cutoff.isoformat()}-{self._seq:03d}",
            kind="monthly",
            cutoff=cutoff.isoformat(),
            rule_version=rule_version,
            param_version=param_version,
            active_rulings=list(rulings),
        )
        self.snapshots[s.snapshot_id] = s
        return s

    def create_event(self, trigger_date: _dt.date, rule_version: str, param_version: str,
                     rulings: list[str]) -> Snapshot:
        self._seq += 1
        s = Snapshot(
            snapshot_id=f"SNAP-E-{trigger_date.isoformat()}-{self._seq:03d}",
            kind="event",
            cutoff=trigger_date.isoformat(),
            rule_version=rule_version,
            param_version=param_version,
            active_rulings=list(rulings),
        )
        self.snapshots[s.snapshot_id] = s
        return s
