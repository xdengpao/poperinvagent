"""可观测性（任务 21.2，需求 R19.1）。

- 结构化日志：run_id/trigger_id 双关联键（contextvars），JSON lines 输出；
- 指标注册表：核心指标（节点时延/失败率/缺口率/触发延迟/UNVERIFIED 率）
  带阈值 → 告警对象；
- 心跳检查复用 orchestration.scheduler.Heartbeat；
- 日终"预期任务 vs 实际执行"对账。
"""

from __future__ import annotations

import contextvars
import datetime as _dt
import json
import sys
from dataclasses import dataclass, field

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")
trigger_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trigger_id", default="")


def log_event(event: str, **fields: object) -> dict:
    """结构化日志行（JSON lines）：自动携带 run_id/trigger_id 关联键。"""
    record = {
        "ts": _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="milliseconds"),
        "event": event,
        "run_id": run_id_var.get(),
        "trigger_id": trigger_id_var.get(),
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False), file=sys.stdout, flush=True)
    return record


@dataclass
class MetricAlert:
    metric: str
    value: float
    threshold: float
    direction: str  # above | below


@dataclass
class MetricsRegistry:
    """核心指标 + 阈值告警（R19.1）。thresholds: 指标名 → (阈值, 方向)。"""

    thresholds: dict[str, tuple[float, str]] = field(default_factory=lambda: {
        "node_failure_rate": (0.05, "above"),
        "gap_rate_monthly": (0.20, "above"),
        "trigger_latency_seconds": (3600.0, "above"),
        "agent_unverified_rate": (0.30, "above"),
    })
    samples: dict[str, float] = field(default_factory=dict)

    def record(self, metric: str, value: float) -> MetricAlert | None:
        self.samples[metric] = value
        if metric not in self.thresholds:
            return None
        threshold, direction = self.thresholds[metric]
        breached = value > threshold if direction == "above" else value < threshold
        if breached:
            alert = MetricAlert(metric, value, threshold, direction)
            log_event("metric_alert", metric=metric, value=value, threshold=threshold)
            return alert
        return None


@dataclass
class TaskReconciliation:
    """日终"预期任务 vs 实际执行"对账（R19.1）。"""

    expected: set[str]
    executed: set[str]

    @property
    def missing(self) -> set[str]:
        return self.expected - self.executed

    @property
    def unexpected(self) -> set[str]:
        return self.executed - self.expected

    @property
    def clean(self) -> bool:
        return not self.missing and not self.unexpected
