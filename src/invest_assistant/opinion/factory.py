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
    """意见类型（框架 10.1 七类；P1 仅防守三类，其余四类由 P2 任务 16.1 扩展：

    买入 / 小仓试探买入 / 加仓 / 分批兑现 / 持有。
    """

    REDUCE = "减仓"
    SELL = "卖出"
    LIQUIDATE = "清仓"


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
    """防守类最小意见工厂（任务 14.7）。P2 任务 16.1 在此基础上扩至全七类。"""

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
