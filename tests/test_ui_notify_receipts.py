"""通知强送达（R17.3）+ 成交回执缺失冻结（任务 14.6，R6.3）+ FMP 切换（R1.4）+ 违规台账（R18.3）。"""

import datetime as dt

import pytest

from invest_assistant.audit.violations import ViolationCategory, ViolationLedger
from invest_assistant.data.source_role import (
    arbiter_wins_on_conflict,
    configure_fmp_token,
    default_us_table,
)
from invest_assistant.ui.notify import Alert, Channel, NotificationService, StricterOnlyViolation
from invest_assistant.ui.receipts import DecisionRef, ReceiptService

CLK = lambda: dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.UTC)  # noqa: E731


# ---------- 通知强送达 ----------


def test_defensive_alert_strong_delivery():
    sent = []
    chain = [Channel("im", lambda a: sent.append(("im", a.alert_id)) or True),
             Channel("sms", lambda a: sent.append(("sms", a.alert_id)) or True)]
    svc = NotificationService(chain, clock=CLK)
    d = svc.send_defensive_alert(Alert("al1", "AMD", "止损减半", "defensive"))
    assert sent and not d.acked
    svc.confirm_delivery(d.delivery_id)
    assert svc.delivery(d.delivery_id).acked


def test_ack_timeout_only_stricter():
    chain = [Channel("im", lambda a: True)]
    with pytest.raises(StricterOnlyViolation):
        NotificationService(chain, clock=CLK, ack_timeout_minutes=60)  # >30 默认被拒
    NotificationService(chain, clock=CLK, ack_timeout_minutes=10)  # 更严 OK


def test_escalation_on_timeout():
    calls = []
    chain = [Channel("im", lambda a: calls.append("im") or True),
             Channel("sms", lambda a: calls.append("sms") or True)]
    t = [dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.UTC)]
    svc = NotificationService(chain, clock=lambda: t[0], ack_timeout_minutes=30)
    svc.send_defensive_alert(Alert("al2", "NVDA", "清仓", "defensive"))
    t[0] = t[0] + dt.timedelta(minutes=31)  # 超时未确认
    escalated = svc.check_escalations()
    assert escalated and calls.count("sms") >= 1  # 升级到第二通道重发


# ---------- 成交回执缺失冻结 ----------


def test_missing_receipt_freeze_advice():
    svc = ReceiptService(clock=CLK)
    svc.record_receipt("d1", symbol="AMD", action="new_open", executed_at="2026-07-08",
                       quantity=100, price=150)
    report = svc.detect_missing([
        DecisionRef("d1", "AMD", "new_open"),           # 有回执
        DecisionRef("d2", "AVGO", "add"),               # 缺回执（扩权类）→ 冻结建议
    ])
    frozen = {a.decision_record_id for a in report.freeze_advices}
    assert "d2" in frozen and "d1" not in frozen


def test_defensive_missing_receipt_no_freeze():
    """防守类决策缺回执如实登记但不建议冻结（A5）。"""
    svc = ReceiptService(clock=CLK)
    report = svc.detect_missing([DecisionRef("d3", "AMD", "stop_loss_halve")])
    assert "d3" in report.defensive_missing
    assert all(a.decision_record_id != "d3" for a in report.freeze_advices)


# ---------- FMP 切换 ----------


def test_fmp_switch_rewrites_roles():
    t = default_us_table(on="2026-07-07")
    assert t.resolve("D2", "US", "2026-07-08")["primary"] == "ADP-EDGAR"
    configure_fmp_token(t, on="2026-07-09")
    roles = t.resolve("D2", "US", "2026-07-10")
    assert roles["primary"] == "ADP-FMP" and roles["backup"] == "ADP-EDGAR"
    # 冲突以 EDGAR 法定披露为准
    assert arbiter_wins_on_conflict(t, "D2", "US", "2026-07-10") == "ADP-EDGAR"


# ---------- 违规台账 ----------


def test_violation_ledger_seven_categories_and_agenda():
    led = ViolationLedger()
    led.register(ViolationCategory.REDLINE_TOUCH, "BIG", "单票 30%>25%", "2026-07-08")
    e2 = led.register(ViolationCategory.COOLOFF_BREACH, "AMD", "拆单避冷静期", "2026-07-08")
    assert len(led.review_agenda()) == 2  # 未闭环全进复盘议程
    led.close(e2.entry_id, "2026-07-09")
    agenda = led.review_agenda()
    assert len(agenda) == 1 and agenda[0].category is ViolationCategory.REDLINE_TOUCH


def test_all_seven_categories_exist():
    assert len(list(ViolationCategory)) == 7
