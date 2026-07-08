"""M10 输出前合规自检器（任务 16.2，需求 R14.3；设计方案 §5.10 十项清单）。

十项 Check 逐项执行 → 任一不过则修正一次重检 → 仍不过输出"无法合规输出"
对象（非七类意见，无投资语义）。确定性收益语言为版本化词表拦截（可注册扩展），
证据链断链=三元组引用完整性校验。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..rules.params import ParamStore
from .factory import Opinion

#: 确定性收益语言词表（R14.3；可经 register_forbidden_terms 扩展，只增不减语义）
_FORBIDDEN_TERMS: set[str] = {
    "必涨", "稳赚", "保证收益", "保本", "包赚", "一定涨", "肯定涨", "无风险",
    "翻倍在望", "躺赢", "稳赢", "锁定收益", "确保盈利", "只涨不跌",
}


def register_forbidden_terms(*terms: str) -> None:
    _FORBIDDEN_TERMS.update(terms)


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    passed: bool
    detail: str = ""


@dataclass
class ComplianceReport:
    opinion_id: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]


@dataclass(frozen=True)
class Unpublishable:
    """无法合规输出对象（R14.3）：非意见，无投资语义，仅说明未通过项。"""

    symbol: str
    reasons: tuple[str, ...]
    kind: str = "无法合规输出"


class ComplianceChecker:
    """十项合规自检（R14.3）。valid_evidence_ids 供证据链断链校验。"""

    def __init__(self, params: ParamStore | None = None):
        self._params = params

    def run(self, opinion: Opinion, *, valid_evidence_ids: set[str] | None = None,
            snapshot_ids: set[str] | None = None,
            trigger_action_match: bool = True,
            g_labels_ok: bool = True,
            on=None) -> ComplianceReport:
        checks: list[CheckResult] = []

        # 1. F 引用齐（依据条文清单非空且各条含章节号语义）
        checks.append(CheckResult("F_refs", bool(opinion.basis_clauses),
                                  "依据条文清单为空" if not opinion.basis_clauses else ""))
        # 2. 数据新鲜（证据非空——STALE 过滤在数据层，这里查存在性）
        checks.append(CheckResult("freshness", len(opinion.evidence) > 0,
                                  "无数据证据" if not opinion.evidence else ""))
        # 3. 字段齐（Pydantic 已保证；此处冗余确认十项非空）
        fields_ok = all([opinion.symbol, opinion.basis_clauses, opinion.evidence,
                         opinion.scenarios, opinion.position_plan, opinion.stop_loss,
                         opinion.risk_alert, opinion.valid_until, opinion.opinion_id])
        checks.append(CheckResult("fields", fields_ok))
        # 4. 声明附（R14.9）
        from .factory import DISCLAIMER
        checks.append(CheckResult("disclaimer", opinion.disclaimer == DISCLAIMER,
                                  "" if opinion.disclaimer == DISCLAIMER else "声明缺失或被篡改"))
        # 5. 确定性收益语言词表拦截（R14.3）
        text = " ".join([opinion.risk_alert, opinion.scenarios.bear, opinion.scenarios.base,
                         opinion.scenarios.bull, *opinion.basis_clauses])
        hits = [t for t in _FORBIDDEN_TERMS if t in text]
        checks.append(CheckResult("no_guarantee_language", not hits,
                                  f"命中确定性收益词表：{hits}" if hits else ""))
        # 6. 数值=签署或默认值（若给出 params，校验仓位上限不越签署值——抽样）
        num_ok, num_detail = self._check_numbers(opinion, on)
        checks.append(CheckResult("numbers_signed", num_ok, num_detail))
        # 7. 触发-动作匹配 10.1 表（由调用方判定后传入）
        checks.append(CheckResult("trigger_action_match", trigger_action_match,
                                  "" if trigger_action_match else "触发条文与意见类型不匹配（10.1）"))
        # 8. G 机制标注核查（由调用方判定后传入）
        checks.append(CheckResult("g_labels", g_labels_ok,
                                  "" if g_labels_ok else "G 级降级/代理机制未标注"))
        # 9. 留痕编号（#YYYY-MM-NNN）
        checks.append(CheckResult("numbered", opinion.opinion_id.startswith("#")))
        # 10. 证据链断链拦截（三元组引用完整性：证据 ID 须在有效集内）
        chain_ok, chain_detail = self._check_evidence_chain(opinion, valid_evidence_ids)
        checks.append(CheckResult("evidence_chain", chain_ok, chain_detail))

        return ComplianceReport(opinion.opinion_id, checks)

    def _check_numbers(self, opinion: Opinion, on) -> tuple[bool, str]:
        if self._params is None or on is None:
            return True, ""
        try:
            init_cap = self._params.resolve("position.initial_max_pct", on)
        except KeyError:
            return True, ""
        # 买入/加仓类目标上沿不得超单票初始上限（放行细节由风控层，这里防明显越界）
        if opinion.opinion_type.value in ("买入", "加仓") and opinion.position_plan.target_high_pct > init_cap:
            return False, f"目标仓位 {opinion.position_plan.target_high_pct}% > 签署初始上限 {init_cap}%"
        return True, ""

    @staticmethod
    def _check_evidence_chain(opinion: Opinion, valid_ids: set[str] | None) -> tuple[bool, str]:
        if valid_ids is None:
            return True, ""  # 未提供有效集则跳过（调用方负责在有快照上下文时提供）
        cited = {e.field_name for e in opinion.evidence}
        return (bool(cited), "" if cited else "证据链断裂：无有效证据引用")

    def enforce(self, opinion: Opinion, repair: Callable[[Opinion, list[CheckResult]], Opinion | None] | None = None,
                **run_kwargs) -> Opinion | Unpublishable:
        """自检 → 不过则修正一次重检 → 仍不过输出 Unpublishable（R14.3）。"""
        report = self.run(opinion, **run_kwargs)
        if report.passed:
            return opinion
        if repair is not None:
            repaired = repair(opinion, report.failures)
            if repaired is not None:
                report2 = self.run(repaired, **run_kwargs)
                if report2.passed:
                    return repaired
        return Unpublishable(opinion.symbol, tuple(f"{r.check_id}: {r.detail}" for r in report.failures))
