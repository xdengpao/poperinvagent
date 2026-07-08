"""金标准回归：S3 第 1 期引擎级复现（需求 R20.2，任务 11.3）。

断言：24 标的四态判定 10 通过/3 否决/3 边缘/8 滞留逐单复现；
MPWR/SNPS/AEHR 命中路径逐条件断言；边缘态恰好挂 D-1/D-2/D-3。
"""

import pytest

from invest_assistant.analysis.s3 import CaseInput, ItemInput, S3Engine
from invest_assistant.rules import load_rules


def _load_cases(golden) -> list[dict]:
    return golden("s3/cases.json")["cases"]


def _to_input(case: dict) -> CaseInput:
    items = {}
    for name, item in case["items"].items():
        items[name] = ItemInput(
            conditions=item["conditions"],
            qualitative_alert=item.get("qualitative_alert", False),
            concluded_not_hit=(item["hit"] is False),  # 报告"未命中"总述=结论级证据
        )
    return CaseInput(
        symbol=case["symbol"],
        market="US",  # 第 1 期 A 组全为美股口径
        items=items,
        evidence_complete=not case.get("stranded_reason"),
    )


@pytest.fixture(scope="module")
def engine():
    return S3Engine(load_rules())


def test_s3_full_tally_reproduction(golden, engine):
    """四态判定逐单复现（R20.2）。"""
    mismatches = []
    tally = {"pass": 0, "veto": 0, "edge": 0, "stranded": 0}
    for case in _load_cases(golden):
        v = engine.judge(_to_input(case), snapshot_id="SNAP-M-2026-07-07-001")
        tally[v.verdict] += 1
        if v.verdict != case["verdict"]:
            mismatches.append(f"{case['symbol']}: 引擎={v.verdict} 报告={case['verdict']}")
    assert not mismatches, mismatches
    assert tally == {"pass": 10, "veto": 3, "edge": 3, "stranded": 8}


def test_mpwr_hit_path(golden, engine):
    """MPWR：ICFR 否定意见机械命中项 1（重述加严项同时在场但命中优先）。"""
    case = next(c for c in _load_cases(golden) if c["symbol"] == "MPWR")
    v = engine.judge(_to_input(case))
    assert v.verdict == "veto"
    item1 = next(r for r in v.items if r.item == "item1")
    assert item1.state == "hit"
    assert item1.conditions["audit_opinion_adverse"] is True
    assert item1.conditions["auditor_changes_5y_ge2"] is False  # 单次更换不达 ≥2 次
    assert item1.conditions["regulatory_fraud_case"] is False  # 集体诉讼系私诉
    assert item1.conditions["restatement_8k402"] is True  # 加严项（命中已成立，无需另行裁决）


def test_snps_hit_path(golden, engine):
    """SNPS：项 4 两条件（并购占增量 >50% 且 利润率低于并表前）同时成立方命中。"""
    case = next(c for c in _load_cases(golden) if c["symbol"] == "SNPS")
    v = engine.judge(_to_input(case))
    assert v.verdict == "veto"
    item4 = next(r for r in v.items if r.item == "item4")
    assert item4.state == "hit"
    assert item4.conditions["ma_revenue_contribution_gt_50pct_3y"] is True
    assert item4.conditions["margin_below_pre_merger"] is True


def test_avgo_edge_not_hit_item4(golden, engine):
    """AVGO 对照：并购占增量 64%>50% 但利润率已回升——"且"条件后半不成立 → 不命中。"""
    case = next(c for c in _load_cases(golden) if c["symbol"] == "AVGO")
    v = engine.judge(_to_input(case))
    assert v.verdict == "pass"
    item4 = next(r for r in v.items if r.item == "item4")
    assert item4.state == "clear"
    assert item4.conditions["ma_revenue_contribution_gt_50pct_3y"] is True
    assert item4.conditions["margin_below_pre_merger"] is False


def test_aehr_hit_path(golden, engine):
    """AEHR：项 2（负现金流持续）+ 项 5（收入下滑期 ATM 增发依赖）双命中。"""
    case = next(c for c in _load_cases(golden) if c["symbol"] == "AEHR")
    v = engine.judge(_to_input(case))
    assert v.verdict == "veto"
    assert next(r for r in v.items if r.item == "item2").state == "hit"
    item5 = next(r for r in v.items if r.item == "item5")
    assert item5.state == "hit"
    assert item5.conditions["per_share_metrics_no_growth_3y"] is True
    assert item5.conditions["revenue_growth_via_dilution"] is True


def test_edge_cases_generate_ruling_requests(golden, engine):
    """NVDA/CDNS（监管处罚字面成立→定性待裁决）与 RMBS（定性警报）→ 边缘冻结。"""
    for sym in ("NVDA", "CDNS", "RMBS"):
        case = next(c for c in _load_cases(golden) if c["symbol"] == sym)
        v = engine.judge(_to_input(case))
        assert v.verdict == "edge" and v.ruling_needed, sym
    rulings = {c["symbol"]: c["ruling_request"] for c in _load_cases(golden) if c["verdict"] == "edge"}
    assert rulings == {"NVDA": "D-1", "CDNS": "D-2", "RMBS": "D-3"}


def test_stranded_eight(golden, engine):
    """8 只滞留：取证未闭合 → 滞留态（回流由 FR-O-06 承接，3 期转淘汰）。"""
    stranded = [c["symbol"] for c in _load_cases(golden)
                if engine.judge(_to_input(c)).verdict == "stranded"]
    assert sorted(stranded) == sorted(["AIP", "INTC", "MU", "AMKR", "COHR", "AMAT", "LRCX", "ASML"])


def test_verdict_carries_trace(golden, engine):
    """A4：判定产物带规则 ID 与快照引用。"""
    case = _load_cases(golden)[0]
    v = engine.judge(_to_input(case), snapshot_id="SNAP-X")
    assert "S3.VERDICT.FOURSTATE" in v.rule_ids and v.snapshot_id == "SNAP-X"
