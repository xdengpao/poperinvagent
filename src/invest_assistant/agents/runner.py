"""代理执行器（agent-runner 容器入口；任务 10 执行侧，需求 R16）。

与判定进程物理隔离（A1）：本进程只有 evidence 写入通道——
LLM 执行器可注入（生产=Agent SDK 按模板档位路由尽调级/抽取级，R16.7；
测试=fake），产物一律构造 EvidenceDraft 过防幻觉四段链后落库；
失败走 dispose 处置链（Gap→一次补取证→滞留；勾稽矛盾→裁决队列）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..core.types import Evidence
from ..data.quality import GapLedger
from .tasks import AgentTask, TaskManager
from .verify import Disposition, EvidenceDraft, VerificationChain, build_evidence, dispose

#: LLM 执行器签名：任务 → EvidenceDraft 原始字典（claims/来源快照/标签）
LlmExecutor = Callable[[AgentTask], Mapping[str, object]]


@dataclass
class RunnerReport:
    executed: int = 0
    accepted: int = 0
    dispositions: list[Disposition] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class AgentRunner:
    manager: TaskManager
    chain: VerificationChain
    executor: LlmExecutor
    ledger: GapLedger = field(default_factory=GapLedger)
    _eid_seq: int = 0

    def run_task(self, task: AgentTask, evidence_sink: Callable[[Evidence], None]) -> Disposition:
        attempt = task.new_attempt(note="runner 派发")
        raw = self.executor(task)
        draft = EvidenceDraft.from_dict(raw)
        outcome = self.chain.verify(draft)
        self._eid_seq += 1
        ev = build_evidence(draft, outcome, evidence_id=f"EV-AGT-{self._eid_seq:06d}")
        evidence_sink(ev)  # 无论过否均落库留痕（UNVERIFIED 置信由校验器决定，A1）
        return dispose(task, attempt, outcome, self.manager, self.ledger)

    def run(self, tasks: list[AgentTask], evidence_sink: Callable[[Evidence], None]) -> RunnerReport:
        report = RunnerReport()
        for task in tasks:
            d = self.run_task(task, lambda ev: (report.evidence.append(ev), evidence_sink(ev)))
            report.executed += 1
            report.dispositions.append(d)
            if d.action == "accepted":
                report.accepted += 1
        return report


def main() -> None:  # pragma: no cover —— 生产薄壳（Docker agent-runner 容器入口）
    raise SystemExit(
        "agent-runner 需要配置 LLM 执行器（Agent SDK，按模板档位路由）与数据库 evidence 通道后启动。"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
