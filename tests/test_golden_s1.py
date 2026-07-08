"""金标准回归：S1 第 1 期两态复现（需求 R20.2，任务 9.4）。"""

import datetime as dt

import pytest

from invest_assistant.analysis.s1 import FactorEvidence, Ruling, S1Engine
from invest_assistant.data.snapshot import SnapshotStore
from invest_assistant.rules import load_rules


def _evidence(golden) -> list[FactorEvidence]:
    doc = golden("s1/evidence.json")
    out = []
    for ind in doc["industries"]:
        for f in ind["factors"]:
            out.append(
                FactorEvidence(
                    industry=ind["industry"],
                    factor=f["factor"],
                    grade=f["grade"],
                    approx=f["approx"],
                    gap=f["gap"],
                )
            )
    return out


def _rulings(golden) -> list[Ruling]:
    return [
        Ruling(
            ruling_id=r["id"],
            industry_scope=tuple(r["scope"]),
            factor=r["factor"],
            grade_from=r["from"],
            grade_to=r["to"],
        )
        for r in golden("s1/rulings.json")
    ]


@pytest.fixture()
def snapshots():
    store = SnapshotStore()
    pre = store.create_monthly(dt.date(2026, 7, 7), "rules-v1", "params-v1", rulings=[])
    post = store.create_monthly(dt.date(2026, 7, 7), "rules-v1", "params-v1", rulings=["裁决A"])
    return pre, post


def test_s1_pre_ruling_state(golden, snapshots):
    """裁决 A 前：22/20/17/17/17/15/8/6，仅算力芯片入围。"""
    expected = golden("s1/expected.json")["pre_ruling"]
    result = S1Engine(load_rules()).run(_evidence(golden), rulings=[], snapshot_id=snapshots[0].snapshot_id)
    assert {s.industry: s.total for s in result.scores} == expected["scores"]
    assert result.qualified == expected["qualified"]
    for name in expected["not_qualified"]:
        assert name not in result.qualified


def test_s1_post_ruling_state(golden, snapshots):
    """裁决 A 后：23/21/17/17/17/15/8/6，算力芯片+半导体设备与材料入围。"""
    expected = golden("s1/expected.json")["post_ruling"]
    result = S1Engine(load_rules()).run(
        _evidence(golden), rulings=_rulings(golden), snapshot_id=snapshots[1].snapshot_id
    )
    assert {s.industry: s.total for s in result.scores} == expected["scores"]
    assert sorted(result.qualified) == sorted(expected["qualified"])
    equip = next(s for s in result.scores if s.industry == "半导体设备与材料")
    assert equip.applied_rulings == ["裁决A"]
    assert equip.qualified and not equip.pending_ruling


def test_s1_gate_line_and_gaps(golden):
    """入围线取自规则库（≥21 前 5 不凑数）；缺证据因子计 0 并产 Gap（R7.1/R7.2）。"""
    rules = load_rules()
    gate = rules.get("S1.GATE.LINE")
    assert gate.params["gate_line"] == 21 and gate.params["top_n"] == 5
    engine = S1Engine(rules)
    result = engine.run(_evidence(golden), rulings=[], snapshot_id="SNAP-TEST")
    doc = golden("s1/evidence.json")
    n_gap_cells = sum(1 for i in doc["industries"] for f in i["factors"] if f["gap"])
    assert len(result.gaps) == n_gap_cells  # 零静默：每个缺失格一条缺口对象
