"""防幻觉四段校验链（P1 任务 10.2-10.4，需求 R16.2/R16.3/R16.5，design §3.7）。

四段流水线：
①来源回溯——证据数值须在"已取回并归档的原文快照文本"中 ±容差命中
  （机械谓词：仅元数据非空不算通过，必须真的在原文中找到该数值）；
②勾稽——可枚举规则清单，每条带 id/适用前提谓词/容差；内置季度加总
  vs 年度、NI÷EPS vs 加权股数、资产=负债+权益三条，清单可扩展注册；
③标签完整性——新鲜度与口径标签必填；
④双源佐证——关键数字须 ≥2 独立来源（发布主体不同且非同一原始通稿）
  或官方原文（source tier=official）；不足则不可用于判定并产 Gap。

任一段失败 → ``Confidence.UNVERIFIED``。本模块是 UNVERIFIED 的唯一
置位方：``VerificationOutcome.confidence`` 为派生只读属性，构造器不
接受置信参数，调用方无法指定置信（A1）。处置链（R16.3）：失败自动
产 Gap → 登记一次补取证重试 → 仍失败按 A2 降级/滞留；勾稽矛盾单独
路由裁决队列（``to_ruling_queue=True``，未决期间不自动补取证，R6.7）。

容差均为参数（生产环境从规则库注入，业务阈值不硬编码于判定代码，
design §3.2/§3.7——此处默认值仅为技术缺省）。纯确定性，无网络调用。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.types import Confidence, Evidence, Freshness, Gap
from ..data.quality import GapLedger
from .tasks import AgentTask, Attempt, AttemptStatus, TaskManager, TaskStatus


class Stage(StrEnum):
    """校验链四段（R16.2/R16.5）。"""

    SOURCE_TRACE = "source_trace"  # ①来源回溯
    RECONCILIATION = "reconciliation"  # ②勾稽
    LABEL_COMPLETENESS = "label_completeness"  # ③标签完整性
    CORROBORATION = "corroboration"  # ④双源佐证


class SourceTier(StrEnum):
    """来源层级：官方原文单源即可佐证（R16.5）。"""

    OFFICIAL = "official"  # 官方原文（法定披露/监管原文）
    MEDIA = "media"
    AGGREGATOR = "aggregator"


@dataclass(frozen=True)
class SourceRef:
    """来源引用：``snapshot_text`` 为已取回归档的原文快照（重放读档，R3.4）。

    ``origin_id`` 标识原始通稿：同一非空 origin 的多处转载判同源，
    不构成独立来源（R16.4/R16.5）。
    """

    source_id: str
    publisher: str
    tier: SourceTier
    origin_id: str = ""
    snapshot_text: str = ""


@dataclass(frozen=True)
class EvidenceDraft:
    """代理产出的证据草稿——入库前必须过四段校验链（R16.2）。

    ``agent_opinion`` 为代理判定性语言，仅存档参考、不参与校验与判定
    （R16.6，A1）；``statement`` 为代理结构化输出，供勾稽规则消费。
    """

    subject: str
    field_name: str
    value: float
    unit: str = ""
    as_of: str = ""
    freshness_label: str = ""  # ③必填（R2.1 新鲜度）
    basis_label: str = ""  # ③必填（口径）
    is_key_figure: bool = False  # ④双源佐证的适用前提（R16.5）
    sources: tuple[SourceRef, ...] = ()
    statement: Mapping[str, object] = field(default_factory=dict)
    agent_opinion: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> EvidenceDraft:
        """从 fixture/传输字典构造（fixtures/adversarial/samples.json 的 draft 格式）。"""
        sources = tuple(
            SourceRef(
                source_id=str(s["source_id"]),
                publisher=str(s["publisher"]),
                tier=SourceTier(str(s["tier"])),
                origin_id=str(s.get("origin_id", "")),
                snapshot_text=str(s.get("snapshot_text", "")),
            )
            for s in d.get("sources", [])  # type: ignore[union-attr]
        )
        return cls(
            subject=str(d["subject"]),
            field_name=str(d["field_name"]),
            value=float(d["value"]),  # type: ignore[arg-type]
            unit=str(d.get("unit", "")),
            as_of=str(d.get("as_of", "")),
            freshness_label=str(d.get("freshness_label", "")),
            basis_label=str(d.get("basis_label", "")),
            is_key_figure=bool(d.get("is_key_figure", False)),
            sources=sources,
            statement=dict(d.get("statement") or {}),  # type: ignore[call-overload]
            agent_opinion=str(d.get("agent_opinion", "")),
        )


# ---------------------------------------------------------------- ①来源回溯

_NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")


def extract_numbers(text: str) -> list[float]:
    """从原文快照提取全部数值候选（千分位归一）。"""
    return [float(m.group().replace(",", "")) for m in _NUM_RE.finditer(text)]


def value_matches_text(value: float, text: str, rel_tol: float, abs_tol: float = 0.0) -> bool:
    """机械谓词（R16.2①）：数值必须真的在原文快照中出现（±容差）。

    快照文本为空即不通过——仅来源元数据（URL/发布方）非空不算通过。
    """
    if not text.strip():
        return False
    tolerance = max(abs_tol, rel_tol * abs(value))
    return any(abs(candidate - value) <= tolerance for candidate in extract_numbers(text))


# ---------------------------------------------------------------- ②勾稽


@dataclass(frozen=True)
class ReconciliationRule:
    """勾稽规则：id + 适用前提谓词 + 校验函数 + 容差（R16.2②）。

    容差登记在规则条目上（生产从规则库注入，design §3.7）。
    """

    rule_id: str
    description: str
    applies: Callable[[Mapping[str, object]], bool]
    check: Callable[[Mapping[str, object], float], bool]
    tolerance: float


@dataclass(frozen=True)
class ReconciliationFinding:
    rule_id: str
    passed: bool


class ReconciliationRegistry:
    """可枚举勾稽清单：内置规则 + 可扩展注册，id 唯一（R16.2②）。"""

    def __init__(self) -> None:
        self._rules: dict[str, ReconciliationRule] = {}

    def register(self, rule: ReconciliationRule) -> ReconciliationRule:
        if rule.rule_id in self._rules:
            raise ValueError(f"勾稽规则 id 重复：{rule.rule_id}")
        self._rules[rule.rule_id] = rule
        return rule

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(self._rules)

    def evaluate(self, statement: Mapping[str, object]) -> list[ReconciliationFinding]:
        """对满足适用前提的每条规则出具 finding；不适用的规则不出具。"""
        return [
            ReconciliationFinding(rule_id=rule.rule_id, passed=rule.check(statement, rule.tolerance))
            for rule in self._rules.values()
            if rule.applies(statement)
        ]


def _rel_close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)


def builtin_registry(
    quarter_sum_tol: float = 0.01, eps_shares_tol: float = 0.02, balance_tol: float = 0.005
) -> ReconciliationRegistry:
    """内置三条勾稽规则（R16.2②）；容差参数化，清单可继续 ``register`` 扩展。"""
    registry = ReconciliationRegistry()
    registry.register(
        ReconciliationRule(
            rule_id="recon.quarter_sum_vs_annual",
            description="季度加总 vs 年度",
            applies=lambda s: "quarters" in s and "annual" in s,
            check=lambda s, tol: _rel_close(
                sum(float(q) for q in s["quarters"]),  # type: ignore[union-attr]
                float(s["annual"]),  # type: ignore[arg-type]
                tol,
            ),
            tolerance=quarter_sum_tol,
        )
    )
    registry.register(
        ReconciliationRule(
            rule_id="recon.ni_over_eps_vs_weighted_shares",
            description="净利润÷EPS vs 加权平均股数",
            applies=lambda s: all(k in s for k in ("net_income", "eps", "weighted_shares"))
            and float(s["eps"]) != 0,  # type: ignore[arg-type]
            check=lambda s, tol: _rel_close(
                float(s["net_income"]) / float(s["eps"]),  # type: ignore[arg-type]
                float(s["weighted_shares"]),  # type: ignore[arg-type]
                tol,
            ),
            tolerance=eps_shares_tol,
        )
    )
    registry.register(
        ReconciliationRule(
            rule_id="recon.balance_sheet_identity",
            description="资产负债表勾稽：资产 = 负债 + 权益",
            applies=lambda s: all(k in s for k in ("total_assets", "total_liabilities", "total_equity")),
            check=lambda s, tol: _rel_close(
                float(s["total_assets"]),  # type: ignore[arg-type]
                float(s["total_liabilities"]) + float(s["total_equity"]),  # type: ignore[arg-type]
                tol,
            ),
            tolerance=balance_tol,
        )
    )
    return registry


# ---------------------------------------------------------------- ④双源佐证


class CorroborationChecker:
    """双源佐证（R16.4/R16.5）：两源计数按发布主体去重，且同一原始通稿
    的多处转载判同源；官方原文（tier=official）单源即可佐证。
    """

    def __init__(self, min_independent: int = 2):
        self.min_independent = min_independent

    @staticmethod
    def independent_count(sources: Iterable[SourceRef]) -> int:
        """独立来源计数：同发布主体或同一非空原始通稿归并为一个等价类。"""
        classes: list[tuple[set[str], set[str]]] = []  # (publishers, origins)
        for src in sources:
            publisher = src.publisher.strip().lower()
            origin = src.origin_id.strip().lower()
            hits = [c for c in classes if publisher in c[0] or (origin and origin in c[1])]
            merged: tuple[set[str], set[str]] = ({publisher}, {origin} if origin else set())
            for c in hits:
                merged[0].update(c[0])
                merged[1].update(c[1])
                classes.remove(c)
            classes.append(merged)
        return len(classes)

    def check(self, sources: Sequence[SourceRef]) -> bool:
        if any(src.tier is SourceTier.OFFICIAL for src in sources):
            return True
        return self.independent_count(sources) >= self.min_independent


# ---------------------------------------------------------------- 结论与链


class VerificationOutcome:
    """校验结论。``confidence`` 为派生只读属性——由失败段唯一决定，
    构造器不接受置信参数，故本类型（即校验器）是 UNVERIFIED 的唯一
    置位方（A1/R16.2）。
    """

    __slots__ = ("stages_failed", "notes", "to_ruling_queue", "matched_source_ids", "gap")

    def __init__(
        self,
        stages_failed: Iterable[Stage] = (),
        notes: Mapping[str, str] | None = None,
        to_ruling_queue: bool = False,
        matched_source_ids: Iterable[str] = (),
        gap: Gap | None = None,
    ) -> None:
        self.stages_failed: tuple[Stage, ...] = tuple(stages_failed)
        self.notes: dict[str, str] = dict(notes or {})
        self.to_ruling_queue = bool(to_ruling_queue)
        self.matched_source_ids: tuple[str, ...] = tuple(matched_source_ids)
        self.gap = gap

    @property
    def passed(self) -> bool:
        return not self.stages_failed

    @property
    def confidence(self) -> Confidence:
        """唯一置位方：VERIFIED 当且仅当四段全过，否则 UNVERIFIED（R16.2）。"""
        return Confidence.VERIFIED if self.passed else Confidence.UNVERIFIED


class VerificationChain:
    """四段防幻觉校验链（R16.2/R16.5，design §3.7）。容差为参数注入。"""

    def __init__(
        self,
        recon_registry: ReconciliationRegistry | None = None,
        corroboration: CorroborationChecker | None = None,
        trace_rel_tol: float = 0.005,
        trace_abs_tol: float = 0.0,
    ) -> None:
        self.recon_registry = recon_registry or builtin_registry()
        self.corroboration = corroboration or CorroborationChecker()
        self.trace_rel_tol = trace_rel_tol
        self.trace_abs_tol = trace_abs_tol

    def verify(self, draft: EvidenceDraft) -> VerificationOutcome:
        failed: list[Stage] = []
        notes: dict[str, str] = {}

        # ①来源回溯：数值 ±容差在原文快照中命中（机械谓词）
        matched = tuple(
            src
            for src in draft.sources
            if value_matches_text(draft.value, src.snapshot_text, self.trace_rel_tol, self.trace_abs_tol)
        )
        if not matched:
            failed.append(Stage.SOURCE_TRACE)
            notes[Stage.SOURCE_TRACE.value] = "原文快照中未找到该数值（仅元数据非空不算通过）"

        # ②勾稽：可枚举清单逐条（仅适用前提成立者）
        contradictions = [f.rule_id for f in self.recon_registry.evaluate(draft.statement) if not f.passed]
        to_ruling_queue = bool(contradictions)
        if contradictions:
            failed.append(Stage.RECONCILIATION)
            notes[Stage.RECONCILIATION.value] = "勾稽矛盾：" + ",".join(contradictions)

        # ③标签完整性：新鲜度与口径必填
        label_problems = []
        if draft.freshness_label.strip() not in {f.value for f in Freshness}:
            label_problems.append("新鲜度标签缺失/非法")
        if not draft.basis_label.strip():
            label_problems.append("口径标签缺失")
        if label_problems:
            failed.append(Stage.LABEL_COMPLETENESS)
            notes[Stage.LABEL_COMPLETENESS.value] = "；".join(label_problems)

        # ④双源佐证：仅对关键数字、且仅在①命中的来源中计数——
        #   未在原文中含该数值的来源不能为其佐证；①整体失败时④不另计
        #   （证据已被①拦截，缺口出处记①）。
        if Stage.SOURCE_TRACE not in failed and draft.is_key_figure and not self.corroboration.check(matched):
            failed.append(Stage.CORROBORATION)
            notes[Stage.CORROBORATION.value] = "关键数字缺 ≥2 独立来源且无官方原文（R16.5）→ 不可用于判定"

        gap: Gap | None = None
        if failed:
            gap = Gap(
                field_name=draft.field_name,
                subject=draft.subject,
                attempted_paths=tuple(src.source_id for src in draft.sources),
                degradation=(
                    f"防幻觉校验失败[{','.join(s.value for s in failed)}] → UNVERIFIED；"
                    "补取证一次后仍失败按 A2 降级/滞留（R16.3）"
                ),
            )
        return VerificationOutcome(
            stages_failed=failed,
            notes=notes,
            to_ruling_queue=to_ruling_queue,
            matched_source_ids=(src.source_id for src in matched),
            gap=gap,
        )


def build_evidence(draft: EvidenceDraft, outcome: VerificationOutcome, evidence_id: str) -> Evidence:
    """由校验结论落证据对象：置信只取 ``outcome.confidence``（唯一置位方，A1）。

    标签缺失/非法时新鲜度按 STALE 保守归档（判定中视同缺失，R2.1）——
    此时证据必已是 UNVERIFIED（③段拦截），归档仅为留痕。
    """
    try:
        freshness = Freshness(draft.freshness_label.strip())
    except ValueError:
        freshness = Freshness.STALE
    return Evidence(
        evidence_id=evidence_id,
        subject=draft.subject,
        field_name=draft.field_name,
        value=draft.value,
        unit=draft.unit,
        source=";".join(src.source_id for src in draft.sources),
        as_of=draft.as_of,
        freshness=freshness,
        confidence=outcome.confidence,
        provenance=draft.basis_label,
    )


# ---------------------------------------------------------------- 失败处置链


@dataclass(frozen=True)
class Disposition:
    """处置结论（R16.3）。action ∈ accepted / retry_registered / stranded / ruling_queue。"""

    action: str
    gap: Gap | None = None
    to_ruling_queue: bool = False
    retry_attempt_no: int | None = None


def dispose(
    task: AgentTask,
    attempt: Attempt,
    outcome: VerificationOutcome,
    manager: TaskManager,
    ledger: GapLedger | None = None,
) -> Disposition:
    """失败处置链（R16.3）：UNVERIFIED → 自动 Gap → 登记一次补取证重试
    → 仍失败按 A2 降级/滞留；勾稽矛盾单独路由裁决队列（未决期间不
    自动补取证、不重复派发，R6.7）。
    """
    if outcome.passed:
        task.mark(attempt.attempt_no, AttemptStatus.VALID)
        return Disposition(action="accepted")

    task.mark(attempt.attempt_no, AttemptStatus.INVALID)
    if ledger is not None and outcome.gap is not None:
        ledger.register(outcome.gap)

    if outcome.to_ruling_queue:
        return Disposition(action="ruling_queue", gap=outcome.gap, to_ruling_queue=True)

    if task.invalid_count >= 2:
        task.status = TaskStatus.STRANDED
        return Disposition(action="stranded", gap=outcome.gap)

    retry = manager.register_retry(
        task, reason=f"补取证重试（防幻觉校验失败：{','.join(s.value for s in outcome.stages_failed)}）"
    )
    return Disposition(action="retry_registered", gap=outcome.gap, retry_attempt_no=retry.attempt_no)
