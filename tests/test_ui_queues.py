"""签认/裁决队列 + S0 准入（任务 14.2/14.3，需求 R17.1/R17.2/R17.4）。"""

import datetime as dt

import pytest

from invest_assistant.ui.queues import (
    QueueAction,
    ReasonRequired,
    RulingItem,
    RulingQueue,
    SignoffItem,
    SignoffItemType,
    SignoffQueue,
    TightenOnlyRejected,
    s0_admission,
)

CLK = lambda: dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.UTC)  # noqa: E731


def _item(item_id="i1", period="2026Q2", itype=SignoffItemType.QUALITATIVE_REVIEW):
    return SignoffItem(item_id=item_id, item_type=itype, subject="AMD",
                       payload={"score": 12}, period=period)


def test_signoff_approve_leaves_record():
    q = SignoffQueue(clock=CLK)
    q.submit(_item())
    rec = q.act("i1", QueueAction.APPROVE, actor="user")
    assert rec.action == "approve" and not q.pending()


def test_reject_requires_reason():
    q = SignoffQueue(clock=CLK)
    q.submit(_item())
    with pytest.raises(ReasonRequired):
        q.act("i1", QueueAction.REJECT, actor="user")


def test_modify_requires_tighten_hook():
    q = SignoffQueue(clock=CLK)
    q.submit(_item())
    with pytest.raises(TightenOnlyRejected):
        q.act("i1", QueueAction.MODIFY, actor="user", changes={"score": 8})
    # 注册 tighten 钩子（只可更保守：分数只降）
    q.register_tighten_hook(SignoffItemType.QUALITATIVE_REVIEW,
                            lambda payload, changes: changes.get("score", 99) <= payload["score"])
    q.submit(_item(item_id="i2"))
    assert q.act("i2", QueueAction.MODIFY, actor="user", changes={"score": 8}).action == "modify"


def test_blind_follow_alert_on_zero_rejection():
    q = SignoffQueue(clock=CLK)
    periods = [f"2026Q{i}" for i in range(4)]
    for i, p in enumerate(periods):
        q.submit(_item(item_id=f"a{i}", period=p))
        q.act(f"a{i}", QueueAction.APPROVE, actor="user")
    alert = q.blind_follow_alert(periods, required_zero_periods=4)
    assert alert is not None  # 连续 4 期驳回率为 0 → 盲从提示


def test_ruling_queue_requires_four_elements():
    q = RulingQueue(clock=CLK)
    item = RulingItem(item_id="r1", subject="NVDA", issue="2022 SEC 披露和解性质",
                      evidence_pro=("e1",), evidence_con=("e2",),
                      default_disposition="裁决前冻结于 S3 出口",
                      review_condition="2027-05 窗口滚出复审")
    q.submit(item)
    assert q.pending()[0].item_id == "r1"
    with pytest.raises(ValueError):
        RulingItem(item_id="r2", subject="X", issue="", evidence_pro=(), evidence_con=(),
                   default_disposition="", review_condition="")


# ---------- S0 准入硬拒绝（R17.4）----------


def test_s0_admission_hard_reject_four_types():
    for fund in ("杠杆资金", "借贷资金", "限期翻倍资金", "期货期权投机资金"):
        d = s0_admission([fund])
        assert not d.admitted and d.mode == "learning_only" and fund in d.hit_types


def test_s0_admission_clean_passes():
    d = s0_admission(["自有闲置资金"])
    assert d.admitted and d.mode == "invest"
