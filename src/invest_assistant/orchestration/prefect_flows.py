"""Prefect 调度迁移（P3 任务 27，需求 R6.8；规格书 §14/§15 P2 调度器迁移评估）。

将日历任务从 APScheduler 薄壳迁移到 Prefect flow/task。核心到期逻辑仍复用
``scheduler.Scheduler.due_tasks``（单一事实来源，不重复实现），Prefect 只负责
编排、重试、可观测与部署调度。设计取舍：触发器总线自持久化（R6.4），不依赖
调度器可靠性——Prefect 崩溃后由 trigger_event 重放兜底，与 APScheduler 语义一致。

Prefect 为可选依赖（extras: app 之外的 orchestration）；未安装时本模块的纯逻辑
（``daily_due_task_names`` 等）仍可用，flow 定义在导入 Prefect 成功时才注册。
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable

from .scheduler import DueTask, Scheduler

try:  # Prefect 为可选依赖
    from prefect import flow, task

    _PREFECT = True
except ImportError:  # pragma: no cover - 环境无 Prefect 时降级为普通函数
    _PREFECT = False

    def task(fn: Callable | None = None, **_kw):  # type: ignore[misc]
        def wrap(f):
            return f
        return wrap(fn) if fn else wrap

    def flow(fn: Callable | None = None, **_kw):  # type: ignore[misc]
        def wrap(f):
            return f
        return wrap(fn) if fn else wrap


#: Prefect 部署调度：日历任务的 cron 表达式（交易日修正在 flow 内按日历过滤）
DEPLOYMENT_SCHEDULES: dict[str, str] = {
    "daily_calendar": "0 22 * * *",       # 每日 22:00（美东收盘后账务日批次）
    "eod_reconcile": "30 22 * * *",       # 日终对账
    "audit_chain": "0 23 * * *",          # 审计链每日哈希
}


def _default_scheduler() -> Scheduler:
    return Scheduler()


# ---------- 纯逻辑（不依赖 Prefect 运行时；flow/task 仅薄包装以便测试与降级） ----------


def _compute_due(on_iso: str, scheduler: Scheduler | None = None) -> list[dict]:
    """核心到期计算（复用 Scheduler.due_tasks，单一事实来源）。"""
    sched = scheduler or _default_scheduler()
    on = _dt.date.fromisoformat(on_iso)
    return [{"kind": t.kind, "due": t.due.isoformat(), "note": t.note} for t in sched.due_tasks(on)]


def _run_daily(on_iso: str, sink: Callable[[dict], None] | None = None) -> list[str]:
    """每日流程主体：计算到期任务并投递（纯函数，Prefect 内外一致）。"""
    due = _compute_due(on_iso)
    for d in due:
        if sink is not None:
            sink(d)
    return [d["kind"] for d in due]


@task(name="compute-due-tasks", retries=2, retry_delay_seconds=30)
def compute_due_tasks(on_iso: str) -> list[dict]:
    """Prefect task：计算给定日期应产生的日历任务。"""
    return _compute_due(on_iso)


@flow(name="daily-calendar-flow")
def daily_calendar_flow(on_iso: str, sink: Callable[[dict], None] | None = None) -> list[str]:
    """Prefect flow：每日计算并投递到期日历任务（R6.8）。

    作为 Prefect deployment 按 DEPLOYMENT_SCHEDULES 触发；也可直接调用（测试/回补）。
    """
    return _run_daily(on_iso, sink)


def daily_due_task_names(on: _dt.date, scheduler: Scheduler | None = None) -> list[str]:
    """给定日期的到期任务种类（纯函数，Prefect 内外一致）。"""
    sched = scheduler or _default_scheduler()
    return [t.kind for t in sched.due_tasks(on)]


def as_due_tasks(on: _dt.date, scheduler: Scheduler | None = None) -> list[DueTask]:
    sched = scheduler or _default_scheduler()
    return sched.due_tasks(on)


def prefect_available() -> bool:
    """运行时是否装有 Prefect（决定 flow 是否为真 Prefect flow）。"""
    return _PREFECT
