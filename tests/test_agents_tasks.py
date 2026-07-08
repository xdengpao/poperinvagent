"""代理任务管理测试（P1 任务 10.1，需求 R16.1/R16.4/R16.7）。"""

import pytest

from invest_assistant.agents.tasks import (
    DEFAULT_TEMPLATES,
    AgentTaskType,
    AttemptStatus,
    ModelTier,
    NaturalKey,
    TaskManager,
    TaskStatus,
    TaskTemplate,
)


def _submit(mgr: TaskManager, **overrides):
    args = {
        "template": DEFAULT_TEMPLATES[AgentTaskType.VETO_FORENSICS],
        "subject": "MPWR",
        "sub_item": "item1_regulatory",
        "period": "2026Q2",
    }
    args.update(overrides)
    return mgr.submit(**args)


def test_seven_task_types_enum():
    """七类任务类型枚举齐备（R16.1）。"""
    assert {t.value for t in AgentTaskType} == {
        "veto_forensics",
        "filing_structuring",
        "industry_event",
        "qualitative_prelim",
        "retro_prep",
        "holding_signal_forensics",
        "disruption_criteria_forensics",
    }


def test_templates_declare_model_tier_and_version():
    """每类任务模板均声明模型档位（尽调级/抽取级双档）与版本（R16.7/R16.1）。"""
    assert set(DEFAULT_TEMPLATES) == set(AgentTaskType)
    assert all(t.version for t in DEFAULT_TEMPLATES.values())
    assert all(t.model_tier in ModelTier for t in DEFAULT_TEMPLATES.values())
    # 判断密集型 → 尽调级；结构化抽取型 → 抽取级
    assert DEFAULT_TEMPLATES[AgentTaskType.VETO_FORENSICS].model_tier is ModelTier.DUE_DILIGENCE
    assert DEFAULT_TEMPLATES[AgentTaskType.FILING_STRUCTURING].model_tier is ModelTier.EXTRACTION
    # 双档均被实际使用
    assert {t.model_tier for t in DEFAULT_TEMPLATES.values()} == {
        ModelTier.DUE_DILIGENCE,
        ModelTier.EXTRACTION,
    }


def test_idempotent_submit_returns_existing_task():
    """同自然键重复提交返回已有任务而非新建（R16.4），attempt 链保留。"""
    mgr = TaskManager()
    task = _submit(mgr)
    task.new_attempt(note="初次取证")
    again = _submit(mgr)
    assert again is task
    assert len(mgr) == 1
    assert len(again.attempts) == 1


def test_natural_key_five_dimensions_create_distinct_tasks():
    """自然键 = 标的×任务类型×子项×期次×模板版本，任一维不同即新任务（R16.4）。"""
    mgr = TaskManager()
    base = _submit(mgr)
    variants = [
        _submit(mgr, subject="SNPS"),
        _submit(mgr, template=DEFAULT_TEMPLATES[AgentTaskType.FILING_STRUCTURING]),
        _submit(mgr, sub_item="item3_insider_selling"),
        _submit(mgr, period="2026Q3"),
        _submit(mgr, template=TaskTemplate(AgentTaskType.VETO_FORENSICS, "v2", ModelTier.DUE_DILIGENCE)),
    ]
    assert all(v is not base for v in variants)
    assert len({id(v) for v in variants}) == 5
    assert len(mgr) == 6
    key = NaturalKey("MPWR", AgentTaskType.VETO_FORENSICS, "item1_regulatory", "2026Q2", "v1")
    assert mgr.get(key) is base


def test_judgement_reads_latest_valid_attempt_only():
    """判定只读最新有效 attempt：跳过更晚的无效 attempt（R16.4）。"""
    mgr = TaskManager()
    task = _submit(mgr)
    task.new_attempt()
    task.mark(1, AttemptStatus.INVALID)
    task.new_attempt()
    task.mark(2, AttemptStatus.VALID, evidence_id="EV-OK-2")
    task.new_attempt()
    task.mark(3, AttemptStatus.INVALID)
    latest = task.latest_valid()
    assert latest is not None
    assert latest.attempt_no == 2
    assert latest.evidence_id == "EV-OK-2"
    assert task.status is TaskStatus.VERIFIED


def test_no_valid_attempt_returns_none():
    """无有效 attempt 时判定不得读任何产物（A2）。"""
    mgr = TaskManager()
    task = _submit(mgr)
    assert task.latest_valid() is None
    task.new_attempt()
    task.mark(1, AttemptStatus.INVALID)
    assert task.latest_valid() is None


def test_retry_is_new_attempt_history_preserved():
    """重试 = 新 attempt，历史 attempt 不被覆写（R16.4/R16.3）。"""
    mgr = TaskManager()
    task = _submit(mgr)
    task.new_attempt(note="初次取证")
    task.mark(1, AttemptStatus.INVALID)
    retry = mgr.register_retry(task, reason="补取证重试")
    assert retry.attempt_no == 2
    assert retry.status is AttemptStatus.PENDING
    assert task.attempts[0].status is AttemptStatus.INVALID  # 历史保留


def test_stranded_task_rejects_further_auto_retry():
    """滞留任务不再自动补取证（A2 降级，R16.3）。"""
    mgr = TaskManager()
    task = _submit(mgr)
    task.status = TaskStatus.STRANDED
    with pytest.raises(ValueError):
        mgr.register_retry(task, reason="再试一次")


def test_empty_natural_key_dimension_rejected():
    mgr = TaskManager()
    with pytest.raises(ValueError):
        _submit(mgr, subject="")
