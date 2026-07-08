"""代理任务管理（P1 任务 10.1，需求 R16.1/R16.4/R16.7，design §3.7）。

七类取证任务以自然键（标的×任务类型×子项×期次×模板版本）幂等去重：
同自然键重复提交返回已有任务而非新建（R16.4）。重试 = 新 attempt，
判定只读最新有效 attempt；任务模板版本化并声明所需模型档位
（尽调级/抽取级双档，R16.7 / FR-A-04），执行器按档位路由。

本模块纯确定性、无网络调用与 LLM 交互；attempt 的有效性由
verify.py 防幻觉校验链判定后回写——证据置信（Confidence）只能由
校验器置位（A1），本模块不触碰置信字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AgentTaskType(StrEnum):
    """七类代理任务（R16.1）。"""

    VETO_FORENSICS = "veto_forensics"  # 否决取证（S3 需取证子项，R9.3）
    FILING_STRUCTURING = "filing_structuring"  # 财报结构化
    INDUSTRY_EVENT = "industry_event"  # 行业事件
    QUALITATIVE_PRELIM = "qualitative_prelim"  # 定性初评（R10.1）
    RETRO_PREP = "retro_prep"  # 复盘备料
    HOLDING_SIGNAL_FORENSICS = "holding_signal_forensics"  # 持仓信号取证（R12）
    DISRUPTION_CRITERIA_FORENSICS = "disruption_criteria_forensics"  # 颠覆判据取证


class ModelTier(StrEnum):
    """模型档位声明（R16.7，FR-A-04 双档）：执行器按档位路由并计量用量。"""

    DUE_DILIGENCE = "due_diligence"  # 尽调级
    EXTRACTION = "extraction"  # 抽取级


@dataclass(frozen=True)
class TaskTemplate:
    """任务模板：版本化 + 模型档位声明（R16.1/R16.7）。

    完整模板 = prompt + 输出 schema + 勾稽规则集引用（design §3.7）；
    prompt 与 schema 在 P1 后续任务装配，此处以 ``prompt_ref`` 引用。
    """

    task_type: AgentTaskType
    version: str
    model_tier: ModelTier
    prompt_ref: str = ""


#: 默认模板注册表（R16.7）：判断密集型任务（否决取证/定性初评/持仓信号
#: 取证/颠覆判据取证）声明尽调级；结构化抽取型任务（财报结构化/行业
#: 事件/复盘备料）声明抽取级。
DEFAULT_TEMPLATES: dict[AgentTaskType, TaskTemplate] = {
    task_type: TaskTemplate(task_type=task_type, version="v1", model_tier=tier)
    for task_type, tier in {
        AgentTaskType.VETO_FORENSICS: ModelTier.DUE_DILIGENCE,
        AgentTaskType.FILING_STRUCTURING: ModelTier.EXTRACTION,
        AgentTaskType.INDUSTRY_EVENT: ModelTier.EXTRACTION,
        AgentTaskType.QUALITATIVE_PRELIM: ModelTier.DUE_DILIGENCE,
        AgentTaskType.RETRO_PREP: ModelTier.EXTRACTION,
        AgentTaskType.HOLDING_SIGNAL_FORENSICS: ModelTier.DUE_DILIGENCE,
        AgentTaskType.DISRUPTION_CRITERIA_FORENSICS: ModelTier.DUE_DILIGENCE,
    }.items()
}


@dataclass(frozen=True)
class NaturalKey:
    """自然键：标的 × 任务类型 × 子项 × 期次 × 模板版本（R16.4）。"""

    subject: str
    task_type: AgentTaskType
    sub_item: str
    period: str
    template_version: str


class AttemptStatus(StrEnum):
    PENDING = "pending"  # 已派发未校验
    VALID = "valid"  # 产物通过防幻觉校验
    INVALID = "invalid"  # 产物校验失败（证据被置 UNVERIFIED）


class TaskStatus(StrEnum):
    OPEN = "open"
    VERIFIED = "verified"  # 存在有效 attempt
    STRANDED = "stranded"  # 补取证仍失败 → 按 A2 降级/滞留（R16.3）


@dataclass
class Attempt:
    """attempt 链节点：重试 = 新 attempt，旧 attempt 永不改写产物（R16.4）。"""

    attempt_no: int
    status: AttemptStatus = AttemptStatus.PENDING
    evidence_id: str = ""  # 通过校验后关联的证据对象 ID
    note: str = ""


@dataclass
class AgentTask:
    """代理任务：自然键 + 模板（含版本与模型档位）+ attempt 链。"""

    task_id: str
    key: NaturalKey
    template: TaskTemplate
    status: TaskStatus = TaskStatus.OPEN
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def model_tier(self) -> ModelTier:
        """模板声明的模型档位（R16.7）。"""
        return self.template.model_tier

    @property
    def template_version(self) -> str:
        return self.key.template_version

    @property
    def invalid_count(self) -> int:
        return sum(1 for a in self.attempts if a.status is AttemptStatus.INVALID)

    def new_attempt(self, note: str = "") -> Attempt:
        """追加新 attempt（重试语义，R16.4）；编号单调递增。"""
        attempt = Attempt(attempt_no=len(self.attempts) + 1, note=note)
        self.attempts.append(attempt)
        return attempt

    def mark(self, attempt_no: int, status: AttemptStatus, evidence_id: str = "") -> Attempt:
        """回写 attempt 校验结论（由 verify.dispose 调用）。"""
        for attempt in self.attempts:
            if attempt.attempt_no == attempt_no:
                attempt.status = status
                if evidence_id:
                    attempt.evidence_id = evidence_id
                if status is AttemptStatus.VALID:
                    self.status = TaskStatus.VERIFIED
                return attempt
        raise KeyError(f"任务 {self.task_id} 无 attempt #{attempt_no}")

    def latest_valid(self) -> Attempt | None:
        """判定只读最新有效 attempt（R16.4）；无有效 attempt 返回 None（A2：判定不得读）。"""
        for attempt in reversed(self.attempts):
            if attempt.status is AttemptStatus.VALID:
                return attempt
        return None


class TaskManager:
    """任务管理器：自然键幂等去重 + attempt 链登记（R16.4）。"""

    def __init__(self) -> None:
        self._tasks: dict[NaturalKey, AgentTask] = {}
        self._seq = 0

    def __len__(self) -> int:
        return len(self._tasks)

    def submit(self, template: TaskTemplate, subject: str, sub_item: str, period: str) -> AgentTask:
        """幂等提交：同自然键重复提交返回已有任务而非新建（R16.4）。"""
        if not (subject and sub_item and period):
            raise ValueError("自然键各维不得为空（标的/子项/期次）")
        key = NaturalKey(
            subject=subject,
            task_type=template.task_type,
            sub_item=sub_item,
            period=period,
            template_version=template.version,
        )
        existing = self._tasks.get(key)
        if existing is not None:
            return existing
        self._seq += 1
        task = AgentTask(task_id=f"AGT-{self._seq:05d}", key=key, template=template)
        self._tasks[key] = task
        return task

    def get(self, key: NaturalKey) -> AgentTask | None:
        return self._tasks.get(key)

    def register_retry(self, task: AgentTask, reason: str) -> Attempt:
        """登记一次补取证重试（R16.3）：重试 = 新 attempt，不覆写历史。"""
        if task.status is TaskStatus.STRANDED:
            raise ValueError(f"任务 {task.task_id} 已滞留（A2 降级），不再自动补取证")
        return task.new_attempt(note=reason)
