"""适配器框架（任务 4.1，需求 R1.1/R1.2）。

- BaseAdapter：probe（连通+鉴权+权限覆盖率）/ fetch / 限速策略；
- ConnectivityMatrix：启动前置自检产物；生产环境任一 §2.3 清单源失败 →
  拒绝进入意见生成模式（degraded 声明）；IBKR 运行时断连按 R1.5 单列。
- 源角色（主/备/仲裁）是 source_role 配置数据（R1.4 FMP 切换不改代码）。
"""

from __future__ import annotations

import abc
import datetime as _dt
from dataclasses import dataclass, field


@dataclass
class ProbeResult:
    adapter: str
    ok: bool
    authenticated: bool | None = None
    coverage: dict[str, bool] = field(default_factory=dict)  # 接口/权限覆盖率登记（R1.3）
    error: str = ""
    probed_at: str = ""
    required: bool = True  # 生产必需源（§2.3 清单）


@dataclass
class RateLimitPolicy:
    max_per_minute: int = 60
    retry_backoff_s: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)


class UpstreamDown(Exception):
    """上游不可达：编排层据此冻结依赖节点（R1.5）。"""


class BaseAdapter(abc.ABC):
    name: str = "base"
    #: 生产必需源（§2.3 清单）——启动探测失败拒绝意见生成模式（R1.2）
    required_in_production: bool = True

    rate_limit = RateLimitPolicy()

    @abc.abstractmethod
    def probe(self) -> ProbeResult: ...

    @abc.abstractmethod
    def fetch(self, request: dict) -> list[dict]:
        """取数；失败抛 UpstreamDown；数据缺失返回空列表（上层产 Gap，禁止插值）。"""


@dataclass
class ConnectivityMatrix:
    results: list[ProbeResult] = field(default_factory=list)

    @property
    def opinion_mode_allowed(self) -> bool:
        return all(r.ok for r in self.results if r.required)

    def degraded_statement(self) -> list[str]:
        return [f"{r.adapter}: {r.error or '不可达'}" for r in self.results if not r.ok]


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, BaseAdapter] = {}

    def register(self, adapter: BaseAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def startup_self_check(self) -> tuple[bool, list[ProbeResult]]:
        """启动前置自检（R1.1/R1.2）：全清单探测 → (是否允许意见模式, 矩阵)。"""
        results: list[ProbeResult] = []
        allow = True
        for a in self._adapters.values():
            try:
                r = a.probe()
            except Exception as exc:  # noqa: BLE001 —— 探测失败必须落矩阵而非崩溃
                r = ProbeResult(adapter=a.name, ok=False, error=str(exc))
            r.probed_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
            r.required = a.required_in_production
            results.append(r)
            if a.required_in_production and not r.ok:
                allow = False
        return allow, results
