"""持仓信号监测器与观察期（任务 20，需求 R12；框架 v2.0 §6.1/6.2/6.3/2.3-2、规格书 §6.5、design §3.4）。

- 检测器（R12.1）：6.2 六项恶化 + 6.3 五项泡沫检测器常驻，阈值全部取自规则库
  ``rules/library/position_signals.yaml``（R5.1：除红线断言层外代码不硬编码阈值）；
  运行频率登记见 :func:`frequency_registry`（数值项=财报事件+日收盘、事件项=代理周扫+披露事件、
  研报密度=周更）。
- 活数据白名单（R3.5）：本监测器即 ``SignalDetector`` 白名单调用方，可读非快照数据，
  但输出只能是触发事件（写入 orchestration.triggers 总线）与动作指令建议——判定（意见对象
  构造）必须经事件快照走 S11（R3.2），指令仅是路由载荷，不得直接作为判定输入。
- 48h 复评倒计时（R12.2）：墙钟 deadline（``Duration.parse("48h")``，R4.3）持久化语义——
  重启恢复从存储的 deadline 继续（:meth:`PositionSignalMonitor.restore_countdowns`），不重算；
  逾期产 :class:`FreezeAdvice`（标的冻结 + 全局新开仓冻结建议，对接 orchestration.freeze 的
  ``reeval_48h_overdue`` 双冻结语义，不直接改写冻结矩阵）。
- 复评二选一（R12.3）：结论枚举 :class:`ReevalConclusion` 仅有 CONFIRMED（→清仓意见指令）与
  UNCONFIRMED（→减半意见指令 + 观察期对象）两个成员——类型层保证不存在第三选项（C-10 统一）。
  观察期为框架口径"一个季度"（自然日，R4.3），以 ``Duration.parse("90d")`` 90 自然日落地，
  落地口径留痕于规则条目 SIG62.ACTION.REEVAL_BINARY 与 :class:`ObservationPeriod` 注释。
- 观察期（R12.4）：期内任一 6.2 信号再命中 → 直接清仓指令（跳过 48h 复评，防守类，A5）；
  到期无再命中自动解除并留痕（:meth:`PositionSignalMonitor.sweep_observations`）。
- 6.3 阈值动作（R12.5）：命中 ≥2 → 减仓评估指令；≥3 → 分批兑现指令（高位吸收低位）。
- 价格触发（R12.6）：日收盘对持仓核对——跌破预设止损位 → 无条件减半指令 + 48h 复评启动
  （防守类，6.6 顺位 3，A5）；跌破成本价 -20% 重审线 → 强制重审任务；上穿牛市目标价 →
  分批兑现评估指令。
- 颠覆名单联动（R12.7）：持仓标的被列入被颠覆名单 → 立即强制复评事件（F 2.3-2）。

输入证据指标一律允许 ``None`` = 缺口：不计命中并产 :class:`~invest_assistant.core.types.Gap`
（A2/R2.6，禁止插值）。本模块只产出触发事件与 :class:`MonitorDirective`，
意见对象由 opinion.factory 构造（R14.1）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ..calendar import CalendarService, Duration
from ..core.types import Gap
from ..data.snapshot import read_live
from ..orchestration.freeze import FreezeFlags
from ..orchestration.triggers import TriggerBus
from ..rules.engine import RuleRepository

#: 活数据白名单调用方标识（R3.5，data.snapshot.LIVE_READERS 成员）
CALLER_ID = "SignalDetector"

#: 6.2 六项恶化检测器规则 ID（框架 6.2 ①-⑥）
DETECTOR_IDS_62: tuple[str, ...] = (
    "SIG62.REV_GUIDANCE",
    "SIG62.GROSS_MARGIN",
    "SIG62.KEY_CUSTOMER",
    "SIG62.OCF_DIVERGENCE",
    "SIG62.AR_INVENTORY",
    "SIG62.GOVERNANCE",
)

#: 6.3 五项泡沫检测器规则 ID（框架 6.3 ①-⑤）
DETECTOR_IDS_63: tuple[str, ...] = (
    "SIG63.PS_PEG",
    "SIG63.TAM_SHARE",
    "SIG63.PRICE_VS_EARNINGS",
    "SIG63.RESEARCH_DENSITY",
    "SIG63.ISSUE_SELL_CONCURRENT",
)

#: 48h 复评逾期对应的标的冻结标志名（对接 orchestration.freeze 语义，R12.2）
OVERDUE_FLAG = "reeval_48h_overdue"
assert OVERDUE_FLAG in FreezeFlags.INSTRUMENT_FLAGS  # 语义对接校验：freeze.py 改名时此处即失败


def frequency_registry(rules: RuleRepository) -> dict[str, tuple[str, ...]]:
    """检测器 → 运行频率登记（R12.1；规格书 §6.5 频率行）。

    数值项=财报事件+日收盘；事件项=代理周扫+披露事件；研报密度=周更。
    频率类别定义取自规则条目 SIG.FREQ.REGISTRY，逐检测器 cadence 取自各自条目。
    """
    cadences = rules.get("SIG.FREQ.REGISTRY").params["cadences"]
    return {
        rid: tuple(cadences[rules.get(rid).params["cadence"]])
        for rid in DETECTOR_IDS_62 + DETECTOR_IDS_63
    }


# ---------------------------------------------------------------- 检测输入与产物


@dataclass(frozen=True)
class Metrics62:
    """6.2 证据指标输入（R12.1）。任一字段 ``None`` = 缺口：不计命中并产 Gap（A2/R2.6）。"""

    # ① 收入 vs 指引（低于指引的偏离为正值，%；连续两季口径 → 最近季 + 上一季两字段）
    revenue_guidance_deviation_pct: float | None = None
    prior_revenue_guidance_deviation_pct: float | None = None
    # ② 毛利率非季节性下滑（下滑为正值，pp；同季同比 + 环比双口径）
    gross_margin_decline_yoy_pp: float | None = None
    gross_margin_decline_qoq_pp: float | None = None
    margin_decline_seasonal: bool | None = None  # 季节性归因结论（True=季节性可解释，不计命中）
    # ③ 大客户流失/标志性订单被夺（代理事件取证结论，R16 任务⑥）
    key_customer_loss: bool | None = None
    # ④ OCF 与净利连续背离季数（经 R2.4/R2.5 归一化）
    ocf_divergence_quarters: int | None = None
    # ⑤ 应收/存货同比 − 收入同比（pp），含合同资产口径开关标注
    ar_excess_over_revenue_pp: float | None = None
    inventory_excess_over_revenue_pp: float | None = None
    contract_asset_scope_on: bool | None = None  # 口径开关未标注（None）视为口径缺口
    # ⑥ 高管离职/大股东密集减持/审计异动（Form 4 聚合 + 8-K + 代理，"或"枚举）
    executive_departure: bool | None = None
    insider_sell_cluster: bool | None = None
    audit_anomaly: bool | None = None


@dataclass(frozen=True)
class Metrics63:
    """6.3 证据指标输入（R12.1）。任一字段 ``None`` = 缺口：不计命中并产 Gap（A2/R2.6）。"""

    # ① 非超高速增长公司 PS>30 或 PEG>3 持续一季
    ps: float | None = None
    peg: float | None = None
    revenue_growth_yoy_pct: float | None = None  # 适用前提：<40%
    valuation_persist_quarters: int | None = None  # PS/PEG 超线持续季数
    # ② 市值/TAM 反推份额（%）
    implied_tam_share_pct: float | None = None
    # ③ 12 个月涨幅（%）与盈利上修幅度（%）
    price_gain_12m_pct: float | None = None
    earnings_revision_12m_pct: float | None = None
    # ④ 研报密度环比倍数 + 一致买入
    research_density_ratio: float | None = None
    consensus_all_buy: bool | None = None
    # ⑤ 高位定增 + 大股东减持并发（"且"）
    equity_offering_at_high: bool | None = None
    insider_selling_concurrent: bool | None = None


@dataclass(frozen=True)
class SignalHit:
    """一次检测器命中：规则条目 ID + 命中明细（A4 可回溯）。"""

    rule_id: str
    subject: str
    detail: str


@dataclass(frozen=True)
class DetectionReport:
    """一次检测批次产物：命中清单 + 缺口清单（缺口不计命中，R2.6/A2）。"""

    subject: str
    hits: tuple[SignalHit, ...]
    gaps: tuple[Gap, ...]

    @property
    def hit_ids(self) -> tuple[str, ...]:
        return tuple(h.rule_id for h in self.hits)


class _Collector:
    """检测过程的命中/缺口收集器（内部工具）。"""

    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.hits: list[SignalHit] = []
        self.gaps: list[Gap] = []

    def hit(self, rule_id: str, detail: str) -> None:
        self.hits.append(SignalHit(rule_id=rule_id, subject=self.subject, detail=detail))

    def gap(self, rule_id: str, field_name: str) -> None:
        self.gaps.append(
            Gap(
                field_name=field_name,
                subject=self.subject,
                attempted_paths=(rule_id,),
                degradation="缺口不计命中，禁止插值（A2/R2.6）",
            )
        )

    def report(self) -> DetectionReport:
        return DetectionReport(subject=self.subject, hits=tuple(self.hits), gaps=tuple(self.gaps))


def detect_62(subject: str, m: Metrics62, rules: RuleRepository) -> DetectionReport:
    """6.2 六项恶化检测（R12.1；框架 6.2 ①-⑥）。

    输入证据指标 ``None`` = 缺口：该项不计命中并产 Gap（A2）；阈值取自规则库。
    """
    c = _Collector(subject)

    # ① 收入连续两季显著低于指引：偏离 ≥10%（F 6.2-①）
    p = rules.get("SIG62.REV_GUIDANCE").params
    dev, prior = m.revenue_guidance_deviation_pct, m.prior_revenue_guidance_deviation_pct
    if dev is None:
        c.gap("SIG62.REV_GUIDANCE", "revenue_guidance_deviation_pct")
    elif prior is None:
        c.gap("SIG62.REV_GUIDANCE", "prior_revenue_guidance_deviation_pct")
    elif dev >= p["deviation_pct_ge"] and prior >= p["deviation_pct_ge"]:
        c.hit(
            "SIG62.REV_GUIDANCE",
            f"收入连续 {p['consecutive_quarters']} 季低于指引 ≥{p['deviation_pct_ge']}%"
            f"（上季 {prior}% / 最近季 {dev}%）",
        )

    # ② 毛利率非季节性下滑 ≥3pp，同季同比 + 环比双口径任一达线（F 6.2-②，同级取严）
    p = rules.get("SIG62.GROSS_MARGIN").params
    declines = {
        "yoy_same_quarter": m.gross_margin_decline_yoy_pp,
        "qoq": m.gross_margin_decline_qoq_pp,
    }
    for caliber, value in declines.items():
        if value is None:
            c.gap("SIG62.GROSS_MARGIN", f"gross_margin_decline_{caliber}")
    reaching = [k for k, v in declines.items() if v is not None and v >= p["decline_pp_ge"]]
    if reaching:
        if m.margin_decline_seasonal is None:
            c.gap("SIG62.GROSS_MARGIN", "margin_decline_seasonal")  # 季节性归因不可判定 → 缺口
        elif not m.margin_decline_seasonal:
            c.hit(
                "SIG62.GROSS_MARGIN",
                f"毛利率非季节性下滑 ≥{p['decline_pp_ge']}pp（口径命中：{'、'.join(reaching)}）",
            )

    # ③ 大客户流失/标志性订单被夺——代理事件取证（F 6.2-③）
    if m.key_customer_loss is None:
        c.gap("SIG62.KEY_CUSTOMER", "key_customer_loss")
    elif m.key_customer_loss:
        c.hit("SIG62.KEY_CUSTOMER", "大客户流失/标志性订单被夺（代理事件取证结论）")

    # ④ OCF 与净利连续 4 季背离（F 6.2-④，归一化引擎口径）
    p = rules.get("SIG62.OCF_DIVERGENCE").params
    if m.ocf_divergence_quarters is None:
        c.gap("SIG62.OCF_DIVERGENCE", "ocf_divergence_quarters")
    elif m.ocf_divergence_quarters >= p["divergence_quarters_ge"]:
        c.hit(
            "SIG62.OCF_DIVERGENCE",
            f"OCF 与净利连续 {m.ocf_divergence_quarters} 季背离（≥{p['divergence_quarters_ge']}）",
        )

    # ⑤ 应收/存货同比超收入 20pp（F 6.2-⑤，含合同资产口径开关）
    p = rules.get("SIG62.AR_INVENTORY").params
    ar_items = {
        "ar_excess_over_revenue_pp": m.ar_excess_over_revenue_pp,
        "inventory_excess_over_revenue_pp": m.inventory_excess_over_revenue_pp,
    }
    for name, value in ar_items.items():
        if value is None:
            c.gap("SIG62.AR_INVENTORY", name)
    over = [k for k, v in ar_items.items() if v is not None and v >= p["excess_over_revenue_pp_ge"]]
    if over:
        if m.contract_asset_scope_on is None:
            c.gap("SIG62.AR_INVENTORY", "contract_asset_scope_on")  # 口径开关未标注 → 缺口
        else:
            scope = "含合同资产" if m.contract_asset_scope_on else "不含合同资产"
            c.hit(
                "SIG62.AR_INVENTORY",
                f"应收/存货同比超收入 ≥{p['excess_over_revenue_pp_ge']}pp（{'、'.join(over)}；口径：{scope}）",
            )

    # ⑥ 高管离职/大股东密集减持/审计异动——"或"枚举（F 6.2-⑥）
    symptoms = {
        "executive_departure": m.executive_departure,
        "insider_sell_cluster": m.insider_sell_cluster,
        "audit_anomaly": m.audit_anomaly,
    }
    for name, value in symptoms.items():
        if value is None:
            c.gap("SIG62.GOVERNANCE", name)
    fired = [k for k, v in symptoms.items() if v]
    if fired:
        c.hit("SIG62.GOVERNANCE", f"治理异动命中：{'、'.join(fired)}（'或'枚举）")

    return c.report()


def detect_63(subject: str, m: Metrics63, rules: RuleRepository) -> DetectionReport:
    """6.3 五项泡沫检测（R12.1；框架 6.3 ①-⑤）。``None`` = 缺口不计命中 + 产 Gap（A2）。"""
    c = _Collector(subject)

    # ① 非超高速增长公司 PS>30 或 PEG>3 持续一季（F 6.3-①）
    p = rules.get("SIG63.PS_PEG").params
    if m.revenue_growth_yoy_pct is None:
        c.gap("SIG63.PS_PEG", "revenue_growth_yoy_pct")  # 适用前提不可判定 → 缺口
    elif m.revenue_growth_yoy_pct < p["applies_below_revenue_growth_pct"]:
        if m.ps is None and m.peg is None:
            c.gap("SIG63.PS_PEG", "ps/peg")
        else:
            over_ps = m.ps is not None and m.ps > p["ps_gt"]
            over_peg = m.peg is not None and m.peg > p["peg_gt"]
            if over_ps or over_peg:
                if m.valuation_persist_quarters is None:
                    c.gap("SIG63.PS_PEG", "valuation_persist_quarters")
                elif m.valuation_persist_quarters >= p["persist_quarters_ge"]:
                    c.hit(
                        "SIG63.PS_PEG",
                        f"PS>{p['ps_gt']} 或 PEG>{p['peg_gt']} 持续 {m.valuation_persist_quarters} 季"
                        f"（PS={m.ps}, PEG={m.peg}，收入增速 {m.revenue_growth_yoy_pct}%<"
                        f"{p['applies_below_revenue_growth_pct']}%）",
                    )
    # 收入增速 ≥40%：超高速增长公司不适用本项（非缺口、非命中）

    # ② 市值/TAM 反推份额 >50%（F 6.3-②）
    p = rules.get("SIG63.TAM_SHARE").params
    if m.implied_tam_share_pct is None:
        c.gap("SIG63.TAM_SHARE", "implied_tam_share_pct")
    elif m.implied_tam_share_pct > p["implied_tam_share_pct_gt"]:
        c.hit(
            "SIG63.TAM_SHARE",
            f"市值反推需占 TAM {m.implied_tam_share_pct}% > {p['implied_tam_share_pct_gt']}%",
        )

    # ③ 12 个月涨幅超盈利上修 50pp（F 6.3-③，含边界取严）
    p = rules.get("SIG63.PRICE_VS_EARNINGS").params
    if m.price_gain_12m_pct is None:
        c.gap("SIG63.PRICE_VS_EARNINGS", "price_gain_12m_pct")
    elif m.earnings_revision_12m_pct is None:
        c.gap("SIG63.PRICE_VS_EARNINGS", "earnings_revision_12m_pct")
    elif m.price_gain_12m_pct - m.earnings_revision_12m_pct >= p["gap_pp_ge"]:
        c.hit(
            "SIG63.PRICE_VS_EARNINGS",
            f"12 月涨幅 {m.price_gain_12m_pct}% 超盈利上修 {m.earnings_revision_12m_pct}% 达 "
            f"{m.price_gain_12m_pct - m.earnings_revision_12m_pct:.1f}pp（≥{p['gap_pp_ge']}pp）",
        )

    # ④ 研报密度环比翻倍且一致买入（F 6.3-④，两条件为"且"）
    p = rules.get("SIG63.RESEARCH_DENSITY").params
    if m.research_density_ratio is None:
        c.gap("SIG63.RESEARCH_DENSITY", "research_density_ratio")
    elif m.consensus_all_buy is None:
        c.gap("SIG63.RESEARCH_DENSITY", "consensus_all_buy")
    elif m.research_density_ratio >= p["density_ratio_ge"] and m.consensus_all_buy:
        c.hit(
            "SIG63.RESEARCH_DENSITY",
            f"研报密度环比 ×{m.research_density_ratio}（≥{p['density_ratio_ge']}）且一致买入",
        )

    # ⑤ 高位定增 + 大股东减持并发（F 6.3-⑤，两事件为"且"）
    if m.equity_offering_at_high is None:
        c.gap("SIG63.ISSUE_SELL_CONCURRENT", "equity_offering_at_high")
    elif m.insider_selling_concurrent is None:
        c.gap("SIG63.ISSUE_SELL_CONCURRENT", "insider_selling_concurrent")
    elif m.equity_offering_at_high and m.insider_selling_concurrent:
        c.hit("SIG63.ISSUE_SELL_CONCURRENT", "高位定增与大股东减持并发")

    return c.report()


# ---------------------------------------------------------------- 动作指令与状态对象


class MonitorAction(StrEnum):
    """监测器产出的意见/任务指令类别（意见对象由 opinion.factory 构造，R14.1）。"""

    LIQUIDATE = "清仓"
    HALVE = "减半"
    UNCONDITIONAL_HALVE = "无条件减半"
    REDUCE_EVAL = "减仓评估"
    STAGED_TAKE_PROFIT = "分批兑现"
    STAGED_TAKE_PROFIT_EVAL = "分批兑现评估"
    FORCED_REREVIEW = "强制重审"
    FORCED_REEVAL = "强制复评"


class ReevalConclusion(StrEnum):
    """48h 复评二选一结论（R12.3，框架 6.2/C-10 统一）——类型层保证无第三选项。

    CONFIRMED：确认命中 3.2 红旗 → 立即清仓意见；
    UNCONFIRMED：证据不足确认 → 减半意见 + 观察期（一个季度自然日，90d 落地）。
    """

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class ObservationPeriod:
    """观察期状态对象（R12.3/R12.4，置于 instrument 上；规格书 §5.1/§6.6）。

    时长为框架口径"一个季度"（自然日，R4.3）——以 ``Duration.parse("90d")`` 90 自然日落地，
    落地口径同时留痕于规则条目 SIG62.ACTION.REEVAL_BINARY 的 observation_window 注释。
    ``active``/``release_note`` 的变更经 :class:`PositionSignalMonitor` 留痕（A4）。
    """

    subject: str
    start: dt.date
    end: dt.date  # start + 90 自然日


@dataclass(frozen=True)
class MonitorDirective:
    """监测器动作指令（R12.3-R12.7）：路由至意见工厂/任务队列，不直接作为判定输入（R3.5）。

    ``defensive``/``freeze_exempt``：防守类不受任何冻结阻断（铁律 A5）；
    ``skip_48h_reeval``：观察期内再命中的直清仓路径标注（R12.4）。
    """

    action: MonitorAction
    subject: str
    reason: str
    rule_ids: tuple[str, ...] = ()
    defensive: bool = False
    freeze_exempt: bool = False
    skip_48h_reeval: bool = False
    trigger_event_id: str = ""
    observation: ObservationPeriod | None = None


@dataclass(frozen=True)
class ReevalCountdown:
    """48h 复评倒计时（R12.2）：墙钟 deadline 为持久化字段——重启恢复从存储值继续，不重算。"""

    subject: str
    started_at: dt.datetime
    deadline: dt.datetime  # started_at + Duration.parse("48h")，墙钟口径（R4.3）
    trigger_event_id: str = ""
    source_rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreezeAdvice:
    """48h 复评逾期的冻结标志建议（R12.2 双冻结）：标的冻结 + 全局新开仓冻结。

    对接 orchestration.freeze 语义：``instrument_flag`` 即 FreezeFlags 的
    ``reeval_48h_overdue``，其置位同时经 ``global_new_open_frozen`` 派生组合级新开仓冻结。
    """

    subject: str
    note: str
    instrument_flag: str = OVERDUE_FLAG
    freeze_global_new_open: bool = True

    def apply_to(self, flags: FreezeFlags) -> None:
        """将建议落到冻结标志位（由编排层调用；防守类动作仍不受阻断，A5）。"""
        flags.mark_reeval_48h_overdue(self.subject, note=self.note)


# ---------------------------------------------------------------- 监测器


class PositionSignalMonitor:
    """持仓信号监测器（design §3.4）：检测 → 触发事件 → 48h 倒计时 → 二选一复评 → 观察期。

    输出仅两类：触发事件（写入 :class:`TriggerBus`，R3.5）与 :class:`MonitorDirective`
    指令建议；全部状态变更追加 ``audit_log`` 留痕（A4）。
    """

    def __init__(
        self,
        rules: RuleRepository,
        bus: TriggerBus,
        calendar: CalendarService,
        holdings: Iterable[str] = (),
    ) -> None:
        read_live(CALLER_ID)  # 活数据白名单闸门（R3.5）：本监测器是登记的白名单调用方
        self.rules = rules
        self.bus = bus
        self.calendar = calendar
        self.holdings: set[str] = set(holdings)
        self.countdowns: dict[str, ReevalCountdown] = {}
        self.observations: dict[str, ObservationPeriod] = {}
        self.audit_log: list[dict[str, str]] = []
        self._reeval_window: Duration = Duration.parse(
            rules.get("SIG62.ACTION.REEVAL_48H").params["reeval_window"]
        )
        self._observation_window: Duration = Duration.parse(
            rules.get("SIG62.ACTION.REEVAL_BINARY").params["observation_window"]
        )

    def _log(self, op: str, subject: str, note: str) -> None:
        self.audit_log.append({"op": op, "subject": subject, "note": note})

    # -------------------------------------------------- 6.2：命中 → 48h / 观察期直清仓

    def process_62(
        self, report: DetectionReport, now: dt.datetime, accounting_day: str
    ) -> MonitorDirective | None:
        """处理一次 6.2 检测报告（R12.2/R12.4）。

        - 观察期内任一 6.2 信号再命中 → 直接清仓指令（跳过 48h 复评，防守类，A5）；
        - 否则命中任一 → 登记触发事件并启动 48h 复评倒计时（墙钟），返回 None，
          复评结论经 :meth:`conclude_reeval` 收口（R12.3）。
        """
        if not report.hits:
            return None
        subject = report.subject
        obs = self.observations.get(subject)
        if obs is not None:
            ev = self.bus.register(
                kind="observation_direct_liquidation",
                subject=subject,
                accounting_day=accounting_day,
                payload={"hits": list(report.hit_ids), "observation_start": obs.start.isoformat()},
                source=CALLER_ID,
            )
            del self.observations[subject]
            self._log(
                "observation_rehit_liquidate",
                subject,
                f"观察期内 6.2 再命中 {'/'.join(report.hit_ids)} → 直接清仓，跳过 48h 复评（R12.4）",
            )
            return MonitorDirective(
                action=MonitorAction.LIQUIDATE,
                subject=subject,
                reason=(
                    f"观察期内 6.2 信号再命中（{'、'.join(report.hit_ids)}）→ 直接清仓，"
                    "跳过 48h 复评（R12.4/F 6.2；防守类不受冻结阻断，A5）"
                ),
                rule_ids=("SIG62.ACTION.OBSERVATION", *report.hit_ids),
                defensive=True,
                freeze_exempt=True,
                skip_48h_reeval=True,
                trigger_event_id=ev.event_id,
            )
        ev = self.bus.register(
            kind="signal_62_reeval",
            subject=subject,
            accounting_day=accounting_day,
            payload={"hits": list(report.hit_ids)},
            source=CALLER_ID,
        )
        self.start_countdown(subject, now, ev.event_id, report.hit_ids)
        return None

    def start_countdown(
        self,
        subject: str,
        now: dt.datetime,
        trigger_event_id: str = "",
        source_rule_ids: tuple[str, ...] = (),
    ) -> ReevalCountdown:
        """启动 48h 复评倒计时（R12.2，墙钟）；同标的已有倒计时则保留更早 deadline（取严）。"""
        existing = self.countdowns.get(subject)
        deadline = self.calendar.add_wall_clock(now, self._reeval_window)
        if existing is not None and existing.deadline <= deadline:
            self._log("countdown_keep", subject, f"已有更早 deadline {existing.deadline.isoformat()}，保留（取严）")
            return existing
        cd = ReevalCountdown(
            subject=subject,
            started_at=now,
            deadline=deadline,
            trigger_event_id=trigger_event_id,
            source_rule_ids=source_rule_ids,
        )
        self.countdowns[subject] = cd
        self._log("countdown_start", subject, f"48h 复评倒计时，deadline={deadline.isoformat()}（R12.2）")
        return cd

    def restore_countdowns(self, records: Iterable[ReevalCountdown]) -> None:
        """重启恢复（R12.2 持久化语义）：从存储的墙钟 deadline 继续计时，不以恢复时刻重算。"""
        for r in records:
            self.countdowns[r.subject] = r
            self._log("countdown_restore", r.subject, f"重启恢复，沿用存储 deadline={r.deadline.isoformat()}")

    def sweep_overdue(self, now: dt.datetime) -> list[FreezeAdvice]:
        """扫描逾期倒计时（R12.2）：逾期未处理 → 产'标的冻结 + 全局新开仓冻结'标志建议。"""
        advices = []
        for cd in self.countdowns.values():
            if now > cd.deadline:
                advices.append(
                    FreezeAdvice(
                        subject=cd.subject,
                        note=(
                            f"48h 复评逾期（deadline={cd.deadline.isoformat()}）→ 冻结该标的加仓"
                            "与组合新开仓（R12.2/F 10.3；防守类动作不受阻断，A5）"
                        ),
                    )
                )
                self._log("reeval_overdue", cd.subject, "48h 复评逾期，产双冻结建议（R12.2）")
        return advices

    def conclude_reeval(
        self, subject: str, conclusion: ReevalConclusion, now: dt.datetime
    ) -> MonitorDirective:
        """复评收口（R12.3）：结论强制二选一，无第三选项（类型层枚举保证）。

        CONFIRMED → 清仓意见指令；UNCONFIRMED → 减半意见指令 + 观察期状态对象
        （一个季度自然日，经 ``Duration.parse("90d")`` 落地）。倒计时同时清除
        （冻结标志的清除由编排层调 ``FreezeFlags.clear_reeval_48h_overdue``）。
        """
        if not isinstance(conclusion, ReevalConclusion):
            raise TypeError(
                f"复评结论必须是 ReevalConclusion 二选一（CONFIRMED/UNCONFIRMED），"
                f"收到 {conclusion!r}——不存在第三选项（R12.3/C-10）"
            )
        self.countdowns.pop(subject, None)
        if conclusion is ReevalConclusion.CONFIRMED:
            self._log("reeval_confirmed", subject, "复评确认命中 3.2 红旗 → 立即清仓（R12.3）")
            return MonitorDirective(
                action=MonitorAction.LIQUIDATE,
                subject=subject,
                reason="复评确认命中 3.2 红旗 → 立即生成清仓意见（R12.3/F 6.2；防守类，A5）",
                rule_ids=("SIG62.ACTION.REEVAL_BINARY",),
                defensive=True,
                freeze_exempt=True,
            )
        start = now.date()
        # 观察期终点：自然日口径与市场日历无关（R4.3），任一已接入市场传参均等价
        end = self.calendar.add("US", start, self._observation_window)
        assert isinstance(end, dt.date)
        obs = ObservationPeriod(subject=subject, start=start, end=end)
        self.observations[subject] = obs
        self._log(
            "observation_start",
            subject,
            f"复评证据不足确认 → 减半 + 观察期至 {end.isoformat()}"
            "（框架口径'一个季度'，以 90 自然日落地，R12.3/R4.3）",
        )
        return MonitorDirective(
            action=MonitorAction.HALVE,
            subject=subject,
            reason=(
                "复评证据不足确认 → 生成减半意见并置观察期一个季度"
                f"（90 自然日，至 {end.isoformat()}；R12.3/F 6.2；防守类，A5）"
            ),
            rule_ids=("SIG62.ACTION.REEVAL_BINARY",),
            defensive=True,
            freeze_exempt=True,
            observation=obs,
        )

    def sweep_observations(self, today: dt.date) -> list[ObservationPeriod]:
        """观察期到期扫描（R12.4）：到期无再命中 → 自动解除并留痕，返回解除清单。"""
        released = []
        for subject, obs in list(self.observations.items()):
            if today >= obs.end:
                del self.observations[subject]
                released.append(obs)
                self._log(
                    "observation_release",
                    subject,
                    f"观察期（{obs.start.isoformat()}~{obs.end.isoformat()}）到期无再命中，自动解除（R12.4 留痕）",
                )
        return released

    # -------------------------------------------------- 6.3：≥2 减仓评估 / ≥3 分批兑现

    def evaluate_63(self, report: DetectionReport, accounting_day: str) -> MonitorDirective | None:
        """6.3 命中数阈值动作（R12.5）：≥3 分批兑现（高位吸收）> ≥2 减仓评估。"""
        p = self.rules.get("SIG63.ACTION.THRESHOLDS").params
        n = len(report.hits)
        if n >= p["staged_take_profit_hits_ge"]:
            ev = self.bus.register(
                kind="signal_63_staged_take_profit",
                subject=report.subject,
                accounting_day=accounting_day,
                payload={"hits": list(report.hit_ids)},
                source=CALLER_ID,
            )
            return MonitorDirective(
                action=MonitorAction.STAGED_TAKE_PROFIT,
                subject=report.subject,
                reason=f"6.3 命中 {n} 项 ≥{p['staged_take_profit_hits_ge']} → 分批兑现意见（R12.5/F 7.3）",
                rule_ids=("SIG63.ACTION.THRESHOLDS", *report.hit_ids),
                defensive=True,
                freeze_exempt=True,
                trigger_event_id=ev.event_id,
            )
        if n >= p["reduce_eval_hits_ge"]:
            ev = self.bus.register(
                kind="signal_63_reduce_eval",
                subject=report.subject,
                accounting_day=accounting_day,
                payload={"hits": list(report.hit_ids)},
                source=CALLER_ID,
            )
            return MonitorDirective(
                action=MonitorAction.REDUCE_EVAL,
                subject=report.subject,
                reason=f"6.3 命中 {n} 项 ≥{p['reduce_eval_hits_ge']} → 减仓评估意见（R12.5/F 6.3）",
                rule_ids=("SIG63.ACTION.THRESHOLDS", *report.hit_ids),
                defensive=True,
                freeze_exempt=True,
                trigger_event_id=ev.event_id,
            )
        return None

    # -------------------------------------------------- 价格触发（R12.6）

    def check_price_triggers(
        self, pos: PositionPricing, now: dt.datetime, accounting_day: str
    ) -> tuple[list[MonitorDirective], list[Gap]]:
        """价格触发检测（R12.6，按标的市场日收盘评估）。

        - 跌破预设止损位（含边界，防守方向取严）→ 无条件减半指令 + 48h 复评启动
          （防守类，6.6 顺位 3，A5）；
        - 跌破成本价 -20% 重审线（含边界取严，阈值取自 PRICE.REREVIEW_LINE）→ 强制重审任务；
        - 上穿牛市目标价（含边界取严）→ 分批兑现评估指令。
        阈值位缺失（None）→ 产 Gap，不触发（A2）。
        """
        directives: list[MonitorDirective] = []
        gaps: list[Gap] = []
        subject = pos.subject

        if pos.stop_loss is None:
            gaps.append(Gap("stop_loss", subject, ("PRICE.STOP_LOSS",), "预设止损位缺失，无法核对（A2）"))
        elif pos.close <= pos.stop_loss:
            ev = self.bus.register(
                kind="stop_loss_halve",  # 6.6 顺位 3，参与批次仲裁（R6.5）
                subject=subject,
                accounting_day=accounting_day,
                payload={"close": pos.close, "stop_loss": pos.stop_loss},
                source=CALLER_ID,
            )
            self.start_countdown(subject, now, ev.event_id, ("PRICE.STOP_LOSS",))
            directives.append(
                MonitorDirective(
                    action=MonitorAction.UNCONDITIONAL_HALVE,
                    subject=subject,
                    reason=(
                        f"日收盘 {pos.close} 跌破预设止损位 {pos.stop_loss} → 无条件减半 + 48h 复评启动"
                        "（R12.6/F 6.1；防守类 6.6 顺位 3，不受冻结阻断，A5）"
                    ),
                    rule_ids=("PRICE.STOP_LOSS",),
                    defensive=True,
                    freeze_exempt=True,
                    trigger_event_id=ev.event_id,
                )
            )

        p = self.rules.get("PRICE.REREVIEW_LINE").params
        if pos.cost_basis is None:
            gaps.append(Gap("cost_basis", subject, ("PRICE.REREVIEW_LINE",), "成本价缺失，无法核对重审线（A2）"))
        else:
            line = pos.cost_basis * (1 - p["drawdown_from_cost_pct"] / 100)
            if pos.close <= line:
                ev = self.bus.register(
                    kind="re_review_line",  # 6.6 顺位 5，参与批次仲裁（R6.5）
                    subject=subject,
                    accounting_day=accounting_day,
                    payload={"close": pos.close, "line": line, "cost_basis": pos.cost_basis},
                    source=CALLER_ID,
                )
                directives.append(
                    MonitorDirective(
                        action=MonitorAction.FORCED_REREVIEW,
                        subject=subject,
                        reason=(
                            f"日收盘 {pos.close} 跌破成本价 -{p['drawdown_from_cost_pct']}% 重审线"
                            f"（{line:.4g}）→ 强制重审任务（R12.6/F 6.1）"
                        ),
                        rule_ids=("PRICE.REREVIEW_LINE",),
                        trigger_event_id=ev.event_id,
                    )
                )

        if pos.bull_target is None:
            gaps.append(Gap("bull_target", subject, ("PRICE.BULL_TARGET_CROSS",), "牛市目标价缺失，无法核对（A2）"))
        elif pos.close >= pos.bull_target:
            ev = self.bus.register(
                kind="bull_target_crossed",
                subject=subject,
                accounting_day=accounting_day,
                payload={"close": pos.close, "bull_target": pos.bull_target},
                source=CALLER_ID,
            )
            directives.append(
                MonitorDirective(
                    action=MonitorAction.STAGED_TAKE_PROFIT_EVAL,
                    subject=subject,
                    reason=(
                        f"日收盘 {pos.close} 上穿牛市目标价 {pos.bull_target} → 分批兑现评估意见"
                        "（R12.6/F 7.3〔校验 F-09〕）"
                    ),
                    rule_ids=("PRICE.BULL_TARGET_CROSS",),
                    trigger_event_id=ev.event_id,
                )
            )
        return directives, gaps

    # -------------------------------------------------- 颠覆名单联动（R12.7）

    def on_disruption_listed(self, subject: str, accounting_day: str) -> MonitorDirective | None:
        """标的被列入被颠覆名单（F 2.3-2）：持仓标的 → 立即强制复评事件（R12.7）。"""
        if subject not in self.holdings:
            self._log("disruption_nonholding", subject, "被列入颠覆名单但非持仓，不产联动复评（R12.7 范围）")
            return None
        ev = self.bus.register(
            kind="disruption_forced_reeval",
            subject=subject,
            accounting_day=accounting_day,
            payload={"source_rule": "DISRUPT.HOLDING_REEVAL"},
            source=CALLER_ID,
        )
        self._log("disruption_reeval", subject, "持仓标的被列入被颠覆名单 → 立即强制复评（R12.7）")
        return MonitorDirective(
            action=MonitorAction.FORCED_REEVAL,
            subject=subject,
            reason="持仓标的被列入被颠覆名单 → 立即触发强制复评（R12.7/F 2.3-2）",
            rule_ids=("DISRUPT.HOLDING_REEVAL",),
            trigger_event_id=ev.event_id,
        )


@dataclass(frozen=True)
class PositionPricing:
    """一只持仓的日收盘核对输入（R12.6）：阈值位缺失（None）按缺口处理（A2）。"""

    subject: str
    market: str  # 标的所在市场（收盘口径按其市场日历，R4.1/R12.6）
    close: float
    stop_loss: float | None = None  # 预设止损位（论点卡，附录 A）
    cost_basis: float | None = None  # 成本价（-20% 重审线基准）
    bull_target: float | None = None  # 三情景牛市目标价


__all__ = [
    "CALLER_ID",
    "DETECTOR_IDS_62",
    "DETECTOR_IDS_63",
    "DetectionReport",
    "FreezeAdvice",
    "Metrics62",
    "Metrics63",
    "MonitorAction",
    "MonitorDirective",
    "ObservationPeriod",
    "PositionPricing",
    "PositionSignalMonitor",
    "ReevalConclusion",
    "ReevalCountdown",
    "SignalHit",
    "detect_62",
    "detect_63",
    "frequency_registry",
]
