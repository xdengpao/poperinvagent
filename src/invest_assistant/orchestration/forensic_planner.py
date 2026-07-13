"""S3 取证规划器（token 成本优化 L1/L2/L3/L5，见 docs/token成本优化方案.md）。

在派发任何 LLM 取证前，先用零成本机械条件（派生指标 + EDGAR submissions
items 字段 + Tushare 结构化）运行否决判定：

- 任一机械命中 → **否决短路**：不派发该标的任何取证任务（一票否决语义下
  其余证据不可能改变判定——数学短路，无损），只归档命中路径 + 跳过留痕（A4）；
- 未短路 → 仅对无法机械判定的子项生成取证任务：按**源文档分组**（同一份
  10-K 服务多个子项，L5），缺省**抽取档**、尽调档须显式理由（L3/L6）；
  抽取档结果"疑似命中/边缘"由执行器按 escalation 策略升级尽调档确认。

本模块只做派发决策，不做判定（A1）；判定仍由 analysis.s3.S3Engine 执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agents.tasks import AgentTaskType, ModelTier
from ..analysis.s3 import CaseInput, ItemInput, S3Engine

#: 子条件 → (所属项, 机械可判定?, 源文档组, 非机械时档位, 尽调理由)
#: 机械=True 的子条件由适配器/派生指标直接给出，永不派发代理。
SUB_CONDITION_REGISTRY: dict[str, dict] = {
    # 项 1
    "regulatory_fraud_case": {
        "item": "item1", "mechanical": False, "doc_group": "regulatory_search",
        "tier": ModelTier.EXTRACTION,  # 初筛：监管事件存在性；定性分类命中时升级
        "escalate_on": "suspected_hit",
    },
    "audit_opinion_adverse": {
        "item": "item1", "mechanical": False, "doc_group": "10-K",
        "tier": ModelTier.EXTRACTION,  # 审计意见段落抽取（含 ICFR）
        "escalate_on": "suspected_hit",
    },
    "auditor_changes_5y_ge2": {
        "item": "item1", "mechanical": True,  # 8-K Item 4.01 计数（EDGAR submissions items 字段）
    },
    "restatement_8k402": {
        "item": "item1", "mechanical": True,  # 8-K Item 4.02 存在性
    },
    # 项 2
    "cash_conversion_lt_0_7_8q": {"item": "item2", "mechanical": True},  # XBRL 派生指标
    "ar_growth_gt_revenue_20pp_4q": {"item": "item2", "mechanical": True},
    # 项 3
    "top_holder_pledge_gt_50pct": {"item": "item3", "mechanical": True},  # Tushare（A 股）
    "top_holder_sales_12m_ge2": {"item": "item3", "mechanical": True},
    # 项 4
    "ma_revenue_contribution_gt_50pct_3y": {
        "item": "item4", "mechanical": False, "doc_group": "10-K",
        "tier": ModelTier.EXTRACTION,  # 并购贡献数字抽取（分部/备考披露）
        "escalate_on": "suspected_hit",
    },
    "margin_below_pre_merger": {"item": "item4", "mechanical": True},  # XBRL 利润率序列
    # 项 5
    "per_share_metrics_no_growth_3y": {"item": "item5", "mechanical": True},  # XBRL
    "revenue_growth_via_dilution": {
        "item": "item5", "mechanical": False, "doc_group": "offering_filings",
        "tier": ModelTier.EXTRACTION,  # 424B5/S-3 增发用途语境
        "escalate_on": "suspected_hit",
    },
}


@dataclass(frozen=True)
class ForensicTaskSpec:
    """一条待派发的取证任务：按 doc_group 批组、缺省抽取档（L3/L5/L6）。"""

    symbol: str
    sub_condition: str
    item: str
    task_type: AgentTaskType
    tier: ModelTier
    doc_group: str
    escalate_on: str = ""  # 升级触发（suspected_hit → 尽调档确认）


@dataclass
class ForensicPlan:
    symbol: str
    short_circuit_verdict: str | None  # "veto"=机械命中短路，None=需取证
    tasks: list[ForensicTaskSpec] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (子条件, 原因) 留痕（A4）
    doc_groups: dict[str, list[str]] = field(default_factory=dict)  # 文档组 → 子条件（L5 批组）

    @property
    def dispatch_count(self) -> int:
        return len(self.tasks)


def plan_s3_forensics(
    symbol: str,
    market: str,
    mechanical_conditions: dict[str, bool | None],
    engine: S3Engine,
) -> ForensicPlan:
    """生成 S3 取证计划（L2 短路 + L5 批组）。

    ``mechanical_conditions``：机械子条件的零成本判定结果（None=该机械源缺数据，
    照旧走 Gap 协议——A2 不因优化而放宽）。非机械子条件不应出现在此入参。
    """
    unknown = set(mechanical_conditions) - set(SUB_CONDITION_REGISTRY)
    if unknown:
        raise KeyError(f"未登记的子条件：{sorted(unknown)}（注册表外不判定，A1）")
    non_mech = [k for k in mechanical_conditions
                if not SUB_CONDITION_REGISTRY[k]["mechanical"]]
    if non_mech:
        raise ValueError(f"非机械子条件不得作为机械输入传入：{non_mech}")

    # —— 阶段 0：仅用机械条件跑否决判定（零 token）——
    items: dict[str, dict[str, bool | None]] = {f"item{i}": {} for i in range(1, 6)}
    for cond, spec in SUB_CONDITION_REGISTRY.items():
        items[spec["item"]][cond] = mechanical_conditions.get(cond)  # 非机械=None（未取证）
    case = CaseInput(symbol=symbol, market=market,
                     items={k: ItemInput(v) for k, v in items.items()},
                     evidence_complete=False)  # 阶段 0 永远视为取证未完成
    verdict = engine.judge(case)

    plan = ForensicPlan(symbol=symbol, short_circuit_verdict=None)
    if verdict.verdict == "veto":
        # 机械命中 → 否决短路：其余取证全部跳过（一票否决语义，无损）
        plan.short_circuit_verdict = "veto"
        hit_items = {r.item for r in verdict.items if r.state == "hit"}
        for cond, spec in SUB_CONDITION_REGISTRY.items():
            if not spec["mechanical"] and spec["item"] not in hit_items:
                plan.skipped.append((cond, "否决短路：机械条件已命中，取证不改变判定（L2 留痕）"))
        return plan

    # —— 阶段 1：为无法机械判定的子项生成取证任务（按文档批组，抽取档缺省）——
    for cond, spec in SUB_CONDITION_REGISTRY.items():
        if spec["mechanical"]:
            continue  # 机械子条件永不派发代理（L1）
        task = ForensicTaskSpec(
            symbol=symbol, sub_condition=cond, item=spec["item"],
            task_type=AgentTaskType.VETO_FORENSICS,
            tier=spec["tier"], doc_group=spec["doc_group"],
            escalate_on=spec.get("escalate_on", ""),
        )
        plan.tasks.append(task)
        plan.doc_groups.setdefault(spec["doc_group"], []).append(cond)
    return plan


def estimate_dispatch_savings(plans: list[ForensicPlan]) -> dict:
    """派发口径的节约统计（供季度报告/成本面板）。基线=每标的 5 项各 1 个尽调代理。"""
    baseline = len(plans) * 5
    dispatched = sum(p.dispatch_count for p in plans)
    short_circuited = sum(1 for p in plans if p.short_circuit_verdict)
    return {
        "baseline_tasks": baseline,
        "dispatched_tasks": dispatched,
        "short_circuited_symbols": short_circuited,
        "dispatch_reduction": 1 - dispatched / baseline if baseline else 0.0,
    }
