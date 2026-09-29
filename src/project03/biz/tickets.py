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
from project03.db.models import DEFAULT_TENANT, Customer, Message, Ticket, TicketEvent, utcnow


class TicketNotFoundError(KeyError):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(f"工单不存在: {ticket_id}")


def get_or_create_customer(session: Session, name: str, tenant_id: str = DEFAULT_TENANT) -> Customer:
    """客户名在**租户内**唯一（同名客户可属于不同租户）。"""
    name = name.strip() or "访客"
    row = session.scalar(
        select(Customer).where(Customer.tenant_id == tenant_id, Customer.name == name)
    )
    if row:
        return row
    cust = Customer(tenant_id=tenant_id, name=name, tier="普通")
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


def sla_deadline_for(priority: str):
    """按优先级算死限（延迟导入 sla：两个模块互相依赖，模块级 import 会成环 ——
    与 sla.py 对本模块的依赖方式保持一致）。"""
    from project03.biz.sla import DEADLINES
    return utcnow() + DEADLINES.get(priority, DEADLINES["P3"])


def create_ticket(
    session: Session,
    customer_id: int,
    text: str,
    intent: str,
    category: str = "",
    priority: str = "P3",
    reason: str = "创建工单",
    tenant_id: str = DEFAULT_TENANT,
) -> Ticket:
    """新单落 NEW 状态 + 首条客户消息 + create 审计事件（带租户归属），并**在建单时开始计时**。

    为什么建单就写 SLA 死限（此前只有图内受理节点才写）：
        投诉 / 低置信 / 高危事件走的是"快速转人工"路径，**不经过图**。
        如果死限只在受理节点写，这些单的 `sla_deadline` 永远是 NULL，
        而 `scan_sla` 会 `if t.sla_deadline is None: continue` 跳过它们 ——
        也就是**最该被 SLA 兜住的单，反而永远不会超时升级**。
        计时起点与业务事实一致才谈得上 SLA：对客服来说"受理"就是建单的那一刻。
    """
    ticket = Ticket(
        tenant_id=tenant_id,
        customer_id=customer_id,
        intent=intent,
        category=category,
        priority=priority,
        state=TicketState.NEW.value,
        sla_deadline=sla_deadline_for(priority),
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


def get_ticket(session: Session, ticket_id: int, tenant_id: str | None = None) -> Ticket:
    """取工单；给 tenant_id 时做租户校验 —— 跨租户一律 404（不泄露存在性）。"""
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketNotFoundError(ticket_id)
    if tenant_id is not None and ticket.tenant_id != tenant_id:
        raise TicketNotFoundError(ticket_id)
    return ticket


def get_ticket_history(session: Session, ticket_id: int, tenant_id: str | None = None) -> dict:
    """工单全景：基本信息 + 消息流 + 状态迁移审计（API 详情/演示页用）。"""
    ticket = get_ticket(session, ticket_id, tenant_id=tenant_id)
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
        # 死限：坐席看详情必须知道"还剩多久"（工作台据此画 SLA 条；不写死限的单为 None）
        "sla_deadline": ticket.sla_deadline.isoformat() if ticket.sla_deadline else None,
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
_MEET_KW = ("会议", "开会", "摄像头", "麦克风", "屏幕共享", "录制", "字幕", "入会", "视频", "音频")
_P1_KW = ("支付", "扣款", "扣钱", "乱扣", "多扣", "扣了两次", "账号被盗", "数据丢失", "丢了", "无法登录", "紧急", "立刻")

# 品类枚举的唯一来源（工作台 /api/v1/meta 暴露它，前端不另抄一份）
CAT_CLOUD = "云盘服务"
CAT_MEET = "会议支持"
CAT_OTHER = "综合"
CATEGORIES: tuple[str, ...] = (CAT_CLOUD, CAT_MEET, CAT_OTHER)


def categorize_ticket(text: str) -> str:
    """产品线品类：按关键词归属，都不中给综合。"""
    if any(k in text for k in _MEET_KW):
        return CAT_MEET
    if any(k in text for k in _CLOUD_KW):
        return CAT_CLOUD
    return CAT_OTHER


def prioritize_ticket(text: str) -> str:
    """P1 语义：资金/账号/数据紧急事件；其余按含"急/立刻"与否 P2/P3。"""
    if any(k in text for k in _P1_KW):
        return "P1"
    if ("急" in text) or ("马上" in text):
        return "P2"
    return "P3"


def resolved_solutions(session: Session, category: str, limit: int = 3,
                       tenant_id: str | None = None) -> list[dict]:
    """历史已解决工单的最终回复（source=diagnosis/human），作诊断补充证据。

    只取 state ∈ {resolved, closed} 且同分类工单的最近一条坐席/诊断消息；
    若工单无坐席消息则跳过（证据必须可归因，不拿空壳凑数）。
    """
    q = select(Ticket.id, Ticket.category).where(Ticket.state.in_(["resolved", "closed"]))
    if tenant_id is not None:
        q = q.where(Ticket.tenant_id == tenant_id)   # 不把别的租户的历史方案当证据
    rows = session.execute(q.order_by(Ticket.id.desc()).limit(50)).all()
    out: list[dict] = []
    for ticket_id, cat in rows:
        if cat != category:
            continue
        msg = session.execute(
            select(Message.content, Message.source).where(
                Message.ticket_id == ticket_id,
                Message.sender == "agent",
            ).order_by(Message.id.desc()).limit(1)
        ).first()
        if msg and msg.content:
            out.append({"ticket_id": ticket_id, "content": msg.content, "source": msg.source})
        if len(out) >= limit:
            break
    return out