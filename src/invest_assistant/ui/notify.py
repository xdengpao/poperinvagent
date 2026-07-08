"""通知服务（任务 21.1，需求 R17.3；design §3.8 通道抽象）。

- 通道抽象：IM/短信 webhook 的发送函数**注入**（本模块不做任何真实网络调用）；
- 防守类警报走强送达路径：即时推送 → 送达确认 → 超时升级重发
  （升级链可注入多通道，逐级切换；超时默认 30 分钟墙钟，只可更严=只可调小，R17.3）；
- 扩权类通知进日摘要队列，按日汇总推送（R17.3）；
- 业务时限口径：30 分钟为墙钟口径（R4.3）；calendar.Duration 无分钟粒度，
  故经注入时钟以秒差比较实现，不构造裸 timedelta（任务 1.4 lint 约束）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

DEFAULT_ACK_TIMEOUT_MINUTES = 30
"""送达确认超时默认 30 分钟墙钟（R17.3：实现级参数，只可更严=只可调小）。"""


class NotifyError(Exception):
    """通知服务错误基类。"""


class StricterOnlyViolation(NotifyError):
    """送达确认超时只可更严（≤30 分钟），放宽方向的配置被拒（R17.3）。"""


class UnknownDeliveryError(NotifyError):
    """送达单不存在。"""


@dataclass(frozen=True)
class Alert:
    """警报/通知语义对象。klass 与冻结矩阵动作分类同口径（defensive/expansive）。"""

    alert_id: str
    subject: str
    message: str
    klass: str = "defensive"


SendFn = Callable[[Alert], bool]
"""可注入发送函数：``(alert) -> 通道是否接受发送``。真实实现为 webhook POST，测试注入 fake。"""


@dataclass(frozen=True)
class Channel:
    """送达通道抽象（IM webhook / 短信 webhook，R17.3）。"""

    name: str
    send: SendFn
    kind: str = "im_webhook"


@dataclass(frozen=True)
class DeliveryAttempt:
    """一次推送尝试留痕。"""

    channel: str
    sent_at: str  # ISO 时间戳
    accepted: bool
    escalation_level: int  # 0=首发，≥1=升级重发


@dataclass
class Delivery:
    """强送达单（防守类警报）：即时推送 → 等待送达确认 → 超时升级重发（R17.3）。"""

    delivery_id: str
    alert: Alert
    attempts: list[DeliveryAttempt] = field(default_factory=list)
    acked: bool = False
    ack_at: str = ""
    escalation_level: int = 0
    last_sent_at: dt.datetime | None = None


@dataclass(frozen=True)
class DailyDigest:
    """扩权类日摘要（R17.3）。"""

    digest_id: str
    generated_at: str
    alerts: tuple[Alert, ...]


Clock = Callable[[], dt.datetime]


class NotificationService:
    """通知服务（R17.3）：防守类强送达 + 扩权类日摘要。"""

    def __init__(
        self,
        escalation_chain: Sequence[Channel],
        *,
        clock: Clock | None = None,
        ack_timeout_minutes: int = DEFAULT_ACK_TIMEOUT_MINUTES,
    ) -> None:
        if not escalation_chain:
            raise NotifyError("防守类强送达至少需要一条通道（R17.3）")
        if ack_timeout_minutes > DEFAULT_ACK_TIMEOUT_MINUTES:
            raise StricterOnlyViolation(
                f"送达确认超时只可更严（≤{DEFAULT_ACK_TIMEOUT_MINUTES} 分钟墙钟），"
                f"拒绝 {ack_timeout_minutes}（R17.3）"
            )
        if ack_timeout_minutes < 1:
            raise ValueError("超时必须 ≥1 分钟")
        self._chain = tuple(escalation_chain)
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._timeout_minutes = ack_timeout_minutes
        self._deliveries: dict[str, Delivery] = {}
        self._digest_queue: list[Alert] = []

    # —— 防守类：强送达 ——
    def send_defensive_alert(self, alert: Alert) -> Delivery:
        """防守类警报即时推送（首发走升级链第一通道），建强送达单等待确认（R17.3）。"""
        delivery = Delivery(delivery_id=uuid.uuid4().hex, alert=alert)
        self._deliveries[delivery.delivery_id] = delivery
        self._push(delivery, level=0)
        return delivery

    def _push(self, delivery: Delivery, level: int) -> None:
        channel = self._chain[min(level, len(self._chain) - 1)]
        now = self._clock()
        accepted = bool(channel.send(delivery.alert))
        delivery.attempts.append(
            DeliveryAttempt(
                channel=channel.name,
                sent_at=now.isoformat(timespec="seconds"),
                accepted=accepted,
                escalation_level=level,
            )
        )
        delivery.escalation_level = level
        delivery.last_sent_at = now

    def confirm_delivery(self, delivery_id: str) -> None:
        """登记送达确认回执（R17.3）。"""
        d = self._deliveries.get(delivery_id)
        if d is None:
            raise UnknownDeliveryError(f"送达单不存在：{delivery_id}")
        d.acked = True
        d.ack_at = self._clock().isoformat(timespec="seconds")

    def check_escalations(self) -> list[Delivery]:
        """扫描超时未确认的强送达单并升级重发（R17.3）。

        超时 = 距最近一次推送 ≥ 超时分钟数（墙钟，注入时钟比较）；
        升级切换到升级链下一通道，链尾之后停留在链尾持续重发。
        返回本次被升级的送达单清单。
        """
        now = self._clock()
        escalated: list[Delivery] = []
        for d in self._deliveries.values():
            if d.acked or d.last_sent_at is None:
                continue
            elapsed_s = (now - d.last_sent_at).total_seconds()
            if elapsed_s >= self._timeout_minutes * 60:
                self._push(d, level=d.escalation_level + 1)
                escalated.append(d)
        return escalated

    def delivery(self, delivery_id: str) -> Delivery:
        d = self._deliveries.get(delivery_id)
        if d is None:
            raise UnknownDeliveryError(f"送达单不存在：{delivery_id}")
        return d

    def unacked(self) -> tuple[Delivery, ...]:
        return tuple(d for d in self._deliveries.values() if not d.acked)

    # —— 扩权类：日摘要 ——
    def enqueue_expansive(self, alert: Alert) -> None:
        """扩权类通知入日摘要队列（不即时推送，R17.3）。"""
        self._digest_queue.append(alert)

    def digest_queue(self) -> tuple[Alert, ...]:
        return tuple(self._digest_queue)

    def flush_daily_digest(self) -> DailyDigest:
        """日终汇总扩权类通知为日摘要对象并清空队列（推送经普通通道，由调用方发送）。"""
        digest = DailyDigest(
            digest_id=uuid.uuid4().hex,
            generated_at=self._clock().isoformat(timespec="seconds"),
            alerts=tuple(self._digest_queue),
        )
        self._digest_queue.clear()
        return digest
