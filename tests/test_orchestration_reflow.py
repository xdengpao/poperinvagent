"""滞留/裁决回流控制器（任务 13.4，需求 R6.6/R6.7/R20.5）。"""

import pytest

from invest_assistant.orchestration.freeze import ACTION_ADD, ActionClass, FreezeFlags, freeze_gate
from invest_assistant.orchestration.reflow import (
    EliminationRecord,
    ReentryInstruction,
    ReflowController,
    RerunRequest,
    StrandedStatus,
    Suspension,
)


def test_gap_closed_triggers_rerun():
    """R6.6：缺口关闭事件到达 → 补取证重跑请求；非滞留标的无补跑。"""
    c = ReflowController()
    c.register_stranded("AEHR", "2026-01", gap_fields=("audit_opinion",))
    req = c.on_gap_closed("AEHR", "2026-01")
    assert isinstance(req, RerunRequest) and req.cause == "gap_closed"
    assert c.on_gap_closed("UNKNOWN", "2026-01") is None


def test_next_period_scan_reruns_below_threshold():
    """R6.6：下期扫描对仍滞留标的触发补跑（阈值内）。"""
    c = ReflowController()
    rec = c.register_stranded("AEHR", "2026-01")
    out2 = c.on_next_period_scan("2026-02")
    out3 = c.on_next_period_scan("2026-03")
    assert all(isinstance(x, RerunRequest) and x.cause == "next_period_scan" for x in out2 + out3)
    assert rec.periods_stranded == 2 and rec.status == StrandedStatus.STRANDED


def test_r205_three_periods_stranded_becomes_elimination():
    """R20.5：连续 3 期仍滞留 → 转淘汰并留痕（R6.6，第 N+3 期）。"""
    c = ReflowController()  # 默认阈值 3
    c.register_stranded("AEHR", "2026-01")
    c.on_next_period_scan("2026-02")
    c.on_next_period_scan("2026-03")
    out = c.on_next_period_scan("2026-04")
    assert len(out) == 1 and isinstance(out[0], EliminationRecord)
    elim = out[0]
    assert elim.subject == "AEHR"
    assert elim.stranded_since_period == "2026-01" and elim.eliminated_period == "2026-04"
    assert elim.periods_stranded == 3 and "R6.6" in elim.reason
    assert elim in c.log  # 转淘汰留痕
    assert c.stranded_record("AEHR").status == StrandedStatus.ELIMINATED
    assert c.on_next_period_scan("2026-05") == []  # 已淘汰不再扫描


def test_threshold_parameterized():
    """R6.6：淘汰阈值参数化（默认 3）。"""
    c = ReflowController(elimination_threshold=2)
    c.register_stranded("MPWR", "2026-01")
    assert isinstance(c.on_next_period_scan("2026-02")[0], RerunRequest)
    assert isinstance(c.on_next_period_scan("2026-03")[0], EliminationRecord)
    with pytest.raises(ValueError):
        ReflowController(elimination_threshold=0)


def test_resolve_stops_rerun_and_elimination():
    """R6.6：滞留解除后不再补跑、不再计淘汰期次。"""
    c = ReflowController()
    c.register_stranded("SNPS", "2026-01")
    c.on_next_period_scan("2026-02")
    c.resolve("SNPS", "2026-02", note="补取证成功，缺口闭合")
    assert c.on_next_period_scan("2026-03") == []
    assert c.stranded_record("SNPS").status == StrandedStatus.RESOLVED


def test_r205_pending_ruling_suspends_without_duplicate_dispatch():
    """R20.5：重跑遇未决裁决 → 挂起本期流转，幂等不重复派发裁决请求（R6.7）。"""
    flags = FreezeFlags()
    c = ReflowController(flags=flags)
    c.register_stranded("MPWR", "2026-01")
    susp1, req1 = c.request_ruling("MPWR", "audit_opinion_conflict", "2026-01")
    assert isinstance(susp1, Suspension) and req1 is not None
    assert req1.idempotency_key == "MPWR:audit_opinion_conflict"
    susp2, req2 = c.request_ruling("MPWR", "audit_opinion_conflict", "2026-02")
    assert isinstance(susp2, Suspension) and req2 is None  # 幂等：不重复派发
    # 裁决未决 → 标的冻结：扩权类被阻断，防守类照常放行（A5）
    assert flags.instrument_frozen("MPWR")
    assert not freeze_gate(ACTION_ADD, ActionClass.EXPANSIVE, flags, subject="MPWR").allowed
    assert freeze_gate("stop_loss_halve", ActionClass.DEFENSIVE, flags, subject="MPWR").allowed
    # 未决期间的下期扫描：只产挂起，不补跑、不计淘汰期次
    out = c.on_next_period_scan("2026-02")
    assert len(out) == 1 and isinstance(out[0], Suspension)
    assert c.stranded_record("MPWR").periods_stranded == 0


def test_gap_closed_during_pending_ruling_suspends():
    """R6.7：未决裁决期间缺口关闭事件同样挂起，不产补跑。"""
    c = ReflowController()
    c.register_stranded("MPWR", "2026-01")
    c.request_ruling("MPWR", "audit_opinion_conflict", "2026-01")
    result = c.on_gap_closed("MPWR", "2026-01")
    assert isinstance(result, Suspension)


def test_ruling_completed_clears_freeze_and_reenters_s3():
    """R6.7：裁决完成事件 → 清除对应冻结 + 重入 S3 指令；幂等键随闭环释放。"""
    flags = FreezeFlags()
    c = ReflowController(flags=flags)
    c.register_stranded("MPWR", "2026-01")
    c.request_ruling("MPWR", "audit_opinion_conflict", "2026-01")
    instruction = c.on_ruling_completed("MPWR", "audit_opinion_conflict", "RUL-2026-001")
    assert isinstance(instruction, ReentryInstruction)
    assert instruction.stage == "S3" and instruction.ruling_id == "RUL-2026-001"
    assert not flags.instrument_frozen("MPWR")  # 冻结已清除
    assert instruction in c.log  # 回流留痕
    # 闭环后同一标的的新争点可再次派发（幂等键已释放）
    _, req = c.request_ruling("MPWR", "audit_opinion_conflict", "2026-02")
    assert req is not None


def test_multiple_pending_rulings_clear_only_when_all_closed():
    """R6.7：同标的多争点未决时，冻结在全部裁决闭环后才清除。"""
    flags = FreezeFlags()
    c = ReflowController(flags=flags)
    c.request_ruling("SNPS", "issue_a", "2026-01")
    c.request_ruling("SNPS", "issue_b", "2026-01")
    c.on_ruling_completed("SNPS", "issue_a", "RUL-2026-002")
    assert flags.instrument_frozen("SNPS")  # issue_b 仍未决
    c.on_ruling_completed("SNPS", "issue_b", "RUL-2026-003")
    assert not flags.instrument_frozen("SNPS")
