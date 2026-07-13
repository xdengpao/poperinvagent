"""S4 第 1 期（2026-07）评分执行脚本——A1：判定全部由 S4Engine 规则引擎产出。

估值维三情景统一口径 M-EST-1（首期约定，用户可裁决修订，只可更严）：
- 熊市 = FY+1 一致 EPS × 18（半导体长周期谷底锚）/ 现价 - 1
- E3   = FY+2 一致 EPS × (1 + 0.5 × g2)，g2 = FY+2/FY+1 - 1（增速半衰延续）
- 基准 = E3 × 28 / 现价 - 1；乐观 = E3 × 40 / 现价 - 1
- 估值档位: 基准≥+100% 且 无熊市拦截 且 PEG<1.5 → high；
  基准≥+50% 且 无拦截 → mid；其余 → low（档内: 基准≥50%→6-7 / 0-50%→3-5 / <0→0-2）
- FY+1/FY+2 一致 EPS 缺失或口径污染 → 估值维 gap（低档下限 0，计入缺失维数）
"""

import datetime as dt
import json

from invest_assistant.analysis.s4 import DimensionInput, S4Engine
from invest_assistant.rules import load_params, load_rules

ON = dt.date(2026, 7, 13)
rules, params = load_rules(), load_params()
engine = S4Engine(rules, params)

PX = {"NVDA": 208.17, "AMD": 542.50, "AVGO": 392.75, "MRVL": 227.50, "ALAB": 395.00,
      "CRDO": 248.00, "ARM": 312.50, "TSM": 433.16, "KLAC": 221.88, "TER": 344.58, "FORM": 112.74}

# (FY+1 EPS, FY+2 EPS, fwd_growth for PEG)  None = 口径污染/缺失 → 估值 gap
EPS = {
    "NVDA": (8.96, 12.73), "AMD": (4.99, 10.83), "AVGO": (11.62, 18.94),  # AVGO FY27 由一致 CAGR ~63% 衍生（间接口径，已标注）
    "MRVL": (4.10, 6.24), "ALAB": None,   # FY27 一致预期矛盾弃用
    "CRDO": None,                          # FY27/28 一致 EPS 未找到
    "ARM": (2.19, 3.06), "TSM": (15.35, 19.50),
    "KLAC": None,                          # $3.71 vs $37.06 拆股口径污染
    "TER": (6.99, 9.78),                   # 保守取低口径（滞后风险已标注）
    "FORM": (2.48, 3.12),
}


def scenario(sym):
    if EPS[sym] is None:
        return None
    e1, e2 = EPS[sym]
    px = PX[sym]
    g2 = e2 / e1 - 1
    e3 = e2 * (1 + 0.5 * g2)
    bear, base, bull = e1 * 18 / px - 1, e3 * 28 / px - 1, e3 * 40 / px - 1
    peg = (px / e1) / (g2 * 100) if g2 > 0 else None
    return engine.scenarios(round(bear, 3), round(base, 3), round(bull, 3), peg=round(peg, 2) if peg else None)


def val_band(sc):
    """M-EST-1 → 估值维档位与分值（文档化口径）。"""
    if sc is None:
        return None, None
    if sc.base_line_ok and not sc.bear_intercept and (sc.peg_cross_check_ok is not False):
        return "high", 13
    if sc.base >= 0.5 and not sc.bear_intercept:
        return "mid", 10
    pts = 6 if sc.base >= 0.5 else (3 if sc.base >= 0 else 2 if sc.base >= -0.3 else 1)
    return "low", pts


# 量化维档位映射（证据→锚点，理由见报告正文）；护城河=代理初评（待签认）
Q = {  # 产业链(band,pts) AI真实 空间 财务 拥挤 护城河初评
    "NVDA": [("high", 14), ("high", 19), ("mid", 8), ("high", 9), ("high", 5), ("high", 13)],
    "AMD":  [("mid", 10), ("high", 17), ("high", 9), ("high", 9), ("low", 1), ("mid", 10)],
    "AVGO": [("high", 13), ("high", 18), ("high", 9), ("mid", 7), ("mid", 3), ("high", 13)],
    "MRVL": [("mid", 10), ("mid", 16), ("high", 9), ("mid", 7), ("low", 1), ("mid", 10)],
    "ALAB": [("mid", 9), ("high", 17), ("mid", 8), ("high", 9), ("low", 0), ("mid", 9)],
    "CRDO": [("mid", 8), ("high", 17), ("mid", 7), ("high", 9), ("low", 0), ("mid", 8)],
    "ARM":  [("high", 13), ("mid", 13), ("high", 9), ("mid", 8), ("low", 1), ("high", 13)],
    "TSM":  [("high", 15), ("high", 18), ("high", 9), ("high", 10), ("mid", 3), ("high", 14)],
    "KLAC": [("high", 13), ("mid", 13), ("mid", 7), ("mid", 8), ("low", 1), ("high", 13)],
    "TER":  [("mid", 11), ("mid", 15), ("mid", 8), ("high", 9), ("low", 1), ("high", 13)],
    "FORM": [("mid", 9), ("mid", 13), ("mid", 7), ("high", 9), ("low", 1), ("mid", 10)],
}
S3_RECORDS = {  # 管理层维装配器 S3 记录注入（R10.5）
    "NVDA": ["D-1:反垄断四调查+循环投资→扣分输入", "SEC和解2027-05滚出"],
    "AMD": ["CEO减持10b5-1计划内(豁免)", "对华管制$8亿计提"],
    "CRDO": ["高管12月减持~$1亿10b5-1(豁免)"],
    "ARM": ["软银质押$85亿(市场口径,S3已档)"],
}

out = {}
for sym, (pos, ai, tam, fin, crowd, moat) in Q.items():
    sc = scenario(sym)
    vb, vp = val_band(sc)
    inputs = [
        DimensionInput("产业链位置", band=pos[0], points=pos[1]),
        DimensionInput("AI收入真实性", band=ai[0], points=ai[1]),
        DimensionInput("市场空间", band=tam[0], points=tam[1]),
        DimensionInput("财务健康", band=fin[0], points=fin[1]),
        DimensionInput("拥挤度", band=crowd[0], points=crowd[1]),
        DimensionInput("估值", band=vb, points=vp, gap=vb is None),
        DimensionInput("护城河", band=moat[0], points=moat[1], agent_draft=True),
        engine.assemble_management(sym, guidance_quarters=0, hit_ratio=None,
                                   s3_records=S3_RECORDS.get(sym, [])),
    ]
    card = engine.score(sym, inputs, signed_off=set(), snapshot_id=f"SNAP-2026-07-S4P1-{sym}", on=ON)
    qual_draft = sum(d.points for d in card.dimensions if d.kind == "qual")
    provisional = card.total_counted + qual_draft
    heavy = params.resolve("score.heavy_line", ON)
    trial = params.resolve("score.trial_line", ON)
    prov_judge = ("heavy" if provisional >= heavy else "trial" if provisional >= trial else "discard")
    if not card.eligible_for_reco:
        prov_judge = "blocked_gaps"
    out[sym] = {
        "dims": {d.name: {"pts": d.points, "band": d.band, "gap": d.gap, "counted": d.counted} for d in card.dimensions},
        "quant_counted": card.total_counted, "qual_draft": qual_draft, "provisional_total": provisional,
        "judgement_now": card.judgement, "judgement_if_signed": prov_judge,
        "gaps": [g.field_name for g in card.gaps], "eligible": card.eligible_for_reco,
        "scenario": None if sc is None else {
            "bear": sc.bear, "base": sc.base, "bull": sc.bull,
            "base_ok": sc.base_line_ok, "bear_intercept": sc.bear_intercept, "peg_ok": sc.peg_cross_check_ok},
        "rule_ids": card.rule_ids,
    }

print(json.dumps(out, ensure_ascii=False, indent=1))
print("\n判定线: heavy =", params.resolve("score.heavy_line", ON), "trial =", params.resolve("score.trial_line", ON))
