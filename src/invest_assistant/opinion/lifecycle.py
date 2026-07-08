"""意见生命周期与涉标的输出声明（任务 16.3/16.4，需求 R14.4/R14.9）。

- 到期/触发条件消失自动作废 + 通知对象（R14.4）；
- 涉标的输出统一渲染出口恒附固定声明（R14.9/CP-8）——意见、扫描报告、
  候选清单、复盘报告共用；声明不可移除。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from .factory import DISCLAIMER, Opinion, OpinionStatus


@dataclass(frozen=True)
class ExpiryNotice:
    opinion_id: str
    reason: str  # expired | condition_gone
    at: _dt.date


def evaluate_expiry(opinion: Opinion, on: _dt.date, *, condition_still_holds: bool = True) -> ExpiryNotice | None:
    """到期或触发条件消失 → 作废通知（R14.4）。"""
    if on >= opinion.valid_until:
        return ExpiryNotice(opinion.opinion_id, "expired", on)
    if not condition_still_holds:
        return ExpiryNotice(opinion.opinion_id, "condition_gone", on)
    return None


def status_after(opinion: Opinion, on: _dt.date, *, executed: bool = False,
                 condition_still_holds: bool = True) -> OpinionStatus:
    if executed:
        return OpinionStatus.EXECUTED
    if evaluate_expiry(opinion, on, condition_still_holds=condition_still_holds) is not None:
        return OpinionStatus.EXPIRED
    return OpinionStatus.ACTIVE


class DisclaimerRemovalError(Exception):
    pass


def render_output(body: str, *, symbol_involved: bool = True) -> str:
    """涉标的输出统一出口（R14.9）：恒在末尾附固定声明，不可移除。

    扫描报告/候选清单/复盘报告/意见共用；symbol_involved=False（纯宏观无标的）
    仍附声明（CP-8 范围为"涉标的输出"，此处保守恒附）。
    """
    if DISCLAIMER in body:
        # 已含声明则不重复；但校验其为完整模板（防篡改）
        if not body.rstrip().endswith(DISCLAIMER):
            raise DisclaimerRemovalError("声明必须完整附于输出末尾且不可篡改（R14.9/CP-8）")
        return body
    return f"{body}\n\n{DISCLAIMER}"


def assert_disclaimer_present(rendered: str) -> None:
    if DISCLAIMER not in rendered:
        raise DisclaimerRemovalError("涉标的输出缺失非投资建议声明（R14.9/CP-8）")
