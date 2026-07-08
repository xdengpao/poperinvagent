"""规则引擎（任务 8.1，需求 R5.1）。

规则条目是数据（YAML/表），引擎是解释器：输入（规则 + 证据 + 参数）→
输出（判定 + A4 三元组引用）。代码不硬编码业务阈值（红线断言层除外）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RuleEntry:
    """规则条目：框架条文的机器可执行表示。"""

    rule_id: str
    source: str  # 框架出处，如 "F 3.1"
    rule_type: str  # threshold | guard | trigger-action | assertion | classification
    params: dict
    effect_level: int  # 效力级：4=红线 3=第六章 2=第三章 1=第四章（R5.3）
    conservative_direction: str = ""
    version: str = "v1"


class RuleRepository:
    """从 YAML 规则库装载条目（rules/library/*.yaml）。"""

    def __init__(self, library_dir: Path):
        self._rules: dict[str, RuleEntry] = {}
        for f in sorted(library_dir.glob("*.yaml")):
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for raw in doc.get("rules", []):
                entry = RuleEntry(
                    rule_id=raw["id"],
                    source=raw["source"],
                    rule_type=raw["type"],
                    params=raw.get("params", {}),
                    effect_level=int(raw.get("effect_level", 2)),
                    conservative_direction=raw.get("conservative_direction", ""),
                    version=str(raw.get("version", "v1")),
                )
                if entry.rule_id in self._rules:
                    raise ValueError(f"规则 ID 重复：{entry.rule_id}")
                self._rules[entry.rule_id] = entry

    def get(self, rule_id: str) -> RuleEntry:
        if rule_id not in self._rules:
            raise KeyError(f"规则库无此条目：{rule_id}（无规则不判定，A1）")
        return self._rules[rule_id]

    def all(self) -> list[RuleEntry]:
        return list(self._rules.values())

    def coverage_matrix(self) -> list[dict]:
        """条文↔规则条目映射（R20.6 覆盖矩阵的规则列）。"""
        return [{"rule_id": r.rule_id, "source": r.source, "type": r.rule_type} for r in self.all()]


@dataclass
class Verdict:
    """判定产物：结论 + 三元组引用（A4）。"""

    rule_id: str
    subject: str
    passed: bool
    detail: dict = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    snapshot_id: str = ""


def arbitrate(entries: list[RuleEntry]) -> RuleEntry:
    """效力仲裁（R5.3）：红线 > 第六章 > 第三章 > 第四章；同级由调用方取严。"""
    if not entries:
        raise ValueError("空规则集不可仲裁")
    top = max(e.effect_level for e in entries)
    winners = [e for e in entries if e.effect_level == top]
    if len(winners) > 1:
        raise ConflictUnresolved(winners)
    return winners[0]


class ConflictUnresolved(Exception):
    """同级冲突无法仲裁 → 冻结扩权类 + 裁决请求（R5.3），由编排层捕获。"""

    def __init__(self, entries: list[RuleEntry]):
        self.entries = entries
        super().__init__(f"同级规则冲突待裁决：{[e.rule_id for e in entries]}")
