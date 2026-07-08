"""数据源角色配置表与 FMP 切换位（任务 15，需求 R1.4）。

源角色（主/备/仲裁）是配置数据而非代码分支（design §3.1）：configure_fmp_token
事件把 D2/D4 美股主源改写为 FMP、EDGAR 转备+仲裁，判定路径不改代码；
数值冲突时以 EDGAR 法定披露为准（arbiter 标志）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    PRIMARY = "primary"
    BACKUP = "backup"
    ARBITER = "arbiter"


@dataclass(frozen=True)
class RoleAssignment:
    domain: str      # D1..D10
    market: str      # US | CN
    role: Role
    adapter: str
    valid_from: str  # ISO 日期
    valid_to: str | None = None


@dataclass
class SourceRoleTable:
    """版本化角色表：同 (domain, market, role) 以生效区间取当日有效条目。"""

    assignments: list[RoleAssignment] = field(default_factory=list)

    def assign(self, a: RoleAssignment) -> None:
        # 关闭同 (domain, market, role) 的开口旧条目
        for i, existing in enumerate(self.assignments):
            if (existing.domain == a.domain and existing.market == a.market
                    and existing.role == a.role and existing.valid_to is None):
                self.assignments[i] = RoleAssignment(
                    existing.domain, existing.market, existing.role, existing.adapter,
                    existing.valid_from, valid_to=a.valid_from)
        self.assignments.append(a)

    def resolve(self, domain: str, market: str, on: str) -> dict[str, str]:
        """返回 {role: adapter} 当日有效映射。"""
        d = _dt.date.fromisoformat(on)
        out: dict[str, str] = {}
        for a in self.assignments:
            if a.domain != domain or a.market != market:
                continue
            start = _dt.date.fromisoformat(a.valid_from)
            end = _dt.date.fromisoformat(a.valid_to) if a.valid_to else None
            if start <= d and (end is None or d < end):
                out[a.role.value] = a.adapter
        return out


def default_us_table(on: str = "2026-07-07") -> SourceRoleTable:
    """签署默认：美股 D2/D4 主源 EDGAR（FMP 未配置，决策 4）。"""
    t = SourceRoleTable()
    for domain in ("D2", "D4"):
        t.assign(RoleAssignment(domain, "US", Role.PRIMARY, "ADP-EDGAR", on))
    return t


def configure_fmp_token(table: SourceRoleTable, *, on: str) -> SourceRoleTable:
    """FMP token 配置事件（R1.4，签署决策 4）：
    D2/D4 美股主源升 FMP、EDGAR 转备源+仲裁源；冲突以 EDGAR 法定披露为准。
    """
    for domain in ("D2", "D4"):
        table.assign(RoleAssignment(domain, "US", Role.PRIMARY, "ADP-FMP", on))
        table.assign(RoleAssignment(domain, "US", Role.BACKUP, "ADP-EDGAR", on))
        table.assign(RoleAssignment(domain, "US", Role.ARBITER, "ADP-EDGAR", on))
    return table


def arbiter_wins_on_conflict(table: SourceRoleTable, domain: str, market: str, on: str) -> str | None:
    """数值冲突仲裁源（EDGAR 法定披露优先，R1.4）。"""
    return table.resolve(domain, market, on).get(Role.ARBITER.value)
