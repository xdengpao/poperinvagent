"""金标准回归：S2 第 1 期候选池复现（需求 R20.2，任务 9.4）。"""

from invest_assistant.analysis.s2 import Candidate, S2Engine
from invest_assistant.rules import load_rules

QUALIFIED = ["算力芯片", "半导体设备与材料"]


def _candidates(golden) -> list[Candidate]:
    doc = golden("s2/inputs.json")
    return [
        Candidate(
            symbol=c["symbol"],
            name=c["name"],
            market=c["market"],
            mainline=c.get("mainline"),
            sources=tuple(c["sources"]),
            data_scope=c["data_scope"],
            adv_usd_90d=c.get("adv_usd_90d"),
            disruption_hit=c.get("disruption_hit", False),
            exclusion_reason=c.get("exclusion_reason", "") or "",
        )
        for c in doc["candidates"]
    ]


def test_s2_pool_reproduction(golden):
    """A 组 24 只 / B 组 16 / C 组 7 / 排除 10，逐单验证。"""
    expected = golden("s2/expected.json")
    result = S2Engine(load_rules()).run(_candidates(golden), QUALIFIED, liquidity_proxy="90d")
    assert sorted(result.group["A"]) == sorted(expected["group_A"])
    assert sorted(result.group["B"]) == sorted(expected["group_B"])
    assert sorted(result.group["C"]) == sorted(expected["group_C"])
    assert sorted(e.symbol for e in result.excluded) == sorted(e["symbol"] for e in expected["excluded"])
    assert all(e.reason_class == "mainline-mismatch" for e in result.excluded)


def test_s2_deferred_groups_no_opinions(golden):
    """B/C 组缓评状态（R8.3）：deferred=True；A 组全部带 90d 代理口径 G 级标注。"""
    result = S2Engine(load_rules()).run(_candidates(golden), QUALIFIED, liquidity_proxy="90d")
    for entry in result.pool:
        assert entry.deferred == (entry.group in ("B", "C"))
        if entry.group == "A":
            assert "G 级降级" in entry.liquidity_note


def test_s2_liquidity_gate(golden):
    """流动性门槛：低于 $1000 万拒入池；口径未获许可拒运行（R8.2）。"""
    import pytest

    engine = S2Engine(load_rules())
    thin = Candidate(
        symbol="THIN", name="流动性不足样例", market="US", mainline="算力芯片",
        sources=("graph#99",), data_scope="US-full", adv_usd_90d=9_999_999,
    )
    result = engine.run([thin], QUALIFIED, liquidity_proxy="90d")
    assert result.excluded and result.excluded[0].reason_class == "liquidity"
    with pytest.raises(ValueError):
        engine.run([thin], QUALIFIED, liquidity_proxy="30d")
