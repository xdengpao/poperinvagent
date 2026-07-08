"""FastAPI 交互层（任务 14，需求 R17）——TestClient 冒烟。"""

from fastapi.testclient import TestClient

from invest_assistant.ui.app import create_app
from invest_assistant.ui.queues import SignoffItemType

client = TestClient(create_app(run_self_check=False))


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_s0_admission_endpoint():
    r = client.post("/s0/admission", json={"declared_fund_types": ["杠杆资金"]})
    body = r.json()
    assert not body["admitted"] and body["mode"] == "learning_only"


def test_signoff_flow():
    client.post("/queues/signoff/submit", json={
        "item_id": "s1", "item_type": SignoffItemType.QUALITATIVE_REVIEW.value,
        "subject": "AMD", "payload": {"score": 12}})
    assert any(i["item_id"] == "s1" for i in client.get("/queues/signoff").json()["pending"])
    r = client.post("/queues/signoff/act", json={"item_id": "s1", "action": "approve"})
    assert r.status_code == 200 and r.json()["action"] == "approve"


def test_reject_without_reason_400():
    client.post("/queues/signoff/submit", json={
        "item_id": "s2", "item_type": SignoffItemType.QUALITATIVE_REVIEW.value,
        "subject": "X", "payload": {}})
    r = client.post("/queues/signoff/act", json={"item_id": "s2", "action": "reject"})
    assert r.status_code == 400


def test_receipt_endpoint():
    r = client.post("/receipts", json={
        "decision_record_id": "d1", "symbol": "AMD", "action": "new_open",
        "executed_at": "2026-07-08", "quantity": 100, "price": 150})
    assert r.status_code == 200 and r.json()["trade_id"]


def test_connectivity_reports_mode():
    r = client.get("/connectivity")
    assert "mode" in r.json() and "matrix" in r.json()


def test_degraded_mode_when_self_check_fails():
    """本环境网络全断 → 启动自检拒绝意见模式（R1.2 现场验证）。"""
    degraded = TestClient(create_app(run_self_check=True))
    body = degraded.get("/connectivity").json()
    assert body["mode"] == "degraded" and not body["opinion_mode_allowed"]
