"""S5 第 1 期（2026-07）执行脚本——签认落地 → 市况分级 → 风控联立 → 意见生成 → M10 合规。

A1：全部判定由规则引擎产出（S4Engine / RegimeGrader / RiskEngine / OpinionFactory /
ComplianceChecker），本脚本只装配输入并串联调用。

市况信号读数来源（A2：缺口一律 None，不计命中并产 Gap）：
- ③ 涨幅腿：SOX 指数 12 个月 +131.2%（IBKR 一手，5607.92@2025-07-14 → 12967.16@2026-07）；
  上修腿与其余信号读数见 REGIME_READINGS 注释（网络检索降级路径取证，逐项标注来源+日期）。
"""

import datetime as dt
import json

from invest_assistant.analysis.regime import RegimeGrader, SignalReading
from invest_assistant.analysis.s4 import DimensionInput, S4Engine
from invest_assistant.analysis.thesis import Hypothesis, ScenarioTriple, ThesisCard
from invest_assistant.opinion.compliance import ComplianceChecker
from invest_assistant.opinion.factory import (
    EvidenceItem,
    OpinionFactory,
    PositionPlan,
    ScenarioTargets,
    ScoreDetail,
    StopLossCheck,
)
from invest_assistant.risk.engine import PortfolioState, Proposal, RiskEngine
from invest_assistant.rules import load_params, load_rules

ON = dt.date(2026, 7, 13)
SIGNOFF_ID = "SIGNOFF-2026-07-S4P1"  # docs/签认记录-S4定性维与估值口径-2026-07.md
# NVDA FY27Q2 财报日：第三方日历 2026-08-26（TipRanks 等，非官方一手——G 级标注）；
# 取用使有效期更短（更保守），官宣后复核（R14.2：有效期 = min(财报日, 90 天)）
NEXT_EARNINGS_NVDA = dt.date(2026, 8, 26)
rules, params = load_rules(), load_params()

# ---------- 1. 签认落地：S4 复算（signed_off 生效） ----------
engine = S4Engine(rules, params)
SIGNED = {"护城河", "管理层"}

# 与 run_s4_period1_2026_07.py 完全一致的输入（快照复用），仅 signed_off 变化
CARDS_IN = {
    "NVDA": [("产业链位置", "high", 14), ("AI收入真实性", "high", 19), ("市场空间", "mid", 8),
             ("财务健康", "high", 9), ("拥挤度", "high", 5), ("估值", "high", 13),
             ("护城河", "high", 13)],
    "TSM": [("产业链位置", "high", 15), ("AI收入真实性", "high", 18), ("市场空间", "high", 9),
            ("财务健康", "high", 10), ("拥挤度", "mid", 3), ("估值", "low", 3),
            ("护城河", "high", 14)],
    "AVGO": [("产业链位置", "high", 13), ("AI收入真实性", "high", 18), ("市场空间", "high", 9),
             ("财务健康", "mid", 7), ("拥挤度", "mid", 3), ("估值", "low", 6),
             ("护城河", "high", 13)],
    "TER": [("产业链位置", "mid", 11), ("AI收入真实性", "mid", 15), ("市场空间", "mid", 8),
            ("财务健康", "high", 9), ("拥挤度", "low", 1), ("估值", "low", 2),
            ("护城河", "high", 13)],
    "AMD": [("产业链位置", "mid", 10), ("AI收入真实性", "high", 17), ("市场空间", "high", 9),
            ("财务健康", "high", 9), ("拥挤度", "low", 1), ("估值", "low", 2),
            ("护城河", "mid", 10)],
    "ARM": [("产业链位置", "high", 13), ("AI收入真实性", "mid", 13), ("市场空间", "high", 9),
            ("财务健康", "mid", 8), ("拥挤度", "low", 1), ("估值", "low", 1),
            ("护城河", "high", 13)],
    "MRVL": [("产业链位置", "mid", 10), ("AI收入真实性", "mid", 16), ("市场空间", "high", 9),
             ("财务健康", "mid", 7), ("拥挤度", "low", 1), ("估值", "low", 2),
             ("护城河", "mid", 10)],
    "KLAC": [("产业链位置", "high", 13), ("AI收入真实性", "mid", 13), ("市场空间", "mid", 7),
             ("财务健康", "mid", 8), ("拥挤度", "low", 1), ("估值", None, None),
             ("护城河", "high", 13)],
    "ALAB": [("产业链位置", "mid", 9), ("AI收入真实性", "high", 17), ("市场空间", "mid", 8),
             ("财务健康", "high", 9), ("拥挤度", "low", 0), ("估值", None, None),
             ("护城河", "mid", 9)],
    "FORM": [("产业链位置", "mid", 9), ("AI收入真实性", "mid", 13), ("市场空间", "mid", 7),
             ("财务健康", "high", 9), ("拥挤度", "low", 1), ("估值", "low", 2),
             ("护城河", "mid", 10)],
    "CRDO": [("产业链位置", "mid", 8), ("AI收入真实性", "high", 17), ("市场空间", "mid", 7),
             ("财务健康", "high", 9), ("拥挤度", "low", 0), ("估值", None, None),
             ("护城河", "mid", 8)],
}
S3_RECORDS = {
    "NVDA": ["D-1:反垄断四调查+循环投资→扣分输入(签认维持中档下限)"],
    "AMD": ["CEO减持10b5-1计划内(豁免)"], "CRDO": ["高管减持10b5-1(豁免)"], "ARM": ["软银质押(S3已档)"],
}
# 熊市拦截结果（S4 第 1 期三情景，M-EST-1 已签认）：symbol → bear_intercept
BEAR_INTERCEPT = {"NVDA": False, "TSM": True, "AVGO": True, "TER": True, "AMD": True,
                  "ARM": True, "MRVL": True, "FORM": True,
                  "KLAC": None, "ALAB": None, "CRDO": None}  # None=估值缺口无法计算

cards = {}
for sym, dims in CARDS_IN.items():
    inputs = [DimensionInput(n, band=b, points=p, gap=b is None) for n, b, p in dims]
    inputs.append(engine.assemble_management(sym, guidance_quarters=0, hit_ratio=None,
                                             s3_records=S3_RECORDS.get(sym, [])))
    cards[sym] = engine.score(sym, inputs, signed_off=SIGNED,
                              snapshot_id=f"SNAP-2026-07-S4P1-{sym}", on=ON)

print("== S4 签认后判定 ==")
for sym, c in sorted(cards.items(), key=lambda kv: -kv[1].total_counted):
    print(f"{sym}: {c.total_counted:g} → {c.judgement}"
          + ("（熊市拦截）" if BEAR_INTERCEPT.get(sym) else ""))

# ---------- 2. 市况分级（R11） ----------
# 读数依据（每项来源+日期见 docs/S5 报告§二；hit=None 为缺口，A2 不计命中）
REGIME_READINGS = [
    SignalReading("REGIME.SIG.INDEX_PS_PEG", None),           # 指数级 PS/PEG 权威口径未获得（缺口）
    SignalReading("REGIME.SIG.INDEX_TAM_IMPLIED", None),      # D7 反推口径未接入（缺口）
    # 涨幅腿 IBKR 一手：SOX 12月 +131.2%；上修腿多源 +8.6%~20% → 剪刀差 ≥111pp ≫ 50pp
    SignalReading("REGIME.SIG.INDEX_GAIN_VS_REVISION", True, evidence_id="EV-SOX-12M-IBKR"),
    # 评级极值腿成立（板块 60% 强买/NVDA 36买1持0卖）；密度环比翻倍腿未找到 → 缺口
    # ⚠ 若密度腿后续证实 → 命中数 5 → 狂热档（暂停新开仓）——已登记核证任务
    SignalReading("REGIME.SIG.RESEARCH_CONSENSUS_EXTREME", None),
    # H1'26 IPO 募资 $1141-1156 亿 vs 上年同期 $148 亿（7-8 倍）；SK 海力士 $265 亿纪录
    SignalReading("REGIME.SIG.IPO_PLACEMENT_SURGE", True, evidence_id="EV-IPO-H1-2026"),
    # 边际：DFII10 3.7 月窗口年化 ≈+127bp（>100）/5.7 月窗口 ≈+96bp（<100），
    # 规则库 conservative_direction="更早命中" → 计命中（级别对此判读不敏感：3或4项均过热）
    SignalReading("REGIME.SIG.REAL_RATE_SPIKE", True, evidence_id="EV-DFII10-2026H1"),
    SignalReading("REGIME.SIG.CREDIT_SPREAD_WIDENING", False),  # HY OAS 3月反而收窄 42-50bp
    # Fed 2026-06-17 连续第4次按兵不动、QT 已于 2025-12 结束；点阵图上修/删宽松措辞为
    # 鹰派前兆，但未达枚举事件类（加息启动/QT宣布/明确收紧声明）→ 不命中（列观察）
    SignalReading("REGIME.SIG.CB_TIGHTENING", False),
    SignalReading("REGIME.SIG.BREADTH_DETERIORATION", False),   # 新高/新低比 >1（349/154 等快照）
    # 2026-05 融资余额 $1.416 万亿名义新高；/GDP 4.1% vs 50年中位 1.5%；同比 +53.7%
    SignalReading("REGIME.SIG.MARGIN_DEBT_PCTILE", True, evidence_id="EV-FINRA-2026-05"),
]
CAPEX_DOWNGRADE = False  # MSFT/GOOGL/META/AMZN 全部上修或维持（合计 $7250 亿 +77%），零下修
INDEX_DRAWDOWN = round(12967.16 / 14655.29 - 1, 4)  # SOX 自高点回撤（IBKR 一手）

grader = RegimeGrader(rules)
assessment, event = grader.assess(REGIME_READINGS, CAPEX_DOWNGRADE, INDEX_DRAWDOWN,
                                  accounting_day="2026-07-13", snapshot_id="SNAP-2026-07-REGIME",
                                  previous_level=None, holdings=[])
print(f"\n== 市况 == {assessment.level}（命中 {assessment.hits} 项，缺口 {len(assessment.gaps)}）")
print("动作:", {k: v for k, v in assessment.actions.items() if k != "level"})

# ---------- 3. 风控联立（R15.1）：NVDA 买入提案 ----------
risk = RiskEngine(rules, params)
state = PortfolioState(holdings=(), cash_pct=100.0, leverage=0.0, regime=str(assessment.level))
proposal = Proposal(symbol="NVDA", target_weight_pct=10.0, first_batch_weight_pct=4.0,
                    stop_distance_pct=22.2, subindustry="AI算力芯片", theme="AI",
                    is_ai=True, is_shovel=True, small_cap=False)
violations = risk.validate_all(state, proposal, on=ON)
print("\n== 风控联立 ==", "通过（零违规）" if not violations else [f"{v.rule_id}:{v.detail}" for v in violations])

# ---------- 4. 意见生成（10.1/10.2）+ M10 合规（R14.3） ----------
factory = OpinionFactory(now=lambda: dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC))
checker = ComplianceChecker(params)

suspend_new = bool(assessment.actions.get("suspend_new_positions"))
double_review = bool(assessment.actions.get("new_position_double_review"))
opinions, blocked = [], []

if cards["NVDA"].judgement == "heavy" and not BEAR_INTERCEPT["NVDA"] and not violations and not suspend_new:
    op = factory.buy(
        score_line=params.resolve("score.heavy_line", ON),
        symbol="NVDA", market="US",
        basis_clauses=("F 3.4 评分≥75 重仓候选", "F 3.3 三情景（基准+107%/熊市-22.5%/PEG 0.55）",
                       "F 6.1 单票初始≤10%/风险预算≤2%", "F 10.1 买入行", "D-1 裁决（2026-07）"),
        evidence=(
            EvidenceItem(field_name="price", value="208.17 USD", source="IBKR 实时快照", as_of=ON),
            EvidenceItem(field_name="dc_revenue_yoy", value="FY27Q1 DC $752亿 +92%", source="NVIDIA PR/8-K", as_of=dt.date(2026, 5, 20)),
            EvidenceItem(field_name="gross_margin", value="nGAAP 75.0%", source="NVIDIA 8-K", as_of=dt.date(2026, 5, 20)),
            EvidenceItem(field_name="eps_consensus", value="FY27 $8.96 / FY28 $12.73", source="多终端汇总(TIKR/Yahoo/Zacks)", as_of=ON),
            EvidenceItem(field_name="ocf", value="$503亿 +83.6%", source="NVIDIA 10-Q", as_of=dt.date(2026, 4, 26)),
            EvidenceItem(field_name="ar_concentration", value="前三客户 64%", source="NVIDIA 10-Q", as_of=dt.date(2026, 4, 26)),
            EvidenceItem(field_name="momentum_12m", value="+27.2%（组内最低）", source="IBKR", as_of=ON),
        ),
        score=ScoreDetail(total=cards["NVDA"].total_counted,
                          dimensions={d.name: d.points for d in cards["NVDA"].dimensions}),
        scenarios=ScenarioTargets(
            bear="-22.5%（≈$161，18×FY27 EPS——签认口径 M-EST-1，未破 -30% 拦截线）",
            base="+107.2%（3 年 ≈$431，28×E3=15.41）",
            bull="+196.1%（≈$616，40×E3）"),
        position_plan=PositionPlan(target_low_pct=6.0, target_high_pct=10.0,
                                   batch_plan="三批建仓：首批 4% → 确认季报后 3% → 回踩或再确认 3%；"
                                              "任何一批不追高于前批成本 +15%"),
        stop_loss=StopLossCheck(
            stop_level="$162（52 周低点 161.99 下沿，距现价 -22.2%）",
            risk_budget_check="首批 4% × 22.2% = 0.89% ≤ 签署预算 2%（RISK.BUDGET.FIRST_BATCH 通过）"),
        risk_alert=("6.2 基本面恶化信号：0 命中。6.3 个股泡沫信号：PS/PEG 不命中（前瞻 PE 23x）、"
                    "涨幅vs上修不命中（+27.2% 组内最低）；评级一致买入极值存在（研报密度腿缺口）。"
                    "特别风险：前三客户应收 64% 集中、对客户循环投资 $15.9B（论点卡证伪候选）、"
                    "四项反垄断调查在途（D-1 条件化复核：升级为财务类执法即重回裁决队列）。"
                    "市况档位动作：见 S5 报告§二（过热档则本意见需双重复核后方可执行）。"),
        next_earnings_date=NEXT_EARNINGS_NVDA,  # 由代理确认；未官宣则 None → 90 天封顶
    )
    report = checker.run(op, on=ON)
    opinions.append((op, report))
else:
    blocked.append(("NVDA", "judgement/拦截/风控/市况 未全部放行"))

for sym in ("TSM", "AVGO", "TER"):
    blocked.append((sym, f"评分 {cards[sym].total_counted:g} 达线（{cards[sym].judgement}）但熊市拦截"
                         "（S4.SCENARIO.LINES：<-30% 不得给出买入意见）→ 观察，待价格/盈利关系修复复算"))

print("\n== 意见 ==")
for op, rep in opinions:
    print(f"{op.opinion_id} {op.symbol} {op.opinion_type} 有效期至 {op.valid_until} "
          f"M10:{'通过' if rep.passed else [f.check_id for f in rep.failures]}")
    if double_review:
        print("  ⚠ 过热档：新开仓意见需双重复核（REGIME.ACTION.OVERHEAT）")
print("== 拦截/未发 ==")
for sym, why in blocked:
    print(f"{sym}: {why}")

# ---------- 5. NVDA 论点卡草稿（R13.1，draft 态待签认） ----------
if opinions:
    card = ThesisCard(
        card_id="THESIS-NVDA-2026-07-v1", symbol="NVDA",
        one_liner="AI 算力平台垄断者：生态锁定+代际领先下，盈利增速持续跑赢股价，估值未透支",
        hypotheses=(
            Hypothesis("H1", "数据中心收入高增速延续", "FY27Q2 与 FY27Q3 数据中心收入 YoY ≥ +50%",
                       dt.date(2026, 12, 31),
                       strengthen_criterion="连续两季超一致预期且毛利率 ≥74%",
                       weaken_criterion="任一季 YoY 落入 +30~50% 或指引下修", is_core=True),
            Hypothesis("H2", "盈利预期不塌方", "FY27 一致 EPS 维持 ≥ $8.96（不下修超 10%）",
                       dt.date(2026, 12, 31),
                       strengthen_criterion="一致 EPS 上修 ≥5%",
                       weaken_criterion="下修 5-10%（未破证伪线）"),
            Hypothesis("H3", "应收质量不恶化", "前三客户应收占比 ≤ 64% 且无重大坏账计提（每季 10-Q 核对）",
                       dt.date(2027, 1, 31),
                       strengthen_criterion="集中度降至 <55%",
                       weaken_criterion="升至 64-70%"),
            Hypothesis("H4", "监管风险不升级", "四项反垄断调查 0 项升级为财务类执法（D-1 条件化复核）",
                       dt.date(2027, 5, 31),
                       strengthen_criterion="任一调查了结/和解且非财务类",
                       weaken_criterion="新增正式指控但非财务类"),
        ),
        scenarios=ScenarioTriple(bear="-22.5%/$161", base="+107%/$431(3年)", bull="+196%/$616"),
        falsification=("①任一季 DC YoY <+20% 或毛利率 <65%；②前三客户应收 >70% 或重大坏账；"
                       "③对客户循环投资被证实实质性虚增收入；④反垄断升级为财务类执法（重回裁决队列，D-1）。"
                       "任一成立 → 清仓意见（R12.3/R13.4），走第十章流程"),
        sell_plan=("跌破 65 分卖出（R10.4）；跌入 65-74 减至试探仓上限；6.3 信号 ≥3 项分批兑现；"
                   "硬止损 $162；狂热档执行全持仓分批兑现评估"),
        linked_opinion_id=opinions[0][0].opinion_id,
    )
    print(f"\n== 论点卡 == {card.card_id} state={card.state}（买入执行前须签认——S8 守卫强制，R13.2）")

# ---------- 6. 推荐清单（F 3.5-4：≤ 持仓上限×150% = 12） ----------
cap = int(params.resolve("portfolio.max_holdings", ON) * params.resolve("reco.list_multiplier", ON))
reco = [(s, cards[s].total_counted, cards[s].judgement,
         "可执行" if any(o.symbol == s for o, _ in opinions) else "熊市拦截-观察")
        for s in ("NVDA", "TSM", "AVGO", "TER")]
assert len(reco) <= cap
print(f"\n== 推荐清单（{len(reco)}/{cap}） ==")
for row in reco:
    print(row)

print("\n== 组合层 ==", json.dumps({
    "现金": f"90%（NVDA 满仓 10% 后），下限 {params.resolve('cash.floor_normal_pct', ON):g}%（常态）/"
           f"{params.resolve('cash.floor_overheat_pct', ON):g}%（过热）均满足",
    "杠杆": 0, "月交易预算": "6 次（本期拟用 1 次首批）",
}, ensure_ascii=False))
