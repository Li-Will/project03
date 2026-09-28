"""FastAPI 入口：/api/v1/chat + 工单三端点 + 健康探针（M1 MVP 版）。

请求链路（单一入口）：/chat → Intent Router → FAQ 直答 | 建单 | 转人工。
- 状态迁移只经 tickets.update_state（校验+审计）；
- 全局 request_id（uuid4）随响应头返回，日志可追踪；
- requirement：本地模式已用 scripts/init_index.py 建好 Qdrant 集合。
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from project03 import __version__
from project03.biz import faq as faq_biz
from project03.biz import intent as intent_biz
from project03.biz.states import TicketState, StateTransitionError
from project03.biz.tickets import (
    add_message,
    categorize_ticket,
    create_ticket,
    get_or_create_customer,
    get_ticket_history,
    log_event,
    prioritize_ticket,
    update_state,
)
from project03.db import models as db
from project03.rag import store

logger = logging.getLogger("project03.api")

CHAT_REPLY = "您好，我是智能客服小助。\n可以问我：网盘容量、上传下载、会议入会、录制等常见问题；需要人工时直接说「转人工」。"
CHAT_HINT = "如需更详细的人工支持，可回复「转人工」由客服跟进。"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()   # 建表 + 种子客户（幂等）
    yield


app = FastAPI(title="Ticket Agent", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(StateTransitionError)
async def on_state_error(_: Request, exc: StateTransitionError):
    return JSONResponse(status_code=422, content={"error": {"type": "invalid_state_transition", "detail": str(exc)}})


# ---------- 请求/响应模型 ----------

class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    customer_name: str = "访客"


class HumanReplyRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    actor: str = "human"


# ---------- 业务实现（独立函数便于单测） ----------

def _escalate(session: Session, text: str, customer_id: int, intent: str, confidence: float, reason: str) -> dict:
    """建单 + 转人工（ESCALATED）固定链路：审计里能看到完整原因。"""
    ticket = create_ticket(
        session, customer_id, text, intent,
        category=categorize_ticket(text), priority=prioritize_ticket(text),
        reason=reason,
    )
    update_state(session, ticket.id, TicketState.ESCALATED, "system", reason)
    add_message(session, ticket.id, "agent", f"已为您转接人工客服，请稍候，我们会尽快跟进（工单 {ticket.id}）。", "template")
    return {"type": "escalated", "ticket_id": ticket.id, "state": "escalated", "reply": f"已为您转接人工客服，稍后由人工跟进（工单 {ticket.id}）。", "confidence": confidence}


def handle_chat(text: str, customer_name: str = "访客") -> dict:
    """单一入口编排：意图 → 分支（FAQ 直答/建单/转人工/寒暄）。"""
    result = intent_biz.classify_intent(text)
    intent = result.intent

    with db.session_scope() as s:
        cust = get_or_create_customer(s, customer_name)

        if intent == intent_biz.CHAT:
            return {"type": "chat", "intent": "chat", "confidence": result.confidence, "reply": CHAT_REPLY}

        if intent == intent_biz.FAQ:
            faq = faq_biz.answer_faq(text)
            if faq.direct:
                return {
                    "type": "faq", "intent": "faq", "confidence": faq.confidence,
                    "reply": faq.answer, "citations": faq.citations,
                }
            # 低置信：绝不硬答 → 转人工（审计 reason 带分数，可复现）
            reason = f"FAQ 置信度 {faq.confidence:.3f} 低于阈值 {faq_biz.CONF_HINT}，转人工"
            return _escalate(s, text, cust.id, "faq", faq.confidence, reason)

        # ticket：建单受理（M1 人工跟进；M2 接诊断 Agent）
        if intent == intent_biz.TICKET:
            ticket = create_ticket(
                s, cust.id, text, intent,
                category=categorize_ticket(text), priority=prioritize_ticket(text),
                reason="报障建单",
            )
            update_state(s, ticket.id, TicketState.IN_TRIAGE, "system", "受理中，等待处理")
            add_message(s, ticket.id, "agent", f"已为您创建工单 {ticket.id}（{ticket.category}，{ticket.priority}），我们会尽快处理。", "template")
            return {"type": "ticket", "ticket_id": ticket.id, "state": ticket.state, "category": ticket.category, "priority": ticket.priority}

        # complaint / need_human：直接转人工
        reason = intent_biz.escalate_reason(intent, result.triggers)
        return _escalate(s, text, cust.id, intent, result.confidence, reason)


# ---------- 路由 ----------

@app.post("/api/v1/chat")
def chat(req: ChatRequest):
    return handle_chat(req.text, req.customer_name)


@app.get("/api/v1/tickets/{ticket_id}")
def ticket_detail(ticket_id: int):
    with db.session_scope() as s:
        try:
            return get_ticket_history(s, ticket_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/tickets/{ticket_id}/human-reply")
def human_reply(ticket_id: int, req: HumanReplyRequest):
    """人工接管端点：回复内容落库，ESCALATED/受理中 → PENDING_USER（等用户确认）。

    终态（closed/rejected）工单拒绝继续回复 —— 状态机红线在 API 层兜底。
    """
    with db.session_scope() as s:
        history = get_ticket_history(s, ticket_id)  # 不存在会抛 KeyError
        state = history["state"]
        if state in ("closed", "rejected"):
            raise HTTPException(status_code=422, detail=f"工单已处于终态（{state}），不可继续回复")
        add_message(s, ticket_id, "agent", req.content, "human")
        if state in ("escalated", "in_triage", "processing", "new"):
            update_state(s, ticket_id, TicketState.PENDING_USER, req.actor, "人工已回复，等待用户确认")
        new_history = get_ticket_history(s, ticket_id)
        return {"ok": True, "ticket_id": ticket_id, "state": new_history["state"]}


@app.get("/api/v1/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/api/v1/health/ready")
def health_ready():
    """依赖就绪检查：DB 可写 + Qdrant 集合存在。任一不可用 → 503 + 原因（不是 500）。"""
    problems: list[str] = []
    try:
        with db.session_scope() as s:
            s.execute(db.text("select 1"))
    except Exception as exc:  # noqa: BLE001 —— 探针必须拦一切
        problems.append(f"db: {exc.__class__.__name__}")
    try:
        if not store.get_client().collection_exists(store.get_settings().qdrant_collection):
            problems.append("qdrant: collection missing")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"qdrant: {exc.__class__.__name__}")
    if problems:
        return JSONResponse(status_code=503, content={"status": "not_ready", "problems": problems})
    return {"status": "ready"}