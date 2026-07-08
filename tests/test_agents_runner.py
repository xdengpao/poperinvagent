"""代理执行器（任务 10 执行侧，R16）：fake LLM 执行器 → 四段链 → 处置。"""

from invest_assistant.agents.runner import AgentRunner
from invest_assistant.agents.tasks import DEFAULT_TEMPLATES, AgentTaskType, TaskManager
from invest_assistant.agents.verify import VerificationChain


def _draft(value: float, snapshot_text: str) -> dict:
    return {
        "subject": "TEST", "field_name": "revenue", "value": value, "unit": "USD",
        "as_of": "2026-07-01", "freshness_label": "fresh", "basis_label": "GAAP",
        "sources": [{"source_id": "s1", "publisher": "SEC", "tier": "official",
                     "snapshot_text": snapshot_text}],
    }


def _template(task_type=AgentTaskType.FILING_STRUCTURING):
    return DEFAULT_TEMPLATES[task_type]


def test_runner_accepts_verified_output():
    mgr = TaskManager()
    task = mgr.submit(_template(), subject="TEST", sub_item="rev", period="2026-07")
    runner = AgentRunner(mgr, VerificationChain(),
                         executor=lambda t: _draft(1000.0, "全年收入 1,000 美元"))
    sink: list = []
    report = runner.run([task], sink.append)
    assert report.accepted == 1 and report.dispositions[0].action == "accepted"
    assert sink and sink[0].confidence.value == "verified"
    assert task.latest_valid() is not None


def test_runner_fabricated_number_retry_then_stranded():
    """编造数字（原文无此数）→ 一次补取证 → 仍失败滞留（A2）。"""
    mgr = TaskManager()
    task = mgr.submit(_template(), subject="TEST", sub_item="rev", period="2026-07")
    runner = AgentRunner(mgr, VerificationChain(),
                         executor=lambda t: _draft(9999.0, "全年收入 1,000 美元"))
    sink: list = []
    d1 = runner.run_task(task, sink.append)
    assert d1.action == "retry_registered" and d1.gap is not None
    d2 = runner.run_task(task, sink.append)
    assert d2.action == "stranded"
    assert all(ev.confidence.value == "unverified" for ev in sink)  # 留痕但不可用于判定
    assert runner.ledger.gaps  # 缺口零静默（R2.6）
