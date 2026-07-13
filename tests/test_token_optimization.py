"""Token 成本优化机制（docs/token成本优化方案.md L1-L6）——全部无损性断言。"""

import datetime as dt

import pytest

from invest_assistant.agents.meter import TokenMeter
from invest_assistant.agents.tasks import AgentTaskType, ModelTier
from invest_assistant.analysis.s3 import S3Engine
from invest_assistant.analysis.s4_bounds import decide_qualitative_dispatch, qualitative_max
from invest_assistant.core.types import Evidence
from invest_assistant.data.evidence_cache import EvidenceCache
from invest_assistant.data.quality import FieldClass
from invest_assistant.orchestration.forensic_planner import (
    estimate_dispatch_savings,
    plan_s3_forensics,
)
from invest_assistant.rules import load_params, load_rules

TODAY = dt.date(2026, 7, 8)


@pytest.fixture(scope="module")
def rules():
    return load_rules()


@pytest.fixture(scope="module")
def engine(rules):
    return S3Engine(rules)


def _mech(**overrides):
    """全部机械子条件缺省 False（干净标的），按需覆盖。"""
    base = {
        "auditor_changes_5y_ge2": False, "restatement_8k402": False,
        "cash_conversion_lt_0_7_8q": False, "ar_growth_gt_revenue_20pp_4q": False,
        "top_holder_pledge_gt_50pct": False, "top_holder_sales_12m_ge2": False,
        "margin_below_pre_merger": False, "per_share_metrics_no_growth_3y": False,
    }
    base.update(overrides)
    return base


# ---------- L2 短路取证（S3） ----------


def test_mechanical_hit_short_circuits_all_forensics(engine):
    """AEHR 型：现金转化率机械命中 → 否决短路，零取证派发（一票否决语义无损）。"""
    plan = plan_s3_forensics("AEHR", "US", _mech(cash_conversion_lt_0_7_8q=True), engine)
    assert plan.short_circuit_verdict == "veto"
    assert plan.dispatch_count == 0
    assert plan.skipped and all("否决短路" in reason for _, reason in plan.skipped)  # A4 留痕


def test_mechanical_edge_does_not_short_circuit(engine):
    """重述（加严项→裁决路由）机械成立 → 边缘非否决，仍需取证（他项命中可覆盖边缘）。"""
    plan = plan_s3_forensics("X", "US", _mech(restatement_8k402=True), engine)
    assert plan.short_circuit_verdict is None
    assert plan.dispatch_count > 0  # 保守：边缘不短路


def test_clean_candidate_dispatches_only_nonmechanical(engine):
    """干净标的：仅 4 个非机械子项派发（vs 基线 5 个尽调代理），缺省抽取档，按文档批组。"""
    plan = plan_s3_forensics("AMD", "US", _mech(), engine)
    assert plan.short_circuit_verdict is None
    assert plan.dispatch_count == 4  # regulatory/audit_opinion/ma_contribution/dilution_usage
    assert all(t.tier is ModelTier.EXTRACTION for t in plan.tasks)  # L3/L6 抽取档缺省
    assert all(t.escalate_on == "suspected_hit" for t in plan.tasks)  # 存疑必升级
    assert set(plan.doc_groups) == {"regulatory_search", "10-K", "offering_filings"}
    assert len(plan.doc_groups["10-K"]) == 2  # 审计意见+并购贡献共享同一文档（L5）


def test_planner_rejects_bad_inputs(engine):
    with pytest.raises(KeyError):
        plan_s3_forensics("X", "US", {"bogus_condition": True}, engine)
    with pytest.raises(ValueError):
        plan_s3_forensics("X", "US", {"audit_opinion_adverse": True}, engine)  # 非机械不得混入


def test_dispatch_savings_estimate(engine):
    plans = [
        plan_s3_forensics("AEHR", "US", _mech(cash_conversion_lt_0_7_8q=True), engine),  # 短路
        plan_s3_forensics("AMD", "US", _mech(), engine),   # 4 任务
        plan_s3_forensics("AVGO", "US", _mech(), engine),  # 4 任务
    ]
    s = estimate_dispatch_savings(plans)
    assert s["baseline_tasks"] == 15 and s["dispatched_tasks"] == 8
    assert s["short_circuited_symbols"] == 1
    assert s["dispatch_reduction"] == pytest.approx(7 / 15)


# ---------- L2 上界剪枝（S4） ----------


def test_s4_bounds_prunes_hopeless_candidate(rules):
    """量化 35 + 定性满分 25 = 60 < 65 → 判定必然放弃，定性取证跳过（无损剪枝）。"""
    params = load_params()
    assert qualitative_max(rules) == 25
    d = decide_qualitative_dispatch(
        "WEAK", {"产业链位置": 5, "AI收入真实性": 10, "市场空间": 5,
                 "估值": 7, "财务健康": 6, "拥挤度": 2},
        rules, params, on=TODAY)
    assert d.upper_bound == 60 and not d.dispatch_qualitative
    assert "剪枝留痕" in d.note  # A4


def test_s4_bounds_dispatches_when_reachable(rules):
    """上界 ≥ 试探线 → 必须派发定性取证（不因优化漏评有希望的标的）。"""
    params = load_params()
    d = decide_qualitative_dispatch(
        "OK", {"产业链位置": 12, "AI收入真实性": 15, "市场空间": 6,
               "估值": 8, "财务健康": 7, "拥挤度": 3},
        rules, params, bear_return=-0.35, on=TODAY)
    assert d.upper_bound == 76 and d.dispatch_qualitative
    assert d.bear_intercepted  # 熊市拦截标注但保守仍派发


def test_s4_bounds_respects_tightened_line(rules):
    """判定线上调（校准 75→80/试探 65→70）后剪枝阈值随参数联动——只可更严不放宽。"""
    params = load_params()
    params.sign("score.trial_line", 70, TODAY, pending_trades=False)
    d = decide_qualitative_dispatch(
        "MID", {"产业链位置": 10, "AI收入真实性": 12, "市场空间": 5,
                "估值": 8, "财务健康": 6, "拥挤度": 3},  # 44+25=69
        rules, params, on=TODAY)
    assert not d.dispatch_qualitative  # 69 < 70：新线下同样必然放弃


def test_s4_bounds_rejects_non_quant_dim(rules):
    with pytest.raises(KeyError):
        decide_qualitative_dispatch("X", {"护城河": 10}, rules, load_params(), on=TODAY)


# ---------- L4 证据缓存 ----------


def _ev(field_name, as_of):
    return Evidence(evidence_id=f"EV-{field_name}", subject="AMD",
                    field_name=field_name, value=1.0, as_of=as_of)


def test_cache_hit_within_freshness_window():
    c = EvidenceCache()
    c.put(_ev("cash_conversion", "2026-05-01"))  # 68 天前，财务窗口 100 天
    assert c.get("AMD", "cash_conversion", FieldClass.FINANCIAL, TODAY) is not None
    assert c.stats.hits == 1


def test_cache_expired_is_treated_as_missing():
    """过期=STALE=缺失（R2.1 不放宽）——照旧走正常取证。"""
    c = EvidenceCache()
    c.put(_ev("consensus_eps", "2026-05-01"))  # 68 天 > 一致预期 31 天窗口
    assert c.get("AMD", "consensus_eps", FieldClass.CONSENSUS, TODAY) is None
    assert c.stats.expired == 1


def test_cache_slow_changing_field_long_window():
    """慢变字段（审计师连任史）365 天窗口（G 级留痕）。"""
    c = EvidenceCache()
    c.put(_ev("auditor_tenure", "2025-09-01"))  # 310 天前
    assert c.get("AMD", "auditor_tenure", FieldClass.FINANCIAL, TODAY) is not None


# ---------- L6 用量计量与预算护栏 ----------


def test_meter_budget_alert_and_defensive_exemption():
    m = TokenMeter(monthly_budget=100_000)
    assert m.record("t1", AgentTaskType.VETO_FORENSICS, ModelTier.EXTRACTION, "2026-07", 60_000) is None
    alert = m.record("t2", AgentTaskType.QUALITATIVE_PRELIM, ModelTier.DUE_DILIGENCE, "2026-07", 50_000)
    assert alert is not None and alert.over and alert.spent == 110_000
    # 超预算：扩权类取证暂停，防守类照常（A5 精神）
    assert not m.dispatch_allowed(AgentTaskType.VETO_FORENSICS, "2026-07")
    assert m.dispatch_allowed(AgentTaskType.HOLDING_SIGNAL_FORENSICS, "2026-07")
    assert m.by_tier("2026-07") == {"extraction": 60_000, "due_diligence": 50_000}


def test_meter_no_budget_means_no_guardrail():
    m = TokenMeter()
    m.record("t1", AgentTaskType.VETO_FORENSICS, ModelTier.EXTRACTION, "2026-07", 10 ** 9)
    assert m.dispatch_allowed(AgentTaskType.VETO_FORENSICS, "2026-07")
