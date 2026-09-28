"""工单 CRUD：建单/改状态/加消息/查历史。

约定（面试口径）：
- 状态更新唯一入口 update_state()——内部先 validate_transition 再落库，
  非法迁移抛 StateTransitionError 且不产生任何写入（原子性由 session_scope 保证）；
- 每次合法迁移写一条 TicketEvent（审计，前端/演示页可回放）；
- category/priority 由规则函数给出（M1 关键词版，M2 接 Agent 分类）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from project03.biz.states import TicketState, validate_transition
from project03.db.models import Customer, Message, Ticket, TicketEvent, utcnow


class TicketNotFoundError(KeyError):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(f"工单不存在: {ticket_id}")


def get_or_create_customer(session: Session, name: str) -> Customer:
    name = name.strip() or "访客"
    row = session.scalar(select(Customer).where(Customer.name == name))
    if row:
        return row
    cust = Customer(name=name, tier="普通")
    session.add(cust)
    session.flush()
    return cust


def log_event(
    session: Session,
    ticket_id: int,
    from_state: TicketState | None,
    to_state: TicketState,
    actor: str,
    reason: str,
) -> None:
    session.add(
        TicketEvent(
            ticket_id=ticket_id,
            from_state=from_state.value if from_state else None,
            to_state=to_state.value,
            actor=actor,
            reason=reason,
        )
    )


def create_ticket(
    session: Session,
    customer_id: int,
    text: str,
    intent: str,
    category: str = "",
    priority: str = "P3",
    reason: str = "创建工单",
) -> Ticket:
    """新单落 NEW 状态 + 首条客户消息 + create 审计事件。"""
    ticket = Ticket(
        customer_id=customer_id,
        intent=intent,
        category=category,
        priority=priority,
        state=TicketState.NEW.value,
    )
    session.add(ticket)
    session.flush()
    session.add(Message(ticket_id=ticket.id, sender="customer", content=text, source="customer"))
    log_event(session, ticket.id, None, TicketState.NEW, "system", reason)
    return ticket


def update_state(
    session: Session,
    ticket_id: int,
    to_state: TicketState,
    actor: str,
    reason: str,
) -> Ticket:
    """状态迁移唯一入口：先校验（非法直接抛，不落库），后写入 + 审计。"""
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketNotFoundError(ticket_id)
    src = TicketState(ticket.state)
    validate_transition(src, to_state)  # 非法 → StateTransitionError
    ticket.state = to_state.value
    ticket.updated_at = utcnow()
    if to_state is TicketState.ESCALATED:
        ticket.assignee = "human"
    log_event(session, ticket.id, src, to_state, actor, reason)
    return ticket


def add_message(
    session: Session,
    ticket_id: int,
    sender: str,
    content: str,
    source: str,
) -> Message:
    msg = Message(ticket_id=ticket_id, sender=sender, content=content, source=source)
    session.add(msg)
    session.flush()
    return msg


def get_ticket(session: Session, ticket_id: int) -> Ticket:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketNotFoundError(ticket_id)
    return ticket


def get_ticket_history(session: Session, ticket_id: int) -> dict:
    """工单全景：基本信息 + 消息流 + 状态迁移审计（API 详情/演示页用）。"""
    ticket = get_ticket(session, ticket_id)
    messages = session.scalars(
        select(Message).where(Message.ticket_id == ticket_id).order_by(Message.id)
    ).all()
    events = session.scalars(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket_id).order_by(TicketEvent.id)
    ).all()
    return {
        "id": ticket.id,
        "intent": ticket.intent,
        "category": ticket.category,
        "priority": ticket.priority,
        "state": ticket.state,
        "assignee": ticket.assignee,
        "created_at": ticket.created_at.isoformat(),
        "messages": [
            {"sender": m.sender, "content": m.content, "source": m.source, "at": m.created_at.isoformat()}
            for m in messages
        ],
        "events": [
            {"from": e.from_state, "to": e.to_state, "actor": e.actor, "reason": e.reason, "at": e.created_at.isoformat()}
            for e in events
        ],
    }


# ---- 规则版分类/定级（M1；M2 由 Ticket Classifier Agent 增强）----

_CLOUD_KW = ("云盘", "上传", "下载", "同步", "分享", "容量", "存储", "备份", "相册", "网盘")
_MEET_KW = ("会议", "摄像头", "麦克风", "屏幕共享", "录制", "字幕", "入会", "视频", "音频")
_P1_KW = ("支付", "扣款", "账号被盗", "数据丢失", "无法登录", "紧急", "立刻")

def categorize_ticket(text: str) -> str:
    """产品线品类：按关键词归属，都不中给综合。"""
    if any(k in text for k in _MEET_KW):
        return "会议支持"
    if any(k in text for k in _CLOUD_KW):
        return "云盘服务"
    return "综合"


def prioritize_ticket(text: str) -> str:
    """P1 语义：资金/账号/数据紧急事件；其余按含"急/立刻"与否 P2/P3。"""
    if any(k in text for k in _P1_KW):
        return "P1"
    if ("急" in text) or ("马上" in text):
        return "P2"
    return "P3"