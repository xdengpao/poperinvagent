"""日历任务调度（任务 13.5，需求 R6.8）。

日历任务清单为配置数据（FR-O-01 全项），到期计算依赖交易日历服务；
执行循环（__main__）为薄壳：计算到期 → 产任务对象 → 心跳上报（R19.1）。
调度器崩溃恢复依赖 trigger_event 持久化重放（R6.4），本模块无状态。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from ..calendar import CalendarService

#: FR-O-01 日历任务清单（R6.8）——cadence: monthly|quarterly|annual|daily
CALENDAR_TASKS: tuple[dict, ...] = (
    {"kind": "monthly_scan", "cadence": "monthly", "day": 1, "note": "月度全量扫描（S1-S5 管道）"},
    {"kind": "monthly_hold_confirm", "cadence": "monthly", "day": 1,
     "note": "月度持仓持有确认（未触发动作持仓生成'持有'意见并过 10.2 校验）"},
    {"kind": "quarterly_rescore", "cadence": "quarterly", "note": "季度重打分（财报季后 10 交易日内完成）"},
    {"kind": "quarterly_disruption_review", "cadence": "quarterly", "note": "季度颠覆名单复审"},
    {"kind": "review_monthly", "cadence": "monthly", "day": 28, "note": "月度复盘"},
    {"kind": "review_quarterly", "cadence": "quarterly", "note": "季度复盘"},
    {"kind": "review_annual", "cadence": "annual", "note": "年度复盘"},
    {"kind": "opinion_lookback_90d", "cadence": "daily", "note": "90 天回看到期任务生成"},
    {"kind": "opinion_expiry", "cadence": "daily", "note": "意见到期作废扫描"},
    {"kind": "quarterly_calibration", "cadence": "quarterly", "note": "季度意见质量校准"},
    {"kind": "ruling_review_scan", "cadence": "daily", "note": "裁决条件化复核到期扫描（R5.6）"},
    {"kind": "eod_reconcile", "cadence": "daily", "note": "日终应触发 vs 已处理对账（R6.4）"},
    {"kind": "audit_chain_daily", "cadence": "daily", "note": "审计链每日哈希批任务（R18.2）"},
)

_QUARTER_MONTHS = (1, 4, 7, 10)


@dataclass(frozen=True)
class DueTask:
    kind: str
    due: _dt.date
    note: str


@dataclass
class Heartbeat:
    """调度心跳（R19.1）：超过间隔未上报 → 可观测性层告警。"""

    last_beat: _dt.datetime | None = None
    interval_minutes: int = 10

    def beat(self, now: _dt.datetime) -> None:
        self.last_beat = now

    def is_stale(self, now: _dt.datetime) -> bool:
        if self.last_beat is None:
            return True
        return (now - self.last_beat).total_seconds() > self.interval_minutes * 60


@dataclass
class Scheduler:
    calendar: CalendarService = field(default_factory=CalendarService)
    heartbeat: Heartbeat = field(default_factory=Heartbeat)

    def due_tasks(self, on: _dt.date) -> list[DueTask]:
        """给定日期应产生的任务集（幂等：由消费方按 (kind, due) 去重）。"""
        out: list[DueTask] = []
        for spec in CALENDAR_TASKS:
            cadence = spec["cadence"]
            if cadence == "daily":
                out.append(DueTask(spec["kind"], on, spec["note"]))
            elif cadence == "monthly" and on.day == int(spec.get("day", 1)):
                out.append(DueTask(spec["kind"], on, spec["note"]))
            elif cadence == "quarterly" and on.month in _QUARTER_MONTHS and on.day == 1:
                out.append(DueTask(spec["kind"], on, spec["note"]))
            elif cadence == "annual" and on.month == 1 and on.day == 1:
                out.append(DueTask(spec["kind"], on, spec["note"]))
        return out

    def expected_daily_kinds(self) -> set[str]:
        """日终对账用：当日预期任务种类集（R19.1"预期任务 vs 实际执行"）。"""
        return {s["kind"] for s in CALENDAR_TASKS if s["cadence"] == "daily"}


def main() -> None:  # pragma: no cover —— 生产薄壳（Docker scheduler 容器入口）
    import time

    sched = Scheduler()
    while True:
        now = _dt.datetime.now(tz=_dt.UTC)
        for task in sched.due_tasks(now.date()):
            print(f"[scheduler] due: {task.kind} {task.due} — {task.note}", flush=True)
        sched.heartbeat.beat(now)
        time.sleep(60)


if __name__ == "__main__":  # pragma: no cover
    main()
