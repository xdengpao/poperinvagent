"""调度器（任务 13.5，R6.8）与可观测性（任务 21.2，R19.1）。"""

import datetime as dt

from invest_assistant.core.obs import MetricsRegistry, TaskReconciliation, log_event, run_id_var
from invest_assistant.orchestration.scheduler import CALENDAR_TASKS, Heartbeat, Scheduler


def test_calendar_task_list_complete():
    """FR-O-01 全项在册（R6.8）。"""
    kinds = {t["kind"] for t in CALENDAR_TASKS}
    required = {
        "monthly_scan", "monthly_hold_confirm", "quarterly_rescore",
        "quarterly_disruption_review", "review_monthly", "review_quarterly", "review_annual",
        "opinion_lookback_90d", "opinion_expiry", "quarterly_calibration",
    }
    assert required <= kinds


def test_due_tasks_by_cadence():
    s = Scheduler()
    monthly_day = {t.kind for t in s.due_tasks(dt.date(2026, 8, 1))}
    assert "monthly_scan" in monthly_day and "quarterly_rescore" not in monthly_day
    quarter_day = {t.kind for t in s.due_tasks(dt.date(2026, 10, 1))}
    assert "quarterly_rescore" in quarter_day and "quarterly_calibration" in quarter_day
    ordinary = {t.kind for t in s.due_tasks(dt.date(2026, 8, 15))}
    assert "monthly_scan" not in ordinary and "opinion_expiry" in ordinary  # daily 恒在


def test_heartbeat_staleness():
    hb = Heartbeat(interval_minutes=10)
    now = dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.UTC)
    assert hb.is_stale(now)
    hb.beat(now)
    assert not hb.is_stale(now + dt.timedelta(minutes=9))
    assert hb.is_stale(now + dt.timedelta(minutes=11))


def test_structured_log_carries_run_id(capsys):
    token = run_id_var.set("RUN-42")
    try:
        rec = log_event("node_done", node="S1", ms=120)
    finally:
        run_id_var.reset(token)
    assert rec["run_id"] == "RUN-42" and rec["event"] == "node_done"
    assert '"run_id": "RUN-42"' in capsys.readouterr().out


def test_metric_threshold_alert():
    m = MetricsRegistry()
    assert m.record("agent_unverified_rate", 0.1) is None
    alert = m.record("agent_unverified_rate", 0.5)
    assert alert is not None and alert.direction == "above"


def test_eod_task_reconciliation():
    r = TaskReconciliation(expected={"eod_reconcile", "opinion_expiry"}, executed={"opinion_expiry"})
    assert r.missing == {"eod_reconcile"} and not r.clean
