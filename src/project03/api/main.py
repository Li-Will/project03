"""FastAPI 入口（M2 完整版）：/chat（图接管工单）+ /chat/stream(SSE) + 工单生命周期端点。

请求链路：
- FAQ 意图 → 直答（不走图："简单问题不该用 Agent"）；
- TICKET 意图 → LangGraph 工单图（建单→分类→诊断→方案→Triage→finalize|转人工）；
  转人工 = escalate_mark 落库 + escalate 节点 interrupt 挂起；
- 人工回复 → /human-reply：原路恢复图（Command(resume)）或 M1 快速路径；
- complaint/need_human/低置信 FAQ → M1 快速转人工（不诊断，直接交给人类）；
- SLA 调度器随 lifespan 常驻；/admin/escalations 暴露升级队列。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from project03 import __version__
from project03.api.auth import Principal, require_agent, require_staff, resolve_principal, warn_if_auth_disabled
from project03.biz import faq as faq_biz
from project03.biz import intent as intent_biz
from project03.biz import safety as safety_biz
from project03.biz.sla import SlaScheduler, list_escalations
from project03.biz.states import TicketState, StateTransitionError
from project03.biz.tickets import (
    add_message,
    categorize_ticket,
    create_ticket,
    get_or_create_customer,
    get_ticket,
    get_ticket_history,
    prioritize_ticket,
    update_state,
)
from project03.db import models as db
from project03.gql import graph as graph_mod
from project03.rag import store

logger = logging.getLogger("project03.api")

CHAT_REPLY = "您好，我是智能客服小助。\n可以问我：网盘容量、上传下载、会议入会、录制等常见问题；需要人工时直接说「转人工」。"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()   # 建表 + 种子客户（幂等）
    warn_if_auth_disabled()   # 未启用鉴权时明确告警（不假装安全）
    scheduler = SlaScheduler()
    task = asyncio.create_task(scheduler.run())
    try:
        yield
    finally:
        scheduler.stop()
        task.cancel()


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
    # ⚠️ 兼容字段：**不采信**。坐席身份一律来自 X-API-Key（否则任何人都能冒充人工回消息）
    actor: str = "human"


# ---------- 业务实现（独立函数便于单测） ----------

def _escalate_fast(session: Session, text: str, customer_id: int, intent: str, confidence: float,
                   reason: str, tenant_id: str = db.DEFAULT_TENANT) -> dict:
    """快速转人工（需人工介入且无需诊断的路径：投诉/低置信/用户要求/高危事件）。"""
    ticket = create_ticket(
        session, customer_id, text, intent,
        category=categorize_ticket(text), priority=prioritize_ticket(text),
        reason=reason, tenant_id=tenant_id,
    )
    update_state(session, ticket.id, TicketState.ESCALATED, "system", reason)
    add_message(session, ticket.id, "agent", f"已为您转接人工客服，请稍候，我们会尽快跟进（工单 {ticket.id}）。", "template")
    return {"type": "escalated", "ticket_id": ticket.id, "state": "escalated",
            "reply": f"已为您转接人工客服，稍后由人工跟进（工单 {ticket.id}）。", "confidence": confidence}


def _answer_faq_direct(text: str, customer_name: str = "访客", tenant: str = db.DEFAULT_TENANT) -> dict:
    faq = faq_biz.answer_faq(text)
    if faq.direct:
        return {"type": "faq", "intent": "faq", "confidence": faq.confidence,
                "reply": faq.answer, "citations": faq.citations}
    with db.session_scope() as s:
        cust = get_or_create_customer(s, customer_name, tenant_id=tenant)
        return _escalate_fast(s, text, cust.id, "faq", faq.confidence,
                              f"FAQ 置信度 {faq.confidence:.3f} 低于阈值 {faq_biz.CONF_HINT}，转人工",
                              tenant_id=tenant)


def _answer_ticket_graph(text: str, customer_name: str, tenant: str = db.DEFAULT_TENANT) -> dict:
    """TICKET 意图 → LangGraph 工单图。返回对外响应结构。"""
    run = graph_mod.run_ticket_graph(text, customer_name, tenant)
    res = run["result"]
    ticket_id = res.get("ticket_id")
    if res.get("escalate"):
        return {"type": "escalated", "ticket_id": ticket_id, "state": "escalated",
                "reply": f"诊断评估认为该问题需要人工介入（{res.get('escalate_reason', '')}），已为您转人工（工单 {ticket_id}）。",
                "reason": res.get("escalate_reason"), "diagnosis_steps": res.get("diagnosis_steps")}
    return {"type": "ticket", "ticket_id": ticket_id, "state": "resolved",
            "category": res.get("category"), "priority": res.get("priority"),
            "reply": res.get("solution", ""), "citations": res.get("citations"),
            "diagnosis_steps": res.get("diagnosis_steps")}


def handle_chat(text: str, customer_name: str = "访客", tenant: str = db.DEFAULT_TENANT) -> dict:
    """单一入口编排：**高危前置闸** → 意图 → 分支（FAQ 直答 / 图诊断 / 快速转人工 / 寒暄）。

    高危前置闸必须在这里：评测的 diagnose/escalate 用例直接调图，所以图内的 P1 规则
    被验证过；但用户真正的入口是本函数 —— 缺了这道闸，"照片被弄丢了"这类事件会被
    意图分类落到 FAQ 分支、检索到一篇 0.69 分的无关文章就直接回答（真实缺陷，见 biz/safety.py）。
    """
    result = intent_biz.classify_intent(text)

    # 0) 数据/账号/资金安全：不让 FAQ 直答吞掉（"该转不转是事故"）
    risk = safety_biz.high_risk_reason(text)
    if risk:
        with db.session_scope() as s:
            cust = get_or_create_customer(s, customer_name, tenant_id=tenant)
            return _escalate_fast(s, text, cust.id, intent_biz.NEED_HUMAN, result.confidence, risk,
                                  tenant_id=tenant)

    intent = result.intent
    if intent == intent_biz.CHAT:
        return {"type": "chat", "intent": "chat", "confidence": result.confidence, "reply": CHAT_REPLY}
    if intent == intent_biz.FAQ:
        return _answer_faq_direct(text, customer_name, tenant)
    if intent == intent_biz.TICKET:
        return _answer_ticket_graph(text, customer_name, tenant)

    # complaint / need_human：直接转人工（无需诊断）
    with db.session_scope() as s:
        cust = get_or_create_customer(s, customer_name, tenant_id=tenant)
        reason = intent_biz.escalate_reason(intent, result.triggers)
        return _escalate_fast(s, text, cust.id, intent, result.confidence, reason, tenant_id=tenant)


# ---------- 路由 ----------

@app.post("/api/v1/chat")
def chat(req: ChatRequest, principal: Principal = Depends(resolve_principal)):
    """用户侧入口：不强制鉴权，但租户从 X-API-Key 推断（无 key 归默认租户）。"""
    return handle_chat(req.text, req.customer_name, principal.tenant)


@app.get("/api/v1/chat/stream")
def chat_stream(text: str = Query(min_length=1, max_length=500), customer_name: str = "访客",
                principal: Principal = Depends(resolve_principal)):
    """SSE 流式；TICKET 意图逐节点推送诊断过程，其余意图一次性 done。"""
    def gen():
        yield "retry: 3000\n\n"
        payload_start = json.dumps({"text": text}, ensure_ascii=False)
        yield f"event: start\ndata: {payload_start}\n\n"
        ir = intent_biz.classify_intent(text)
        yield f"event: intent\ndata: {json.dumps({'intent': ir.intent, 'confidence': ir.confidence}, ensure_ascii=False)}\n\n"
        if ir.intent == intent_biz.TICKET:
            tid, events = None, []
            try:
                for thread_id, node_name, update in graph_mod.stream_ticket_graph(
                        text, customer_name, principal.tenant):
                    tid = thread_id
                    if node_name in ("diagnose", "write_solution", "triage"):
                        events.append((node_name, update))
                        data = json.dumps({"node": node_name,
                                           "diagnosis_steps": update.get("diagnosis_steps", []),
                                           "evidence": [c.get("id") for c in update.get("evidence", [])]},
                                          ensure_ascii=False)
                        yield f"event: node\ndata: {data}\n\n"
            except Exception as exc:  # noqa: BLE001 —— SSE 通信不可因单点异常中断
                logger.exception("chat/stream 图执行异常")
                yield f"event: error\ndata: {json.dumps({'detail': str(exc)}, ensure_ascii=False)}\n\n"
                return
            final = _answer_ticket_graph(text, customer_name, principal.tenant) if tid is None else {"skip": True}
            if final.get("skip"):
                # stream 已跑完（含 interrupt 挂起）：按最终图状态回查工单
                final = _result_from_thread(tid)
            yield f"event: done\ndata: {json.dumps(final, ensure_ascii=False)}\n\n"
        else:
            final = handle_chat(text, customer_name, principal.tenant)
            yield f"event: done\ndata: {json.dumps(final, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _result_from_thread(thread_id: str | None) -> dict:
    """stream 跑完后按 checkpoint 末态回查工单（用于最终事件）。"""
    from sqlalchemy import select
    with db.session_scope() as s:
        t = s.scalar(select(db.Ticket).where(db.Ticket.graph_thread_id == thread_id)) if thread_id else None
        if t is None:
            return {"type": "ticket", "ticket_id": None, "state": "unknown", "reply": ""}
        if t.state == "escalated":
            return {"type": "escalated", "ticket_id": t.id, "state": "escalated",
                    "reply": f"已为您转人工（工单 {t.id}），请等待人工回复。"}
        return {"type": "ticket", "ticket_id": t.id, "state": t.state, "reply": ""}


@app.get("/api/v1/tickets/{ticket_id}")
def ticket_detail(ticket_id: int, principal: Principal = Depends(resolve_principal)):
    """工单详情：**只看得到自己租户的单**（跨租户返回 404，不泄露存在性）。"""
    with db.session_scope() as s:
        try:
            return get_ticket_history(s, ticket_id, tenant_id=principal.tenant)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/tickets/{ticket_id}/human-reply")
def human_reply(ticket_id: int, req: HumanReplyRequest, principal: Principal = Depends(require_agent)):
    """人工接管端点：有挂起的 LangGraph 诊断 → resume 原路恢复；否则快速路径。

    - **鉴权**：坐席端点，REQUIRE_AUTH=true 时必须带有效 X-API-Key；
    - **身份**：actor 取凭据（principal.actor），请求体里的 actor 一律忽略；
    - **租户**：跨租户工单 404；
    - 终态工单拒绝回复（状态机红线兜底）。
    """
    actor = principal.actor
    with db.session_scope() as s:
        try:
            history = get_ticket_history(s, ticket_id, tenant_id=principal.tenant)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        state = history["state"]
    if state in ("closed", "rejected"):
        raise HTTPException(status_code=422, detail=f"工单已处于终态（{state}），不可继续回复")

    if state == "escalated":
        with db.session_scope() as s:
            t = s.get(db.Ticket, ticket_id)
            graph_thread = t.graph_thread_id if t else None
        if graph_thread:
            graph_mod.resume_ticket_graph(graph_thread, req.content, actor)  # apply_human：PENDING_USER
            with db.session_scope() as s:
                return {"ok": True, "ticket_id": ticket_id, "state": get_ticket(s, ticket_id).state}

    with db.session_scope() as s:  # 无图路径：加消息 + 受理中/转人工 → PENDING_USER
        get_ticket(s, ticket_id, tenant_id=principal.tenant)   # 租户复核（防御性）
        add_message(s, ticket_id, "agent", req.content, "human")
        if state in ("escalated", "in_triage", "processing", "new"):
            update_state(s, ticket_id, TicketState.PENDING_USER, actor, "人工已回复，等待用户确认")
        return {"ok": True, "ticket_id": ticket_id, "state": get_ticket(s, ticket_id).state}


@app.post("/api/v1/tickets/{ticket_id}/ack")
def ack_ticket(ticket_id: int, rating: int | None = Query(default=None, ge=1, le=5),
               principal: Principal = Depends(resolve_principal)):
    """用户确认解决：RESOLVED → CLOSED（附满意度入库）。"""
    with db.session_scope() as s:
        try:
            t = get_ticket(s, ticket_id, tenant_id=principal.tenant)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        cur = TicketState(t.state)
        if cur == TicketState.RESOLVED:
            update_state(s, ticket_id, TicketState.CLOSED, "customer", "用户确认解决，关闭")
        elif cur == TicketState.PENDING_USER:
            update_state(s, ticket_id, TicketState.RESOLVED, "customer", "用户反馈已解决")
            update_state(s, ticket_id, TicketState.CLOSED, "customer", "满意度确认后关闭")
        else:
            raise HTTPException(status_code=422, detail=f"当前状态 {cur.value} 不能确认关闭")
        if rating is not None:
            s.add(db.Review(ticket_id=ticket_id, rating=rating, comment="ack"))
        return {"ok": True, "ticket_id": ticket_id, "state": get_ticket(s, ticket_id).state}


@app.get("/api/v1/admin/escalations")
def admin_escalations(limit: int = Query(default=20, ge=1, le=100),
                      principal: Principal = Depends(require_staff)):
    """管理面升级队列（坐席只读端点：需鉴权；只列本租户工单，按 SLA 死限升序）。"""
    with db.session_scope() as s:
        items = list_escalations(s, limit=limit, tenant_id=principal.tenant)
        return {"count": len(items), "tenant": principal.tenant, "items": items}


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


# 静态演示页（M4 收口），M2 先挂载
try:
    _static_dir = Path(__file__).resolve().parents[3] / "static"
except NameError:
    _static_dir = None
if _static_dir and _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")