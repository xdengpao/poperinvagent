"""S4 评分引擎（任务 12，需求 R10）。"""

import datetime as dt

import pytest

from invest_assistant.analysis.s4 import DimensionInput, S4Engine
from invest_assistant.rules import load_params, load_rules

TODAY = dt.date(2026, 7, 8)


@pytest.fixture()
def engine():
    return S4Engine(load_rules(), load_params())


def _full_inputs(**overrides):
    base = {
        "产业链位置": DimensionInput("产业链位置", band="high", points=14),
        "AI收入真实性": DimensionInput("AI收入真实性", band="high", points=18),
        "市场空间": DimensionInput("市场空间", band="mid", points=7),
        "护城河": DimensionInput("护城河", band="high", points=13, agent_draft=True),
        "管理层": DimensionInput("管理层", band="mid", points=7, agent_draft=True),
        "估值": DimensionInput("估值", band="mid", points=10),
        "财务健康": DimensionInput("财务健康", band="high", points=9),
        "拥挤度": DimensionInput("拥挤度", band="mid", points=3),
    }
    base.update(overrides)
    return list(base.values())


ALL_QUAL_SIGNED = {"护城河", "管理层"}


def test_full_score_heavy(engine):
    card = engine.score("TEST", _full_inputs(), signed_off=ALL_QUAL_SIGNED, on=TODAY)
    assert card.total_counted == 81 and card.judgement == "heavy"


def test_qualitative_unsigned_blocks_judgement(engine):
    """定性维未签认 → 不参与判定线且判定挂起（R10.1，规格书 S4 更严口径）。"""
    card = engine.score("TEST", _full_inputs(), signed_off=set(), on=TODAY)
    assert card.judgement == "blocked_pending_signoff"
    assert sorted(card.pending_signoff) == ["护城河", "管理层"]
    assert card.total_counted == 81 - 13 - 7  # 未签认分不计入


def test_single_gap_low_floor(engine):
    """单维缺失 → 低档下限 + 标注，仍可判定（R10.2）。"""
    card = engine.score("TEST", _full_inputs(拥挤度=DimensionInput("拥挤度", gap=True)),
                        signed_off=ALL_QUAL_SIGNED, on=TODAY)
    crowd = next(d for d in card.dimensions if d.name == "拥挤度")
    assert crowd.gap and crowd.points == 0 and card.eligible_for_reco
    assert card.gaps and card.judgement in ("heavy", "trial", "discard")


def test_two_gaps_block_recommendation(engine):
    """缺失 ≥2 维 → 不得进入推荐输出（R10.2）。"""
    card = engine.score(
        "TEST",
        _full_inputs(拥挤度=DimensionInput("拥挤度", gap=True), 市场空间=DimensionInput("市场空间", gap=True)),
        signed_off=ALL_QUAL_SIGNED, on=TODAY,
    )
    assert not card.eligible_for_reco and card.judgement == "blocked_gaps"


def test_band_range_enforced(engine):
    with pytest.raises(ValueError):
        engine.score("TEST", _full_inputs(估值=DimensionInput("估值", band="mid", points=14)),
                     signed_off=ALL_QUAL_SIGNED, on=TODAY)


def test_scenario_bear_intercept(engine):
    """熊市 <-30% 一票拦截，无论总分（R10.3）。"""
    r = engine.scenarios(bear=-0.35, base=1.2, bull=2.5, peg=1.2)
    assert r.bear_intercept and r.base_line_ok and r.peg_cross_check_ok
    r2 = engine.scenarios(bear=-0.25, base=0.8, bull=2.0)
    assert not r2.bear_intercept and not r2.base_line_ok


def test_management_assembler_cold_start(engine):
    """冷启动：库不满 4 季 → 中档下限 + 标签（R2.8，签署决策 2）。"""
    d = engine.assemble_management("TEST", guidance_quarters=2, hit_ratio=None, s3_records=[])
    assert d.band == "mid" and d.points == 6
    assert any("cold_start" in e for e in d.evidence_ids)


def test_management_assembler_actual_ratio(engine):
    """满 4 季自动切换实际兑现率；S3 记录注入证据（R10.5）。"""
    d = engine.assemble_management("TEST", guidance_quarters=6, hit_ratio=0.85,
                                   s3_records=["s3:form4:CEO减持$2.2亿(10b5-1)"])
    assert d.band == "high" and "s3:form4:CEO减持$2.2亿(10b5-1)" in d.evidence_ids
    assert engine.assemble_management("T", guidance_quarters=6, hit_ratio=0.7, s3_records=[]).band == "mid"
    assert engine.assemble_management("T", guidance_quarters=6, hit_ratio=0.5, s3_records=[]).band == "low"


def test_requalify_downgrade_directives(engine):
    """持仓季度重打分：65-74 → 减仓至试探仓上限；<65 → 卖出（R10.4，出处=框架 3.4）。"""
    trial_card = engine.score("HELD", _full_inputs(
        AI收入真实性=DimensionInput("AI收入真实性", band="low", points=8),
        估值=DimensionInput("估值", band="low", points=5),
    ), signed_off=ALL_QUAL_SIGNED, on=TODAY)
    assert 65 <= trial_card.total_counted < 75
    assert engine.requalify_directive(trial_card, is_held=True, on=TODAY) == "reduce_to_trial_cap"

    sell_card = engine.score("HELD", _full_inputs(
        AI收入真实性=DimensionInput("AI收入真实性", band="low", points=0),
        估值=DimensionInput("估值", band="low", points=0),
        产业链位置=DimensionInput("产业链位置", band="low", points=5),
    ), signed_off=ALL_QUAL_SIGNED, on=TODAY)
    assert sell_card.total_counted < 65
    assert engine.requalify_directive(sell_card, is_held=True, on=TODAY) == "sell"
    assert engine.requalify_directive(sell_card, is_held=False, on=TODAY) is None  # 非持仓不产处置
