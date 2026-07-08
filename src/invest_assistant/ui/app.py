"""FastAPI 交互层最小应用（任务 14.2/14.3，需求 R17；Docker app 容器入口）。

启动执行前置自检并把 mode=production/degraded 放入 app.state（R1.2）；
暴露签认/裁决队列、成交回执、连通性矩阵、S0 准入接口。所有依赖以 app.state
持有，测试可注入替身。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..data.adapters.misc import build_default_registry
from .queues import (
    QueueAction,
    RulingQueue,
    SignoffItem,
    SignoffItemType,
    SignoffQueue,
    s0_admission,
)
from .receipts import ReceiptService


class SignoffActionRequest(BaseModel):
    item_id: str
    action: str  # approve | reject | modify
    actor: str = "user"
    reason: str = ""


class ReceiptRequest(BaseModel):
    decision_record_id: str
    symbol: str
    action: str
    executed_at: str
    quantity: float
    price: float


class S0Request(BaseModel):
    declared_fund_types: list[str] = []


class SignoffSubmitRequest(BaseModel):
    item_id: str
    item_type: str
    subject: str
    payload: dict[str, Any] = {}


def create_app(*, run_self_check: bool = True) -> FastAPI:
    app = FastAPI(title="智能投资助手交互层", version="0.1.0")
    app.state.signoff = SignoffQueue()
    app.state.ruling = RulingQueue()
    app.state.receipts = ReceiptService()

    # 启动前置自检 → mode（R1.2）
    if run_self_check:
        allow, results = build_default_registry().startup_self_check()
        app.state.mode = "production" if allow else "degraded"
        app.state.connectivity = [
            {"adapter": r.adapter, "ok": r.ok, "required": r.required, "error": r.error}
            for r in results
        ]
    else:
        app.state.mode = "test"
        app.state.connectivity = []

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "mode": app.state.mode}

    @app.get("/connectivity")
    def connectivity() -> dict:
        return {"mode": app.state.mode, "opinion_mode_allowed": app.state.mode == "production",
                "matrix": app.state.connectivity}

    @app.post("/s0/admission")
    def s0(req: S0Request) -> dict:
        d = s0_admission(req.declared_fund_types)
        return {"admitted": d.admitted, "mode": d.mode, "hit_types": list(d.hit_types),
                "reason": d.reason}

    @app.get("/queues/signoff")
    def list_signoff() -> dict:
        return {"pending": [{"item_id": i.item_id, "item_type": i.item_type.value,
                             "subject": i.subject} for i in app.state.signoff.pending()]}

    @app.post("/queues/signoff/submit")
    def submit_signoff(req: SignoffSubmitRequest) -> dict:
        item = SignoffItem(item_id=req.item_id, item_type=SignoffItemType(req.item_type),
                           subject=req.subject, payload=req.payload)
        app.state.signoff.submit(item)
        return {"submitted": req.item_id}

    @app.post("/queues/signoff/act")
    def act_signoff(req: SignoffActionRequest) -> dict:
        try:
            rec = app.state.signoff.act(req.item_id, QueueAction(req.action),
                                        actor=req.actor, reason=req.reason)
        except Exception as e:  # noqa: BLE001 —— 队列校验错误转 400
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"item_id": rec.item_id, "action": rec.action}

    @app.get("/queues/ruling")
    def list_ruling() -> dict:
        return {"pending": [{"item_id": i.item_id, "subject": i.subject, "issue": i.issue}
                            for i in app.state.ruling.pending()]}

    @app.post("/receipts")
    def record_receipt(req: ReceiptRequest) -> dict:
        rec = app.state.receipts.record_receipt(
            req.decision_record_id, symbol=req.symbol, action=req.action,
            executed_at=req.executed_at, quantity=req.quantity, price=req.price)
        return {"trade_id": rec.trade_id}

    return app


# Docker app 容器入口：uvicorn invest_assistant.ui.app:app
app = create_app(run_self_check=False)
