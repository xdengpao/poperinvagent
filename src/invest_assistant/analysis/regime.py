"""市况分级器（任务 19，需求 R11；框架 v2.0 §6.4；规格书 §6.4〔校验 C-17〕）。

输入：信号池 10 项检测结果（``bool | None``，None=数据缺口——缺口信号不计
命中并产 Gap，A2）+ 云厂商 capex 指引下修标志 + 指数自高点回撤值；
输出：``RegimeAssessment``（market_regime 行的内存形态）——级别（常态/过热/
狂热/冰点）、命中信号清单、档位动作清单（按档位从规则条目取，R11.3）、
生效区间开口（``effective_to=None``，下次级别变更时闭合）。

级别变更产生 ``RegimeChangeEvent`` 供触发器总线登记（R11.2，design §3.4：
级别变更事件驱动风控参数档切换与任务生成——狂热档对全部持仓生成分批
兑现评估任务〔校验 F-09〕）。

未分类重大事件（R11.4）：``register_unclassified_event`` 只产 O-04 人工
裁决通道对象，不产生任何自动动作（设计方案 O-04：未列举事件不触发自动
动作，人工裁决只可调严）。

业务时限一律用账务日 ISO 字符串（R4.2 拼合口径），不引入裸 timedelta（R4.3）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.types import Gap
from ..data.quality import GapLedger
from ..rules.engine import RuleEntry, RuleRepository

#: 信号池 10 项规则条目 ID（R11.1；规格书 §6.4 ①-⑩ 顺序）
SIGNAL_IDS: tuple[str, ...] = (
    "REGIME.SIG.INDEX_PS_PEG",
    "REGIME.SIG.INDEX_TAM_IMPLIED",
    "REGIME.SIG.INDEX_GAIN_VS_REVISION",
    "REGIME.SIG.RESEARCH_CONSENSUS_EXTREME",
    "REGIME.SIG.IPO_PLACEMENT_SURGE",
    "REGIME.SIG.REAL_RATE_SPIKE",
    "REGIME.SIG.CREDIT_SPREAD_WIDENING",
    "REGIME.SIG.CB_TIGHTENING",
    "REGIME.SIG.BREADTH_DETERIORATION",
    "REGIME.SIG.MARGIN_DEBT_PCTILE",
)

CAPEX_GATE_ID = "REGIME.CAPEX.DOWNGRADE_GATE"
CLASSIFY_ID = "REGIME.LEVEL.CLASSIFY"
GAP_RULE_ID = "REGIME.SIG.GAP_NO_HIT"
UNCLASSIFIED_ID = "REGIME.EVENT.UNCLASSIFIED"


class RegimeLevel(StrEnum):
    """四级市况（F 6.4 分级表，R11.2）。"""

    NORMAL = "normal"
    OVERHEAT = "overheat"
    MANIA = "mania"
    FREEZE = "freeze"

#: 级别 → 动作规则条目（R11.3：动作按档位从规则条目取，不硬编码）
_ACTION_RULE_BY_LEVEL: dict[RegimeLevel, str] = {
    RegimeLevel.NORMAL: "REGIME.ACTION.NORMAL",
    RegimeLevel.OVERHEAT: "REGIME.ACTION.OVERHEAT",
    RegimeLevel.MANIA: "REGIME.ACTION.MANIA",
    RegimeLevel.FREEZE: "REGIME.ACTION.FREEZE",
}


@dataclass(frozen=True)
class SignalReading:
    """单信号检测结果：``hit=None`` 表示数据缺口（A2，不计命中并产 Gap）。"""

    signal_id: str
    hit: bool | None
    evidence_id: str = ""


@dataclass
class RegimeAssessment:
    """市况判定产物（market_regime 行的内存形态，R11.2）。

    生效区间开口：``effective_from`` 为本次判定账务日，``effective_to``
    恒为 None（开口），由下一次级别变更闭合（持久层职责）。
    """

    level: RegimeLevel
    signals_hit: list[str]
    capex_downgrade: bool
    index_drawdown: float | None
    actions: dict  # 档位动作参数（来自 REGIME.ACTION.* 规则条目，R11.3）
    action_rule_id: str
    effective_from: str  # 账务日 ISO 字符串（R4.2）
    effective_to: None = None  # 开口区间
    alerts: list[str] = field(default_factory=list)  # capex 整链警报等
    tasks: list[dict] = field(default_factory=list)  # 狂热档全持仓分批兑现评估任务
    gaps: list[Gap] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)  # A4 三元组
    snapshot_id: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return len(self.signals_hit)


@dataclass(frozen=True)
class RegimeChangeEvent:
    """级别变更事件（R11.2）：供触发器总线登记（kind=regime_change）。"""

    previous: RegimeLevel | None
    current: RegimeLevel
    accounting_day: str
    signals_hit: tuple[str, ...]
    kind: str = "regime_change"
    subject: str = "market"

    def payload(self) -> dict:
        """TriggerBus.register 的 payload 形态（R6.4：持久化优先于消费）。"""
        return {
            "previous": None if self.previous is None else str(self.previous),
            "current": str(self.current),
            "signals_hit": list(self.signals_hit),
        }


@dataclass(frozen=True)
class UnclassifiedEventAlert:
    """未分类重大事件警报 → O-04 人工裁决通道对象（R11.4）。

    ``auto_actions`` 恒为空元组：未列举事件不得触发任何自动动作，
    仅推送警报并请用户按 6.6 顺位人工裁决（只可调严，不可放宽）。
    """

    description: str
    accounting_day: str
    channel: str = "O-04"
    manual_adjust_direction: str = "tighten_only"
    auto_actions: tuple[()] = ()
    rule_id: str = UNCLASSIFIED_ID


class RegimeGrader:
    """市况分级引擎（R11）：规则条目是数据，本引擎只解释（A1）。"""

    def __init__(self, rules: RuleRepository, gap_ledger: GapLedger | None = None):
        self._rules = rules
        self._gaps = gap_ledger or GapLedger()
        self.unclassified_log: list[UnclassifiedEventAlert] = []
        # 装载即校验注册表完整（无规则不判定，A1）
        for sid in (*SIGNAL_IDS, CAPEX_GATE_ID, CLASSIFY_ID, GAP_RULE_ID, UNCLASSIFIED_ID):
            rules.get(sid)
        for action_id in _ACTION_RULE_BY_LEVEL.values():
            rules.get(action_id)

    # —— 注册表（R11.1：每项配数据源适配器 + 阈值 + 检测频率）——
    def signal_registry(self) -> list[RuleEntry]:
        """信号池 10 项检测器注册表（design §3.4）。"""
        return [self._rules.get(sid) for sid in SIGNAL_IDS]

    def capex_gate(self) -> RuleEntry:
        """capex 下修主闸门（单列最高权重，不计入 10 项计数）。"""
        return self._rules.get(CAPEX_GATE_ID)

    # —— 级别判定（R11.2）——
    def assess(
        self,
        readings: Sequence[SignalReading],
        capex_downgrade: bool | None,
        index_drawdown: float | None,
        accounting_day: str,
        snapshot_id: str,
        previous_level: RegimeLevel | None = None,
        holdings: Sequence[str] = (),
    ) -> tuple[RegimeAssessment, RegimeChangeEvent | None]:
        """判定市况级别并产出档位动作与（若级别变更）变更事件。

        ``capex_downgrade``/``index_drawdown`` 为 None 时同样按 A2 处理：
        登记 Gap，且缺回撤数据不判冰点（冰点是放宽档，缺数据不放宽）。
        """
        classify = self._rules.get(CLASSIFY_ID)
        gap_rule = self._rules.get(GAP_RULE_ID)
        p = classify.params

        seen = {r.signal_id for r in readings}
        unknown = seen - set(SIGNAL_IDS)
        if unknown:
            raise KeyError(f"信号池外的检测结果：{sorted(unknown)}（无规则不判定，A1）")

        gaps: list[Gap] = []
        hits: list[str] = []
        evidence_ids: list[str] = []
        for r in readings:
            if r.hit is None:  # 缺口信号不计命中并产 Gap（A2）
                assert gap_rule.params["on_gap"] == "not_hit" and gap_rule.params["register_gap"]
                gaps.append(
                    self._gaps.register(
                        Gap(r.signal_id, "market", degradation="市况信号数据缺口：不计命中（A2/R11.2）")
                    )
                )
            elif r.hit:
                hits.append(r.signal_id)
                if r.evidence_id:
                    evidence_ids.append(r.evidence_id)
        for sid in SIGNAL_IDS:
            if sid not in seen:  # 未接入的信号 = 缺口，同样零静默（A2）
                gaps.append(
                    self._gaps.register(
                        Gap(sid, "market", degradation="市况信号未接入：不计命中（A2/R11.2）")
                    )
                )

        alerts: list[str] = []
        capex = bool(capex_downgrade)
        gate = self.capex_gate()
        if capex_downgrade is None:
            gaps.append(
                self._gaps.register(
                    Gap(CAPEX_GATE_ID, "market", degradation="capex 指引信号缺口：不计命中（A2）")
                )
            )
        elif capex and gate.params["full_chain_alert"]:
            alerts.append("云厂商 capex 指引下修：整链警报（F 6.4 单列最高权重）")
        if index_drawdown is None:
            gaps.append(
                self._gaps.register(
                    Gap("index_drawdown", "market", degradation="指数回撤缺口：不判冰点（A2，保守不放宽）")
                )
            )

        n = len(hits)
        mania_by_capex = capex and n >= int(gate.params["mania_with_additional_hits"])
        if (
            index_drawdown is not None
            and index_drawdown <= float(p["freeze_drawdown_le"])
            and n <= int(p["freeze_hits_max"])
        ):
            level = RegimeLevel.FREEZE  # 冰点：指数 -30% 以上且命中 ≤1（F 6.4）
        elif n >= int(p["mania_hits_min"]) or mania_by_capex:
            level = RegimeLevel.MANIA  # 狂热：≥5 项，或 capex 下修+任意 2 项
        elif n >= int(p["overheat_hits_min"]):
            level = RegimeLevel.OVERHEAT  # 过热：2-4 项
        else:
            level = RegimeLevel.NORMAL  # 常态：≤1 项

        action_rule = self._rules.get(_ACTION_RULE_BY_LEVEL[level])
        actions = dict(action_rule.params)
        tasks: list[dict] = []
        if level is RegimeLevel.MANIA and actions.get("staged_exit_review_all_holdings"):
            # 狂热档：对全部持仓生成分批兑现评估任务（〔校验 F-09〕/R11.3）
            tasks = [
                {"task": "staged_exit_review", "subject": h, "source_rule": action_rule.rule_id}
                for h in holdings
            ]

        assessment = RegimeAssessment(
            level=level,
            signals_hit=hits,
            capex_downgrade=capex,
            index_drawdown=index_drawdown,
            actions=actions,
            action_rule_id=action_rule.rule_id,
            effective_from=accounting_day,
            alerts=alerts,
            tasks=tasks,
            gaps=gaps,
            rule_ids=[CLASSIFY_ID, GAP_RULE_ID, CAPEX_GATE_ID, action_rule.rule_id],
            snapshot_id=snapshot_id,
            evidence_ids=evidence_ids,
        )
        event: RegimeChangeEvent | None = None
        if previous_level != level:  # 含首次判定（previous=None → 变更事件驱动档位落位）
            event = RegimeChangeEvent(
                previous=previous_level,
                current=level,
                accounting_day=accounting_day,
                signals_hit=tuple(hits),
            )
        return assessment, event

    # —— 未分类重大事件（R11.4）——
    def register_unclassified_event(self, description: str, accounting_day: str) -> UnclassifiedEventAlert:
        """信号池未覆盖的重大事件 → 警报 + O-04 人工裁决通道，零自动动作。"""
        rule = self._rules.get(UNCLASSIFIED_ID)
        assert rule.params["auto_actions_forbidden"], "R11.4: 未分类事件不得自动动作"
        alert = UnclassifiedEventAlert(
            description=description,
            accounting_day=accounting_day,
            channel=str(rule.params["channel"]),
            manual_adjust_direction=str(rule.params["manual_adjust_direction"]),
        )
        assert alert.auto_actions == (), "R11.4: 人工裁决通道对象不携带自动动作"
        self.unclassified_log.append(alert)
        return alert
