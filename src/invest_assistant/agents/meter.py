"""LLM 用量计量与月预算护栏（补齐任务 10.6 缺口；需求 R19.5；优化 L6）。

逐任务计量（档位×token）→ 月度汇总 → 预算告警。超预算的护栏语义对齐 A5 精神：
**暂停扩权类取证派发（新候选尽调），防守类取证（持仓信号/证伪相关）照常**——
成本控制不得削弱风险监测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tasks import AgentTaskType, ModelTier

#: 防守类取证任务：预算超限时仍照常派发（A5 精神）
DEFENSIVE_TASK_TYPES: frozenset[AgentTaskType] = frozenset({
    AgentTaskType.HOLDING_SIGNAL_FORENSICS,   # 持仓恶化/泡沫信号取证（R12）
    AgentTaskType.DISRUPTION_CRITERIA_FORENSICS,  # 颠覆判据（持仓强制复评输入）
})


@dataclass(frozen=True)
class UsageRecord:
    task_id: str
    task_type: AgentTaskType
    tier: ModelTier
    month: str  # "YYYY-MM"
    tokens: int


@dataclass(frozen=True)
class BudgetAlert:
    month: str
    spent: int
    budget: int
    over: bool


@dataclass
class TokenMeter:
    """用量计量：record() 由执行器逐任务调用；monthly_budget=0 表示未设预算。"""

    monthly_budget: int = 0
    records: list[UsageRecord] = field(default_factory=list)

    def record(self, task_id: str, task_type: AgentTaskType, tier: ModelTier,
               month: str, tokens: int) -> BudgetAlert | None:
        if tokens < 0:
            raise ValueError("token 数不可为负")
        self.records.append(UsageRecord(task_id, task_type, tier, month, tokens))
        if self.monthly_budget and self.month_total(month) > self.monthly_budget:
            return BudgetAlert(month, self.month_total(month), self.monthly_budget, over=True)
        return None

    def month_total(self, month: str) -> int:
        return sum(r.tokens for r in self.records if r.month == month)

    def by_tier(self, month: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.records:
            if r.month == month:
                out[r.tier.value] = out.get(r.tier.value, 0) + r.tokens
        return out

    def dispatch_allowed(self, task_type: AgentTaskType, month: str) -> bool:
        """预算护栏：超预算 → 暂停扩权类取证；防守类照常（A5 精神）。"""
        if not self.monthly_budget or self.month_total(month) <= self.monthly_budget:
            return True
        return task_type in DEFENSIVE_TASK_TYPES
