"""审计链（任务 1.3，需求 R18.2）。

每日批任务：对当日新增 append-only 对象计算 Merkle 根，与前日链头连成哈希链；
恢复演练执行链完整性校验（篡改任意历史对象 → 校验失败）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_hash(obj: dict) -> str:
    return _h(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode())


def merkle_root(hashes: list[str]) -> str:
    """确定性 Merkle 根（奇数层复制尾节点）。空日以空串哈希占位。"""
    if not hashes:
        return _h(b"")
    layer = sorted(hashes)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [_h((layer[i] + layer[i + 1]).encode()) for i in range(0, len(layer), 2)]
    return layer[0]


@dataclass
class ChainLink:
    day: str
    prev_head: str
    root: str
    count: int

    @property
    def head(self) -> str:
        return _h(f"{self.day}|{self.prev_head}|{self.root}|{self.count}".encode())


@dataclass
class AuditChain:
    links: list[ChainLink] = field(default_factory=list)

    def append_day(self, day: str, objects: list[dict]) -> ChainLink:
        prev = self.links[-1].head if self.links else ""
        link = ChainLink(day=day, prev_head=prev, root=merkle_root([object_hash(o) for o in objects]), count=len(objects))
        self.links.append(link)
        return link

    def verify(self, objects_by_day: dict[str, list[dict]]) -> tuple[bool, str]:
        """恢复演练用（R18.2）：重算全链，任何历史对象被篡改/链断裂 → (False, 位置)。"""
        prev = ""
        for link in self.links:
            objs = objects_by_day.get(link.day, [])
            if link.prev_head != prev:
                return False, f"{link.day}: 链头不连续"
            if link.root != merkle_root([object_hash(o) for o in objs]) or link.count != len(objs):
                return False, f"{link.day}: 对象集与登记根不符（疑似篡改）"
            prev = link.head
        return True, ""
