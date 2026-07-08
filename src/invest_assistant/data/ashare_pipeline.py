"""A 股全量接入管道路径（P3 任务 24，需求 R8.3；框架 §3.2 A 股口径）。

C 组（A 股）标的在数据通道接入前置缓评（S2）。本模块承载其激活路径：
当某 A 股标的的否决项 A 股口径证据齐备（质押率、12 个月减持次数、审计意见、
增发）时，从缓评态提升为 S3-ready 并以 market="CN" 运行 S3；证据缺失则按
A2 保持缓评并产 Gap，不推断、不放行。

项 3 A 股口径（区别于美股，R9.4）：质押率 >50% 且 12 个月减持 ≥2 次（两条件"且"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..analysis.s3 import CaseInput, ItemInput, S3Engine, VetoVerdict
from ..core.types import Gap


@dataclass(frozen=True)
class AShareEvidence:
    """A 股标的否决项证据（None = 缺口，A2 不推断）。"""

    symbol: str
    # 项 1
    regulatory_fraud_case: bool | None = None
    audit_opinion_adverse: bool | None = None
    auditor_changes_5y_ge2: bool | None = None
    restatement: bool | None = None
    # 项 2
    cash_conversion_lt_0_7_8q: bool | None = None
    ar_growth_gt_revenue_20pp_4q: bool | None = None
    # 项 3（A 股口径：质押 >50% 且 12 月减持 ≥2 次）
    top_holder_pledge_gt_50pct: bool | None = None
    top_holder_sales_12m_ge2: bool | None = None
    # 项 4
    ma_revenue_contribution_gt_50pct_3y: bool | None = None
    margin_below_pre_merger: bool | None = None
    # 项 5
    per_share_metrics_no_growth_3y: bool | None = None
    revenue_growth_via_dilution: bool | None = None


#: 每项否决审查所需的关键证据字段（缺任一关键字段 → 该项无法闭合）
_REQUIRED_BY_ITEM = {
    "item1": ("audit_opinion_adverse", "auditor_changes_5y_ge2"),
    "item2": ("cash_conversion_lt_0_7_8q",),
    "item3": ("top_holder_pledge_gt_50pct", "top_holder_sales_12m_ge2"),
    "item4": ("ma_revenue_contribution_gt_50pct_3y",),
    "item5": ("revenue_growth_via_dilution",),
}


@dataclass
class ActivationResult:
    symbol: str
    activated: bool                       # 证据齐 → 进入 S3
    verdict: VetoVerdict | None = None
    gaps: list[Gap] = field(default_factory=list)
    deferred_reason: str = ""


class AShareActivator:
    """C 组激活器：数据齐则跑 S3（CN 口径），否则缓评留 Gap（A2）。"""

    def __init__(self, s3: S3Engine):
        self._s3 = s3

    def _evidence_complete(self, ev: AShareEvidence) -> tuple[bool, list[Gap]]:
        gaps: list[Gap] = []
        for item, fields in _REQUIRED_BY_ITEM.items():
            for f in fields:
                if getattr(ev, f) is None:
                    gaps.append(Gap(f, ev.symbol,
                                    degradation=f"A 股口径 {item} 关键证据缺失：缓评（A2/F 3.2）"))
        return (not gaps), gaps

    def activate(self, ev: AShareEvidence, snapshot_id: str = "") -> ActivationResult:
        complete, gaps = self._evidence_complete(ev)
        if not complete:
            return ActivationResult(ev.symbol, activated=False, gaps=gaps,
                                    deferred_reason="A 股否决项证据不足，保持缓评（A2）")
        case = CaseInput(
            symbol=ev.symbol, market="CN",
            items={
                "item1": ItemInput({
                    "regulatory_fraud_case": ev.regulatory_fraud_case,
                    "audit_opinion_adverse": ev.audit_opinion_adverse,
                    "auditor_changes_5y_ge2": ev.auditor_changes_5y_ge2,
                    "restatement_8k402": ev.restatement,
                }),
                "item2": ItemInput({
                    "cash_conversion_lt_0_7_8q": ev.cash_conversion_lt_0_7_8q,
                    "ar_growth_gt_revenue_20pp_4q": ev.ar_growth_gt_revenue_20pp_4q,
                }, concluded_not_hit=not any([ev.cash_conversion_lt_0_7_8q,
                                              ev.ar_growth_gt_revenue_20pp_4q])),
                "item3": ItemInput({
                    "top_holder_pledge_gt_50pct": ev.top_holder_pledge_gt_50pct,
                    "top_holder_sales_12m_ge2": ev.top_holder_sales_12m_ge2,
                }),
                "item4": ItemInput({
                    "ma_revenue_contribution_gt_50pct_3y": ev.ma_revenue_contribution_gt_50pct_3y,
                    "margin_below_pre_merger": ev.margin_below_pre_merger,
                }, concluded_not_hit=not ev.ma_revenue_contribution_gt_50pct_3y),
                "item5": ItemInput({
                    "per_share_metrics_no_growth_3y": ev.per_share_metrics_no_growth_3y,
                    "revenue_growth_via_dilution": ev.revenue_growth_via_dilution,
                }, concluded_not_hit=not ev.revenue_growth_via_dilution),
            },
        )
        verdict = self._s3.judge(case, snapshot_id=snapshot_id)
        return ActivationResult(ev.symbol, activated=True, verdict=verdict)
