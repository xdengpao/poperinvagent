"""论点卡与证伪状态机（任务 14 核心，需求 R13；框架 7.2 可证伪假设法 / 附录 A 论点卡模板 v2.0）。

- 论点卡（R13.1）：一句话逻辑、3-5 条含数字与时间的假设（每条含增强判据与转弱判据，
  其中 1-2 条标记为核心假设——构造时断言）、三情景、证伪条件与动作、卖出计划、
  关联意见编号、季度标记历史（✅/⚠️/❌）与连续 ⚠️ 计数。
- 状态机（design §3.4）：draft → signed → 季度标记循环 → invalidated。
  签认前不得放行买入/加仓类（R13.2，见 :attr:`ThesisCard.expansion_allowed`，S8 守卫消费）；
  用户修改假设仅可向更保守方向——由 tighten-only 校验钩子把关（R13.2）。
- 失效判定（R13.4 / 框架 7.2 统一版）：核心假设单季 ❌ → 无条件清仓（6.6 顺位第 2 级
  证伪清仓，防守类不受任何冻结阻断，铁律 A5）；同一假设连续两季 ⚠️ → 至少减半；
  第三季仍 ⚠️ → 清仓。
- 换故事续命防御（R13.5）：论点失效后 :func:`new_thesis` 必须在原卡失效动作执行完成后、
  且新卡携带重走 S3 的判定引用时才放行，否则拒绝。
- 观察期语义（R12.3/R12.4）不在本模块——由持仓信号监测器实现（任务 19）。

本模块只产出动作指令（:class:`ActionDirective`），意见对象由 opinion.factory 构造（R14.1）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------- 异常


class ThesisError(Exception):
    """论点卡域错误基类。"""


class ThesisStateError(ThesisError):
    """状态机非法迁移（R13.2/R13.3）。"""


class TightenOnlyViolation(ThesisError):
    """用户修改假设不满足只可更保守方向（R13.2）。"""


class UnsignedMarkError(ThesisError):
    """季度标记未经用户签认（R13.3：每假设标记须签认）。"""


class SupersedeRejected(ThesisError):
    """换故事续命被拒（R13.5）。"""


# ---------------------------------------------------------------- 值对象


class Mark(StrEnum):
    """季度标记（框架 7.2）：✅ 按数据达成；⚠️ 偏离未达证伪线；❌ 核心定量假设硬性落空。"""

    OK = "✅"
    WARN = "⚠️"
    FAIL = "❌"


class ThesisState(StrEnum):
    DRAFT = "draft"  # 草稿：系统起草，等待用户签认（R13.1）
    SIGNED = "signed"  # 已签认：进入季度标记循环（R13.3）
    INVALIDATED = "invalidated"  # 已失效：待执行/已执行失效动作（R13.4/R13.5）


class ActionKind(StrEnum):
    LIQUIDATE = "清仓"
    HALVE_AT_LEAST = "至少减半"


@dataclass(frozen=True)
class ActionDirective:
    """证伪状态机产出的动作指令（R13.4）。

    - ``priority_66``：框架 6.6 触发器顺位（证伪清仓 = 第 2 级）；
    - ``freeze_exempt``：防守类不受任何冻结阻断（铁律 A5）；
    - ``unconditional``：核心 ❌ 的清仓"无条件、不看价格"（框架 7.2）。
    """

    kind: ActionKind
    reason: str
    priority_66: int | None = None
    defensive: bool = True
    freeze_exempt: bool = True
    unconditional: bool = False


@dataclass(frozen=True)
class Hypothesis:
    """假设条目（R13.1）：含数字与时间，配增强/转弱判据，可标记核心。"""

    hypothesis_id: str
    statement: str  # 假设陈述
    metric_target: str  # 数字判据（必须含数字，框架 7.2"带数字与时间"）
    deadline: dt.date  # 时间判据
    strengthen_criterion: str  # 增强判据：跟踪指标连续超预期（框架 7.2）
    weaken_criterion: str  # 转弱判据：低于预期但未达证伪线（框架 7.2）
    is_core: bool = False  # 核心假设：关于收入/盈利拐点的定量假设（框架 7.2）

    def __post_init__(self) -> None:
        if not self.hypothesis_id or not self.statement:
            raise ThesisError("假设缺 ID 或陈述（R13.1）")
        if not any(ch.isdigit() for ch in self.metric_target):
            raise ThesisError(f"假设 {self.hypothesis_id} 的判据不含数字——框架 7.2 要求带数字与时间")
        if not self.strengthen_criterion or not self.weaken_criterion:
            raise ThesisError(f"假设 {self.hypothesis_id} 缺增强/转弱判据（R13.1）")


@dataclass(frozen=True)
class ScenarioTriple:
    """三情景目标（R13.1；熊市 ≥-30% / 基准 ≥+100% 的数值校验在 S4 三情景计算器）。"""

    bear: str
    base: str
    bull: str

    def __post_init__(self) -> None:
        if not (self.bear and self.base and self.bull):
            raise ThesisError("三情景目标不完整（R13.1）")


@dataclass(frozen=True)
class QuarterMarks:
    """一次已签认的季度标记（R13.3）。"""

    period: str  # 如 "2026Q3"
    marks: Mapping[str, Mark]  # hypothesis_id → 标记
    signoff_id: str


TightenCheck = Callable[[Hypothesis, Hypothesis], bool]
"""tighten-only 校验钩子：``(原假设, 新假设) -> 是否更保守``（R13.2）。

保守语义域相关（判定线只升/证伪线只紧等），由调用方（签认队列，任务 14.2）注入；
钩子返回 False 的修改一律拒绝。
"""


def _validate_hypotheses(hypotheses: tuple[Hypothesis, ...]) -> None:
    """构造/修改时断言：3-5 条假设，核心 1-2 条，ID 唯一（R13.1）。"""
    if not 3 <= len(hypotheses) <= 5:
        raise ThesisError(f"假设须 3-5 条，当前 {len(hypotheses)} 条（R13.1）")
    ids = [h.hypothesis_id for h in hypotheses]
    if len(set(ids)) != len(ids):
        raise ThesisError("假设 ID 重复")
    core = sum(1 for h in hypotheses if h.is_core)
    if not 1 <= core <= 2:
        raise ThesisError(f"核心假设须 1-2 条，当前 {core} 条（R13.1 / 框架 7.2）")


# ---------------------------------------------------------------- 论点卡


@dataclass
class ThesisCard:
    """论点卡（R13.1；存储对应 thesis_card ★ 表，append-only 由存证层强制）。"""

    card_id: str
    symbol: str
    one_liner: str  # 一句话逻辑
    hypotheses: tuple[Hypothesis, ...]
    scenarios: ScenarioTriple
    falsification: str  # 证伪条件与动作
    sell_plan: str  # 卖出计划
    linked_opinion_id: str  # 关联意见编号（附录 A v2.0 新增栏）
    state: ThesisState = ThesisState.DRAFT
    signoff_id: str = ""  # 签认记录引用（R13.2）
    mark_history: list[QuarterMarks] = field(default_factory=list)  # 季度标记历史（R13.3）
    consecutive_warn: dict[str, int] = field(default_factory=dict)  # 逐假设连续 ⚠️ 计数（R13.3）
    pending_exit: ActionDirective | None = None  # 失效动作（R13.4）
    exit_executed: bool = False  # 失效动作是否已执行（R13.5 supersede 前置）
    supersedes: str = ""  # 被替换的原卡 ID（R13.5）
    s3_verdict_id: str = ""  # 新论点重走 S3 的判定引用（R13.5）

    def __post_init__(self) -> None:
        if not (self.card_id and self.symbol and self.one_liner):
            raise ThesisError("论点卡缺标识或一句话逻辑（R13.1）")
        if not (self.falsification and self.sell_plan and self.linked_opinion_id):
            raise ThesisError("论点卡缺证伪条件与动作/卖出计划/关联意见编号（R13.1）")
        _validate_hypotheses(self.hypotheses)

    # -------------------------------------------------- 签认与修改

    @property
    def expansion_allowed(self) -> bool:
        """签认前不得放行买入/加仓类（R13.2）——S7/S8 守卫读取此门。"""
        return self.state is ThesisState.SIGNED

    def sign(self, signoff_id: str) -> None:
        """用户签认（R13.2）：仅草稿态可签认。"""
        if self.state is not ThesisState.DRAFT:
            raise ThesisStateError(f"仅草稿态可签认，当前 {self.state}")
        if not signoff_id:
            raise UnsignedMarkError("签认必须携带签认记录 ID（R17.1 留痕）")
        self.state = ThesisState.SIGNED
        self.signoff_id = signoff_id

    def revise_hypotheses(self, replacements: Iterable[Hypothesis], tighten_check: TightenCheck) -> None:
        """用户修改假设——仅可更保守（R13.2，tighten-only 钩子把关）。

        仅草稿态可修改；不得增删/替换条目（换逻辑须走 :func:`new_thesis` 重走全漏斗，R13.5）。
        """
        if self.state is not ThesisState.DRAFT:
            raise ThesisStateError("仅草稿态可修改假设；签认后的新逻辑须走 new_thesis（R13.5）")
        new_by_id = {h.hypothesis_id: h for h in replacements}
        if set(new_by_id) != {h.hypothesis_id for h in self.hypotheses}:
            raise TightenOnlyViolation("不得增删或替换假设条目——新逻辑须重走 S3 起全流程（R13.5）")
        for old in self.hypotheses:
            new = new_by_id[old.hypothesis_id]
            if new != old and not tighten_check(old, new):
                raise TightenOnlyViolation(f"假设 {old.hypothesis_id} 的修改不满足只可更保守方向（R13.2）")
        candidate = tuple(new_by_id[h.hypothesis_id] for h in self.hypotheses)
        _validate_hypotheses(candidate)
        self.hypotheses = candidate

    # -------------------------------------------------- 季度标记与动作判定

    def record_quarter_marks(
        self, period: str, marks: Mapping[str, Mark], signoff_id: str
    ) -> ActionDirective | None:
        """登记一季已签认的逐假设标记，返回动作指令（R13.3/R13.4）。

        - 标记须经用户签认（signoff_id 必填，R13.3）；
        - 标记须覆盖全部假设（逐假设 ✅/⚠️/❌）；
        - 非核心假设的 ❌ 按 ⚠️ 保守计入连续计数（框架 7.2 的 ❌ 仅定义于核心定量假设；
          保守方向处理并留痕于此注释）。
        """
        if self.state is not ThesisState.SIGNED:
            raise ThesisStateError(f"季度标记仅对已签认论点卡进行，当前 {self.state}（R13.3）")
        if not signoff_id:
            raise UnsignedMarkError(f"{period} 标记未签认——每假设标记须经用户签认（R13.3）")
        if not period:
            raise ThesisError("缺期次标识")
        if any(q.period == period for q in self.mark_history):
            raise ThesisError(f"期次 {period} 已登记标记（thesis_card 为 append-only，R18.1）")
        expected = {h.hypothesis_id for h in self.hypotheses}
        if set(marks) != expected:
            raise ThesisError(f"标记须覆盖全部假设：缺 {sorted(expected - set(marks))}（R13.3）")

        self.mark_history.append(QuarterMarks(period=period, marks=dict(marks), signoff_id=signoff_id))
        for hid in expected:
            if marks[hid] in (Mark.WARN, Mark.FAIL):
                self.consecutive_warn[hid] = self.consecutive_warn.get(hid, 0) + 1
            else:
                self.consecutive_warn[hid] = 0
        return self._decide_action(period, marks)

    def _decide_action(self, period: str, marks: Mapping[str, Mark]) -> ActionDirective | None:
        """动作判定（R13.4）：核心 ❌ > 三季 ⚠️ 清仓 > 两季 ⚠️ 减半。"""
        core_fail = [h.hypothesis_id for h in self.hypotheses if h.is_core and marks[h.hypothesis_id] is Mark.FAIL]
        if core_fail:
            directive = ActionDirective(
                kind=ActionKind.LIQUIDATE,
                reason=(
                    f"{period} 核心假设 {'、'.join(core_fail)} 单季 ❌ → 无条件清仓，不看价格"
                    "（R13.4/框架 7.2；6.6 顺位第 2 级证伪清仓；防守类不受冻结阻断，A5）"
                ),
                priority_66=2,
                defensive=True,
                freeze_exempt=True,
                unconditional=True,
            )
            return self._invalidate(directive)
        warn3 = [hid for hid, n in self.consecutive_warn.items() if n >= 3]
        if warn3:
            directive = ActionDirective(
                kind=ActionKind.LIQUIDATE,
                reason=(
                    f"{period} 假设 {'、'.join(sorted(warn3))} 第三季仍 ⚠️ → 清仓"
                    "（R13.4/框架 7.2；6.6 顺位第 2 级证伪清仓；防守类不受冻结阻断，A5）"
                ),
                priority_66=2,
                defensive=True,
                freeze_exempt=True,
            )
            return self._invalidate(directive)
        warn2 = [hid for hid, n in self.consecutive_warn.items() if n == 2]
        if warn2:
            return ActionDirective(
                kind=ActionKind.HALVE_AT_LEAST,
                reason=f"{period} 假设 {'、'.join(sorted(warn2))} 连续两季 ⚠️ → 至少减半（R13.4/框架 7.2）",
                defensive=True,
                freeze_exempt=True,
            )
        return None

    def _invalidate(self, directive: ActionDirective) -> ActionDirective:
        self.state = ThesisState.INVALIDATED
        self.pending_exit = directive
        self.exit_executed = False
        return directive

    def confirm_exit_executed(self, receipt_ref: str) -> None:
        """失效动作执行完成回执（S9 成交回执引用）——supersede 的前置（R13.5）。"""
        if self.state is not ThesisState.INVALIDATED or self.pending_exit is None:
            raise ThesisStateError("无待执行的失效动作")
        if not receipt_ref:
            raise ThesisError("失效动作回执引用必填（R6.3 回执缺失将冻结标的）")
        self.exit_executed = True


# ---------------------------------------------------------------- 换故事续命防御


def new_thesis(old: ThesisCard, draft: ThesisCard, *, s3_verdict_id: str) -> ThesisCard:
    """论点失效后的新论点入口（R13.5）：拒绝在原持仓上续命。

    放行条件（全部满足，否则 :class:`SupersedeRejected`）：
    ①原卡已失效且失效动作已执行完成（先执行原失效动作）；
    ②新卡携带重走 S3 的判定引用（新论点须重走 S3 起全流程）；
    ③新卡为草稿态（仍须走签认，R13.2）且与原卡同标的。
    """
    if old.state is not ThesisState.INVALIDATED:
        raise SupersedeRejected(f"原论点卡 {old.card_id} 尚未失效——不允许换故事续命（R13.5）")
    if old.pending_exit is None or not old.exit_executed:
        raise SupersedeRejected(f"原卡 {old.card_id} 的失效动作未执行完成——新论点创建被拒（R13.5）")
    if not s3_verdict_id:
        raise SupersedeRejected("新论点必须重走 S3 全流程并携带判定引用（R13.5）")
    if draft.symbol != old.symbol:
        raise SupersedeRejected("supersede 仅适用于同一标的的新逻辑（R13.5）")
    if draft.state is not ThesisState.DRAFT:
        raise SupersedeRejected("新论点卡必须为草稿态并重走签认（R13.2）")
    draft.supersedes = old.card_id
    draft.s3_verdict_id = s3_verdict_id
    return draft
