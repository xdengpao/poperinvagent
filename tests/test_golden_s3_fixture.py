"""S3 金标准 fixture 完整性断言（任务 2.4 验收；引擎逐条件断言在 P1 任务 11.3）。"""


def test_s3_tally(golden):
    doc = golden("s3/cases.json")
    tally = {"pass": 0, "veto": 0, "edge": 0, "stranded": 0}
    for c in doc["cases"]:
        tally[c["verdict"]] += 1
    assert tally == doc["expected_tally"] == {"pass": 10, "veto": 3, "edge": 3, "stranded": 8}


def test_s3_veto_condition_vectors(golden):
    """MPWR/SNPS/AEHR 命中路径条件向量存在且与报告判定一致。"""
    cases = {c["symbol"]: c for c in golden("s3/cases.json")["cases"]}
    mpwr = cases["MPWR"]["items"]["item1"]["conditions"]
    assert mpwr["audit_opinion_adverse"] is True          # ICFR 否定 → 字面命中
    assert mpwr["auditor_changes_5y_ge2"] is False        # 单次更换未达 ≥2 次
    assert mpwr["regulatory_fraud_case"] is False         # 集体诉讼系私诉
    assert mpwr["restatement_8k402"] is True              # 加严项 → 裁决队列
    assert cases["SNPS"]["items"]["item4"]["hit"] is True
    assert cases["AEHR"]["items"]["item2"]["hit"] is True
    assert cases["AEHR"]["items"]["item5"]["hit"] is True


def test_s3_edge_and_stranded_wiring(golden):
    """边缘态恰好挂 D-1/D-2/D-3；滞留原因恰在 8 只滞留标的上非空。"""
    cases = golden("s3/cases.json")["cases"]
    rulings = {c["symbol"]: c["ruling_request"] for c in cases if c["verdict"] == "edge"}
    assert rulings == {"NVDA": "D-1", "CDNS": "D-2", "RMBS": "D-3"}
    for c in cases:
        assert (c["verdict"] == "stranded") == bool(c.get("stranded_reason"))


def test_s3_pass_group_no_hits(golden):
    """通过组 10 只：任何 item 不得 hit=true（监控项另行登记）。"""
    for c in golden("s3/cases.json")["cases"]:
        if c["verdict"] == "pass":
            assert all(item.get("hit") is not True for item in c["items"].values()), c["symbol"]
