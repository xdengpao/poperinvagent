"""审计链完整性（任务 1.3，需求 R18.2）。"""

from invest_assistant.audit.chain import AuditChain


def _objs(day, n):
    return [{"id": f"{day}-{i}", "v": i} for i in range(n)]


def test_chain_verify_ok_and_detects_tampering():
    chain = AuditChain()
    data = {"2026-07-07": _objs("2026-07-07", 3), "2026-07-08": _objs("2026-07-08", 2)}
    for day, objs in data.items():
        chain.append_day(day, objs)
    ok, _ = chain.verify(data)
    assert ok
    data["2026-07-07"][0]["v"] = 999  # 篡改历史对象
    ok, where = chain.verify(data)
    assert not ok and "2026-07-07" in where


def test_empty_day_allowed():
    chain = AuditChain()
    chain.append_day("2026-07-07", [])
    assert chain.verify({"2026-07-07": []})[0]
