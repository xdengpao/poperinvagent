"""Prefect 调度迁移（P3 任务 27，需求 R6.8）。

验证 flow/task 复用 Scheduler 的到期逻辑、与 APScheduler 语义一致；
Prefect 装或未装均可运行（可选依赖降级）。
"""

import datetime as dt

import pytest

from invest_assistant.orchestration.prefect_flows import (
    DEPLOYMENT_SCHEDULES,
    _run_daily,
    daily_calendar_flow,
    daily_due_task_names,
    prefect_available,
)
from invest_assistant.orchestration.scheduler import Scheduler


def test_flow_matches_scheduler_semantics():
    """Prefect flow 主体的到期集与 APScheduler 薄壳一致（单一事实来源）。"""
    on = dt.date(2026, 10, 1)  # 季度首日
    via_flow = set(daily_due_task_names(on))
    via_scheduler = {t.kind for t in Scheduler().due_tasks(on)}
    assert via_flow == via_scheduler
    assert "quarterly_rescore" in via_flow and "monthly_scan" in via_flow


def test_run_daily_emits_to_sink():
    """flow 主体（纯逻辑）：计算并投递到期任务；不依赖 Prefect 运行时。"""
    emitted: list[dict] = []
    result = _run_daily("2026-08-01", emitted.append)
    assert "monthly_scan" in result
    assert emitted and all("kind" in e and "due" in e for e in emitted)
    # 非月初日无月度任务
    assert "monthly_scan" not in _run_daily("2026-08-15")


def test_deployment_schedules_cover_core():
    assert "daily_calendar" in DEPLOYMENT_SCHEDULES
    assert all(len(cron.split()) == 5 for cron in DEPLOYMENT_SCHEDULES.values())  # 合法 cron


@pytest.mark.skipif(not prefect_available(), reason="Prefect 未安装（可选依赖，extras: orchestration）")
def test_daily_calendar_flow_is_real_prefect_flow():
    """装有 Prefect → daily_calendar_flow 为真 Prefect Flow 对象（可部署调度）。"""
    from prefect import Flow
    assert isinstance(daily_calendar_flow, Flow)
    assert daily_calendar_flow.name == "daily-calendar-flow"


def test_degrades_without_prefect():
    """无 Prefect 时纯逻辑仍可用（降级路径，R6.8 单一事实来源不依赖调度器）。"""
    # _run_daily / daily_due_task_names 不依赖 Prefect 运行时，恒可调用
    assert daily_due_task_names(dt.date(2026, 8, 1))  # 月初有任务
