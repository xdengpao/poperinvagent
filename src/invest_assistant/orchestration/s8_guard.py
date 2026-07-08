"""S8 决策关联校验（任务 13.6/14，需求 R15.7；框架附录 D-4 可执行化、7.4 决策记录、10.3 执行边界）。

录入买入/加仓类决策记录（扩权类）时校验：
①关联意见编号存在；②意见状态为 active；③未过有效期；④对应论点卡已签认（R13.2）。
任一不满足 → 返回拒绝对象（:class:`S8Verdict`，含理由清单——逐条报告而非遇错即停，design §3.6）。

减仓/卖出/清仓类（防守类，铁律 A5 方向）不受论点卡签认约束，
但仍须关联有效意见或框架触发来源（6.6 顺位触发/trigger_event 引用）。

本守卫为纯函数：不写库、不产生副作用；decision_record 落库与冷静期/次数预算/红线扫描
的联立在 P2 任务 18 接入（design §3.6）。拒绝事件由调用方写 violation_ledger（R18.3）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..analysis.thesis import ThesisCard, ThesisState
from ..opinion.factory import Opinion, OpinionStatus


class DecisionAction(StrEnum):
    """S8 决策动作类别（P1 范围：扩权两类 + 防守三类；其余动作 P2 扩展）。"""

    BUY = "买入"
    ADD = "加仓"
    REDUCE = "减仓"
    SELL = "卖出"
    LIQUIDATE = "清仓"


EXPANSION_ACTIONS = frozenset({DecisionAction.BUY, DecisionAction.ADD})
"""扩权类：受论点卡签认约束（R13.2/R15.7），且被冻结矩阵阻断（R6.3）。"""

DEFENSIVE_ACTIONS = frozenset({DecisionAction.REDUCE, DecisionAction.SELL, DecisionAction.LIQUIDATE})
"""防守类：不受论点卡签认约束，不被任何冻结阻断（铁律 A5）。"""


@dataclass(frozen=True)
class DecisionDraft:
    """待录入的决策记录草稿（附录 B 最小子集：标的、动作、关联来源）。"""

    symbol: str
    action: DecisionAction
    opinion_id: str = ""  # 所依据的系统意见编号（框架 7.4 必填字段之一）
    framework_trigger: str = ""  # 框架触发来源（6.6 顺位触发 / trigger_event 引用）


@dataclass(frozen=True)
class OpinionRecord:
    """意见登记视图：意见对象（frozen）+ 生命周期状态（opinion 表 status 列）。"""

    opinion: Opinion
    status: OpinionStatus = OpinionStatus.ACTIVE


@dataclass(frozen=True)
class S8Verdict:
    """校验结论：admitted=False 时 reasons 为完整拒绝理由清单（R15.7）。"""

    admitted: bool
    reasons: tuple[str, ...] = ()


def check_s8_admission(
    draft: DecisionDraft,
    *,
    opinions: Mapping[str, OpinionRecord],
    thesis_cards: Mapping[str, ThesisCard],
    on_date: dt.date,
) -> S8Verdict:
    """S8 录入守卫（R15.7）：返回放行/拒绝结论，拒绝时附全部理由。"""
    reasons: list[str] = []

    if draft.opinion_id:
        record = opinions.get(draft.opinion_id)
        if record is None:
            reasons.append(f"关联意见编号 {draft.opinion_id} 不存在（R15.7）")
        else:
            if record.status is not OpinionStatus.ACTIVE:
                reasons.append(f"关联意见 {draft.opinion_id} 状态非 active（当前 {record.status}，R15.7）")
            if on_date > record.opinion.valid_until:
                reasons.append(
                    f"关联意见 {draft.opinion_id} 已过有效期（{record.opinion.valid_until}，R14.2/R15.7）"
                )
            if record.opinion.symbol != draft.symbol:
                reasons.append(
                    f"关联意见标的 {record.opinion.symbol} 与决策标的 {draft.symbol} 不一致（附录 D-4）"
                )

    if draft.action in EXPANSION_ACTIONS:
        if not draft.opinion_id:
            reasons.append("买入/加仓类决策必须关联系统意见编号（框架 7.4/附录 D-4，R15.7）")
        card = thesis_cards.get(draft.symbol)
        if card is None:
            reasons.append(f"标的 {draft.symbol} 无论点卡——签认前不得放行买入/加仓（R13.2）")
        elif card.state is not ThesisState.SIGNED:
            reasons.append(f"标的 {draft.symbol} 论点卡未签认（当前 {card.state}，R13.2/R15.7）")
    else:
        # 防守类（A5）：不受论点卡签认约束，但仍须关联有效意见或框架触发来源。
        if not draft.opinion_id and not draft.framework_trigger:
            reasons.append("防守类决策仍须关联有效意见或框架触发来源（R15.7/附录 D-4 计划外交易红线）")

    return S8Verdict(admitted=not reasons, reasons=tuple(reasons))
