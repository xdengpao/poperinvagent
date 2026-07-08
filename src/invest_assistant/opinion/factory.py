"""最小意见构造器（任务 14.7，需求 R14.1/R14.2/R14.9；框架第十章 10.1/10.2、结尾声明 v2.0）。

**P1 范围**：防守类三类构造器——减仓 / 卖出 / 清仓（框架 10.1 中"卖出（清仓）"为同一意见类，
本模块按需求措辞区分卖出（R10.4 跌破 65）与清仓（R12.3/R12.4/R13.4）两种执行形态）。
全七类（买入/小仓试探买入/加仓/分批兑现/持有）在 P2 任务 16.1 扩展——见 :class:`OpinionType` 注释。

框架 10.2 必备字段原文（docs/AI时代股票投资指导框架-v2.0.md 第 10.2 节，以「｜」分隔）：
    标的与市场｜意见类型｜依据条文清单（本框架章节号）｜数据证据（每项数据附来源与日期）｜
    评分与分维明细｜三情景目标区间｜建议仓位区间与分批计划｜预设止损位与风险预算校验｜
    有效期（至下一财报或 90 天孰早）｜风险提示（该标的 6.2/6.3 信号现状）｜生成时间与意见编号。
    「缺任一字段的意见无效，系统不得输出。」
下游文档所称"十项"即前十段内容字段；末段"生成时间与意见编号"由本工厂盖章（发号器 + 时钟），
在模型层同为 required。全部字段缺一构造失败（R14.1，Pydantic 模型层断言）。

编号 ``#YYYY-MM-NNN`` 月内单调、同月并发唯一（R14.2）；
有效期 = min(下一财报日, 生成 + 90 自然日)，财报日历不可得退化为 90 天（R14.2 / FR-D-08）；
自然日经 calendar.Duration 解析（R4.3，业务时限禁止裸 timedelta）。
非投资建议声明为固定模板常量，构造后不可改、不可移除（R14.9 / CP-8，pydantic frozen）。
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ..calendar import CalendarService, Duration

_VALIDITY_90D = Duration.parse("90d")  # 意见有效期 90 天为自然日口径（R4.3/R14.2）

DISCLAIMER = (
    "本意见为框架规则运算结果，附依据条文与数据证据，仅用于教育与研究，不构成投资建议，"
    "不构成收益承诺，不保证正确；按框架操作仍可能亏损大部分本金。最终投资决策与执行由用户"
    "独立作出并复核签认，盈亏自负；必要时咨询持牌专业人士。（框架结尾声明 v2.0 / CP-8）"
)
"""非投资建议声明固定模板（R14.9）：组件不可移除，字段不可改写。"""

OPINION_ID_PATTERN = r"^#\d{4}-\d{2}-\d{3}$"


class OpinionType(StrEnum):
    """意见类型（框架 10.1 七类，P2 任务 16.1 全量）。

    框架七类：买入 / 小仓试探买入 / 加仓 / 减仓 / 卖出（清仓）/ 分批兑现 / 持有。
    其中"卖出（清仓）"按需求执行形态拆分为 SELL（R10.4 跌破 65）与 LIQUIDATE
    （R12.3/R12.4/R13.4 证伪全额清仓）两种，映射同一框架类型。
    """

    BUY = "买入"
    TRIAL_BUY = "小仓试探买入"
    ADD = "加仓"
    REDUCE = "减仓"
    SELL = "卖出"
    LIQUIDATE = "清仓"
    STAGED_TAKE_PROFIT = "分批兑现"
    HOLD = "持有"


#: 框架 10.1 七类 → 本枚举映射（完备性断言用；卖出（清仓）对应 SELL+LIQUIDATE）
FRAMEWORK_OPINION_TYPES: dict[str, tuple[OpinionType, ...]] = {
    "买入": (OpinionType.BUY,),
    "小仓试探买入": (OpinionType.TRIAL_BUY,),
    "加仓": (OpinionType.ADD,),
    "减仓": (OpinionType.REDUCE,),
    "卖出（清仓）": (OpinionType.SELL, OpinionType.LIQUIDATE),
    "分批兑现": (OpinionType.STAGED_TAKE_PROFIT,),
    "持有": (OpinionType.HOLD,),
}


class OpinionStatus(StrEnum):
    """意见状态（规格书 §3.3 opinion 表；db/migrations/001_schema.sql）。"""

    ACTIVE = "active"
    EXECUTED = "executed"
    EXPIRED = "expired"
    REJECTED = "rejected"
    OVERDUE = "overdue"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceItem(_Frozen):
    """数据证据条目（10.2 第 4 项）：每项数据附来源与日期。"""

    field_name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    source: str = Field(min_length=1)
    as_of: dt.date


class ScoreDetail(_Frozen):
    """评分与分维明细（10.2 第 5 项）。"""

    total: float
    dimensions: dict[str, float] = Field(min_length=1)


class ScenarioTargets(_Frozen):
    """三情景目标区间（10.2 第 6 项）。"""

    bear: str = Field(min_length=1)
    base: str = Field(min_length=1)
    bull: str = Field(min_length=1)


class PositionPlan(_Frozen):
    """建议仓位区间与分批计划（10.2 第 7 项）。"""

    target_low_pct: float = Field(ge=0)
    target_high_pct: float = Field(ge=0)
    batch_plan: str = Field(min_length=1)

    @field_validator("target_high_pct")
    @classmethod
    def _band(cls, v: float, info: ValidationInfo) -> float:
        low = info.data.get("target_low_pct")
        if low is not None and v < low:
            raise ValueError("仓位区间上沿不得低于下沿")
        return v


class StopLossCheck(_Frozen):
    """预设止损位与风险预算校验（10.2 第 8 项）。"""

    stop_level: str = Field(min_length=1)
    risk_budget_check: str = Field(min_length=1)


class Opinion(_Frozen):
    """意见对象（R14.1）：框架 10.2 必备字段全 required，缺一构造失败；frozen 不可改。"""

    symbol: str = Field(min_length=1)  # 10.2-1 标的与市场
    market: Literal["CN", "US"]  # 10.2-1 标的与市场
    opinion_type: OpinionType  # 10.2-2 意见类型
    basis_clauses: tuple[str, ...] = Field(min_length=1)  # 10.2-3 依据条文清单（框架章节号）
    evidence: tuple[EvidenceItem, ...] = Field(min_length=1)  # 10.2-4 数据证据
    score: ScoreDetail  # 10.2-5 评分与分维明细
    scenarios: ScenarioTargets  # 10.2-6 三情景目标区间
    position_plan: PositionPlan  # 10.2-7 建议仓位区间与分批计划
    stop_loss: StopLossCheck  # 10.2-8 预设止损位与风险预算校验
    valid_until: dt.date  # 10.2-9 有效期（至下一财报或 90 天孰早）
    risk_alert: str = Field(min_length=1)  # 10.2-10 风险提示（6.2/6.3 信号现状）
    generated_at: dt.datetime  # 10.2-末段 生成时间
    opinion_id: str = Field(pattern=OPINION_ID_PATTERN)  # 10.2-末段 意见编号 #YYYY-MM-NNN
    disclaimer: str = DISCLAIMER  # R14.9 恒附声明（固定模板，不可改）

    @field_validator("basis_clauses")
    @classmethod
    def _clauses_nonempty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if any(not c.strip() for c in v):
            raise ValueError("依据条文清单不得含空条目（10.2 第 3 项）")
        return v

    @field_validator("disclaimer")
    @classmethod
    def _disclaimer_locked(cls, v: str) -> str:
        if v != DISCLAIMER:
            raise ValueError("非投资建议声明为固定模板，不可修改或移除（R14.9/CP-8）")
        return v


class OpinionNumberIssuer:
    """意见编号发号器（R14.2）：``#YYYY-MM-NNN`` 月内单调，同月并发唯一（锁保护）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def issue(self, on: dt.date) -> str:
        month_key = f"{on.year:04d}-{on.month:02d}"
        with self._lock:
            n = self._counters.get(month_key, 0) + 1
            if n > 999:
                raise RuntimeError(f"{month_key} 月内编号超出 NNN 容量（R14.2 编号格式 #YYYY-MM-NNN）")
            self._counters[month_key] = n
        return f"#{month_key}-{n:03d}"


class OpinionFactory:
    """意见工厂（任务 16.1）：框架 10.1 全七类构造器；十项字段模型层 required。

    七类触发语义（10.1）在构造器中以断言落地：买入需评分 ≥ 判定线（调用方保证
    三层漏斗与风控已过，此处校验触发上下文一致性）；小仓试探仓位 ≤ 单票初始上限
    一半；清仓目标仓位=0。判定与放行由规则引擎/风控/S8 守卫承担（A1），本工厂
    只负责合规构造与盖章。
    """

    def __init__(
        self,
        issuer: OpinionNumberIssuer | None = None,
        calendar: CalendarService | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._issuer = issuer or OpinionNumberIssuer()
        self._calendar = calendar or CalendarService()
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    def _valid_until(self, market: str, issued_on: dt.date, next_earnings: dt.date | None) -> dt.date:
        """有效期 = min(下一财报日, 生成+90 自然日)；财报日历不可得（None）退化 90 天（R14.2）。"""
        cap = self._calendar.add(market, issued_on, _VALIDITY_90D)
        assert isinstance(cap, dt.date)
        if next_earnings is None:
            return cap
        return min(next_earnings, cap)

    def _build(
        self,
        opinion_type: OpinionType,
        *,
        symbol: str,
        market: Literal["CN", "US"],
        basis_clauses: Sequence[str],
        evidence: Sequence[EvidenceItem],
        score: ScoreDetail,
        scenarios: ScenarioTargets,
        position_plan: PositionPlan,
        stop_loss: StopLossCheck,
        risk_alert: str,
        next_earnings_date: dt.date | None = None,
    ) -> Opinion:
        generated_at = self._now()
        issued_on = generated_at.date()
        return Opinion(
            symbol=symbol,
            market=market,
            opinion_type=opinion_type,
            basis_clauses=tuple(basis_clauses),
            evidence=tuple(evidence),
            score=score,
            scenarios=scenarios,
            position_plan=position_plan,
            stop_loss=stop_loss,
            valid_until=self._valid_until(market, issued_on, next_earnings_date),
            risk_alert=risk_alert,
            generated_at=generated_at,
            opinion_id=self._issuer.issue(issued_on),
        )

    # —— 扩权类三类（10.1；放行由风控/S8 守卫把关，A1）——

    def buy(self, *, score_line: float | None = None, **kwargs) -> Opinion:
        """买入意见（10.1：评分 ≥75 + 三情景达标 + 风控可容纳 + 市况非狂热 + 分批建仓）。

        触发上下文一致性校验：若给出 score_line，则意见携带的总分须 ≥ 该判定线。
        """
        score: ScoreDetail = kwargs["score"]
        if score_line is not None and score.total < score_line:
            raise ValueError(f"买入意见总分 {score.total} < 判定线 {score_line}（10.1/3.4）")
        return self._build(OpinionType.BUY, **kwargs)

    def trial_buy(self, *, initial_cap_pct: float | None = None, **kwargs) -> Opinion:
        """小仓试探买入（10.1：评分 65-74；仓位 ≤ 单票初始上限的一半）。"""
        plan: PositionPlan = kwargs["position_plan"]
        if initial_cap_pct is not None and plan.target_high_pct > initial_cap_pct / 2 + 1e-9:
            raise ValueError(
                f"小仓试探仓位 {plan.target_high_pct}% > 单票初始上限 {initial_cap_pct}% 的一半（10.1）")
        return self._build(OpinionType.TRIAL_BUY, **kwargs)

    def add(self, **kwargs) -> Opinion:
        """加仓意见（10.1/7.3：加仓条件成立 + 冷静期与次数预算满足——由风控前置校验）。"""
        return self._build(OpinionType.ADD, **kwargs)

    # —— 兑现/持有 ——

    def staged_take_profit(self, **kwargs) -> Opinion:
        """分批兑现意见（10.1/7.3：分批兑现条件成立；R11.3 狂热档、R12.5 6.3≥3）。"""
        return self._build(OpinionType.STAGED_TAKE_PROFIT, **kwargs)

    def hold(self, **kwargs) -> Opinion:
        """持有意见（10.1：持仓未触发任何动作条件，月度确认一次；R6.8 持有确认）。"""
        return self._build(OpinionType.HOLD, **kwargs)

    # —— 防守类三类 ——

    def reduce(self, **kwargs) -> Opinion:
        """减仓意见（框架 10.1/7.3 减仓行；R10.4 65-74 跌档、R12.5、R13.4 至少减半）。"""
        return self._build(OpinionType.REDUCE, **kwargs)

    def sell(self, **kwargs) -> Opinion:
        """卖出意见（框架 10.1 卖出（清仓）；R10.4 跌破 65，出处=框架 3.4）。"""
        return self._build(OpinionType.SELL, **kwargs)

    def liquidate(self, **kwargs) -> Opinion:
        """清仓意见（框架 10.1 卖出（清仓）之全额形态；R12.3/R12.4/R13.4 证伪清仓）。

        清仓语义断言：目标仓位区间必须为 0。
        """
        plan: PositionPlan = kwargs["position_plan"]
        if plan.target_high_pct != 0:
            raise ValueError("清仓意见的目标仓位必须为 0（框架 7.3 清仓行）")
        return self._build(OpinionType.LIQUIDATE, **kwargs)
