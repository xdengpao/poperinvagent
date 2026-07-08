"""防幻觉校验链测试（P1 任务 10.2-10.4，需求 R16.2/R16.3/R16.5，验收 R20.4）。

对抗样本库拦截率必须为 100%，且逐样本断言拦截段正确。
"""

import json
from pathlib import Path

import pytest

from invest_assistant.agents.tasks import (
    DEFAULT_TEMPLATES,
    AgentTaskType,
    AttemptStatus,
    TaskManager,
    TaskStatus,
)
from invest_assistant.agents.verify import (
    CorroborationChecker,
    EvidenceDraft,
    ReconciliationRule,
    SourceRef,
    SourceTier,
    Stage,
    VerificationChain,
    VerificationOutcome,
    build_evidence,
    builtin_registry,
    dispose,
    value_matches_text,
)
from invest_assistant.core.types import Confidence, Freshness
from invest_assistant.data.quality import GapLedger

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = json.loads(
    (ROOT / "fixtures" / "adversarial" / "samples.json").read_text(encoding="utf-8")
)["samples"]
ADVERSARIAL = [s for s in SAMPLES if s["expect_intercept"]]
LEGIT = [s for s in SAMPLES if not s["expect_intercept"]]


def _chain() -> VerificationChain:
    return VerificationChain(recon_registry=builtin_registry())


def _src(source_id, publisher, tier="media", origin="", text="季度营收 500 百万美元"):
    return SourceRef(
        source_id=source_id, publisher=publisher, tier=SourceTier(tier),
        origin_id=origin, snapshot_text=text,
    )


# ---------------------------------------------------------- 对抗样本库（R20.4）


@pytest.mark.parametrize("sample", ADVERSARIAL, ids=lambda s: s["sample_id"])
def test_adversarial_sample_intercepted_at_expected_stage(sample):
    """逐样本断言：拦截 + 拦截段正确 + 勾稽矛盾路由 + 自动产 Gap（R16.2/R16.3）。"""
    outcome = _chain().verify(EvidenceDraft.from_dict(sample["draft"]))
    assert not outcome.passed, f"{sample['sample_id']} 应被拦截"
    assert outcome.confidence is Confidence.UNVERIFIED
    assert sorted(outcome.stages_failed) == sorted(sample["expected_stages"]), (
        f"{sample['sample_id']} 期望拦截段 {sample['expected_stages']}，"
        f"实际 {[s.value for s in outcome.stages_failed]}"
    )
    assert outcome.to_ruling_queue == sample["expect_ruling_queue"]
    assert outcome.gap is not None  # 任一段失败 → 自动产 Gap
    assert outcome.gap.subject == sample["draft"]["subject"]


def test_adversarial_interception_rate_is_100_percent():
    """UNVERIFIED 拦截率 = 100%（R20.4），样本库 ≥8 个编造样本。"""
    assert len(ADVERSARIAL) >= 8
    chain = _chain()
    intercepted = [
        s for s in ADVERSARIAL if not chain.verify(EvidenceDraft.from_dict(s["draft"])).passed
    ]
    assert len(intercepted) == len(ADVERSARIAL)


@pytest.mark.parametrize("sample", LEGIT, ids=lambda s: s["sample_id"])
def test_legit_sample_passes(sample):
    """合法样本四段全过 → VERIFIED、无 Gap、不进裁决队列。"""
    outcome = _chain().verify(EvidenceDraft.from_dict(sample["draft"]))
    assert outcome.passed, f"{sample['sample_id']} 不应被拦截：{outcome.notes}"
    assert outcome.confidence is Confidence.VERIFIED
    assert outcome.gap is None
    assert not outcome.to_ruling_queue


# ---------------------------------------------------------- ①来源回溯


def test_source_trace_tolerance_is_parameterized():
    """数值 ±容差匹配，容差参数化（R16.2①）。"""
    text = "全年收入 100,000 百万元。"
    assert value_matches_text(100000, text, rel_tol=0.005)
    assert value_matches_text(100200, text, rel_tol=0.005)  # 0.2% 偏差在容差内
    assert not value_matches_text(102000, text, rel_tol=0.005)  # 2% 偏差超容差
    assert value_matches_text(102000, text, rel_tol=0.03)  # 放宽容差后命中


def test_metadata_only_source_never_passes():
    """机械谓词：仅元数据非空不算通过——空快照必不命中（R16.2①）。"""
    assert not value_matches_text(500, "", rel_tol=0.5)
    assert not value_matches_text(500, "   ", rel_tol=0.5)


# ---------------------------------------------------------- ②勾稽清单


def test_recon_registry_builtin_rules_and_extension():
    """内置至少三条勾稽规则；清单可扩展注册；id 唯一（R16.2②）。"""
    registry = builtin_registry()
    assert set(registry.rule_ids()) >= {
        "recon.quarter_sum_vs_annual",
        "recon.ni_over_eps_vs_weighted_shares",
        "recon.balance_sheet_identity",
    }
    custom = ReconciliationRule(
        rule_id="recon.gross_profit_vs_margin",
        description="毛利÷收入 vs 毛利率（扩展示例）",
        applies=lambda s: all(k in s for k in ("revenue", "gross_profit", "gross_margin_pct")),
        check=lambda s, tol: abs(
            float(s["gross_profit"]) / float(s["revenue"]) * 100 - float(s["gross_margin_pct"])
        ) <= tol,
        tolerance=0.5,
    )
    registry.register(custom)
    findings = registry.evaluate({"revenue": 100.0, "gross_profit": 40.0, "gross_margin_pct": 40.0})
    assert any(f.rule_id == "recon.gross_profit_vs_margin" and f.passed for f in findings)
    with pytest.raises(ValueError):
        registry.register(custom)  # 重复 id 拒绝


def test_recon_rules_apply_only_when_precondition_holds():
    """适用前提谓词：字段不齐的规则不出具 finding（R16.2②）。"""
    findings = builtin_registry().evaluate({"net_income": 100.0})  # 缺 eps/shares
    assert findings == []


# ---------------------------------------------------------- ④双源佐证


def test_corroboration_independence_rules():
    """发布主体去重 + 同一原始通稿判同源 + 官方原文单源即可（R16.4/R16.5）。"""
    checker = CorroborationChecker()
    same_wire = [_src("a", "新浪财经", origin="prn-1"), _src("b", "网易财经", origin="prn-1")]
    assert checker.independent_count(same_wire) == 1
    assert not checker.check(same_wire)

    same_pub = [_src("a", "Seeking Alpha"), _src("b", "Seeking Alpha")]
    assert not checker.check(same_pub)

    independent = [_src("a", "Reuters", origin="r-1"), _src("b", "Bloomberg", origin="b-1")]
    assert checker.independent_count(independent) == 2
    assert checker.check(independent)

    official_single = [_src("a", "SEC EDGAR", tier="official")]
    assert checker.check(official_single)


# ---------------------------------------------------------- UNVERIFIED 唯一置位方


def test_unverified_can_only_be_set_by_verifier():
    """置信为派生只读属性：构造器不收置信参数、不可改写、只由失败段决定（A1/R16.2）。"""
    with pytest.raises(TypeError):
        VerificationOutcome(confidence=Confidence.UNVERIFIED)  # 调用方无法指定置信
    clean = VerificationOutcome()
    assert clean.confidence is Confidence.VERIFIED
    failed = VerificationOutcome(stages_failed=(Stage.SOURCE_TRACE,))
    assert failed.confidence is Confidence.UNVERIFIED
    with pytest.raises(AttributeError):
        failed.confidence = Confidence.VERIFIED  # 只读，无 setter


def test_build_evidence_stamps_confidence_from_outcome_only():
    """证据置信只取校验结论；标签非法时新鲜度保守归档为 STALE（R2.1）。"""
    draft = EvidenceDraft.from_dict(ADVERSARIAL[0]["draft"])
    bad = _chain().verify(draft)
    ev = build_evidence(draft, bad, evidence_id="EV-ADV-1")
    assert ev.confidence is Confidence.UNVERIFIED

    ok_draft = EvidenceDraft.from_dict(LEGIT[0]["draft"])
    good = _chain().verify(ok_draft)
    ok_ev = build_evidence(ok_draft, good, evidence_id="EV-OK-1")
    assert ok_ev.confidence is Confidence.VERIFIED
    assert ok_ev.freshness is Freshness.FRESH
    assert ok_ev.provenance == ok_draft.basis_label  # 口径标签随证据归档


# ---------------------------------------------------------- 失败处置链（R16.3）


def _task(mgr: TaskManager):
    return mgr.submit(
        DEFAULT_TEMPLATES[AgentTaskType.VETO_FORENSICS],
        subject="NVDA", sub_item="item2_audit", period="2026Q2",
    )


def test_dispose_success_marks_attempt_valid():
    mgr = TaskManager()
    task = _task(mgr)
    attempt = task.new_attempt()
    outcome = _chain().verify(EvidenceDraft.from_dict(LEGIT[0]["draft"]))
    disposition = dispose(task, attempt, outcome, mgr)
    assert disposition.action == "accepted"
    assert task.latest_valid() is attempt
    assert task.status is TaskStatus.VERIFIED


def test_dispose_failure_gap_then_one_retry_then_stranded():
    """失败 → 自动 Gap + 登记一次补取证重试；仍失败 → A2 降级/滞留（R16.3）。"""
    mgr = TaskManager()
    task = _task(mgr)
    ledger = GapLedger()
    chain = _chain()
    fabricated = EvidenceDraft.from_dict(ADVERSARIAL[0]["draft"])

    first = task.new_attempt(note="初次取证")
    d1 = dispose(task, first, chain.verify(fabricated), mgr, ledger)
    assert d1.action == "retry_registered"
    assert d1.gap is not None and len(ledger.gaps) == 1  # 自动产 Gap 且登记
    assert d1.retry_attempt_no == 2
    assert task.attempts[1].status is AttemptStatus.PENDING  # 重试 = 新 attempt

    d2 = dispose(task, task.attempts[1], chain.verify(fabricated), mgr, ledger)
    assert d2.action == "stranded"
    assert task.status is TaskStatus.STRANDED
    assert task.latest_valid() is None  # 判定无可读产物（A2）
    assert len(task.attempts) == 2  # 滞留后不再登记第三次自动重试


def test_recon_contradiction_routes_to_ruling_queue_without_auto_retry():
    """勾稽矛盾单独路由裁决队列，未决期间不自动补取证（R16.3/R6.7）。"""
    mgr = TaskManager()
    task = _task(mgr)
    ledger = GapLedger()
    recon_sample = next(s for s in ADVERSARIAL if s["expect_ruling_queue"])
    attempt = task.new_attempt()
    outcome = _chain().verify(EvidenceDraft.from_dict(recon_sample["draft"]))
    disposition = dispose(task, attempt, outcome, mgr, ledger)
    assert disposition.action == "ruling_queue"
    assert disposition.to_ruling_queue
    assert disposition.gap is not None and len(ledger.gaps) == 1
    assert len(task.attempts) == 1  # 未登记自动重试
