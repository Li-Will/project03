"""工作台读端点：业务规则的单一来源 + 坐席/运营视图数据。

为什么要有 `/api/v1/meta`：
    工作台要画状态机（当前状态 + 合法可达状态）、显示 SLA 时限与临近阈值、标出置信度阈值。
    **这些全都是业务规则**。让前端各抄一份 = 规则改两处、必然漂移 ——
    与 project01「科目注册表只认 config/subjects.yaml」是同一条纪律。
    所以后端把规则本身当作数据暴露：states.TRANSITIONS / sla.DEADLINES / tickets.CATEGORIES /
    intent.INTENTS / settings 阈值，前端只负责渲染，不负责定义。

鉴权分档（沿用 api/auth 的三档）：
    - `/meta`：纯静态元数据（不含任何业务数据）→ 不鉴权；
    - `/tickets`（列表）、`/admin/overview`、`/admin/me` → `require_staff`
      （agent / viewer 只读；写操作仍然只在 human-reply，且仅 agent）。

诚实边界：
    overview 是**当前库内、当前租户**的一次聚合，不是时间序列 —— 没有落库的指标采样就没有趋势，
    所以这里不画折线（要趋势得先有采样表，那是另一件事，不在这里假装有）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from project03 import __version__
from project03.api.auth import Principal, require_staff
from project03.biz import intent as intent_biz
from project03.biz import sla as sla_biz
from project03.biz.escalation import EVIDENCE_CONF_THRESHOLD
from project03.biz.states import TRANSITIONS, TicketState, all_states
from project03.biz.tickets import CATEGORIES
from project03.config import get_settings
from project03.db import models as db
from project03.db.models import Review, Ticket, utcnow

router = APIRouter(prefix="/api/v1", tags=["workbench"])

PRIORITIES: tuple[str, ...] = ("P1", "P2", "P3")

# 终态：没有任何合法迁移出去
TERMINAL_STATES: tuple[str, ...] = tuple(s.value for s in TicketState if not TRANSITIONS[s])

# 两个**不同**的口径，别混（混了就是"看板说没超时、队列里全是红的"）：
#   AUTO_SCAN_STATES —— 调度器 scan_sla 会扫描的状态（它决定"哪些单会被自动升级"）；
#   _NOT_TIMED       —— 不参与超时计时的状态（已处理完的 resolved、终态）。
# escalated **要算超时**：它是最需要人盯的一类，把它归成"不参与计时"等于告诉运营"一切正常"。
AUTO_SCAN_STATES: tuple[str, ...] = ("new", "in_triage", "processing", "pending_user")
_NOT_TIMED = ("resolved",)


# ---------- 纯函数（可直接单测，不必绕 HTTP） ----------

def meta_payload() -> dict[str, Any]:
    """业务元数据：状态机 / SLA / 阈值 / 枚举。前端据此渲染，不复制规则。"""
    st = get_settings()
    return {
        "version": __version__,
        "states": all_states(),
        "transitions": {s.value: sorted(d.value for d in TRANSITIONS[s]) for s in TicketState},
        "terminal_states": list(TERMINAL_STATES),
        "priorities": list(PRIORITIES),
        "categories": list(CATEGORIES),
        "intents": list(intent_biz.INTENTS),
        "sla_deadlines_seconds": {p: int(td.total_seconds()) for p, td in sla_biz.DEADLINES.items()},
        "sla_warn_ratio": sla_biz.WARN_RATIO,
        "conf_direct": st.conf_direct,
        "conf_hint": st.conf_hint,
        "evidence_conf_threshold": EVIDENCE_CONF_THRESHOLD,
        "auth_required": st.require_auth,
    }


def sla_bucket(ticket: Ticket, now: datetime) -> str:
    """单张工单的 SLA 状态。时限公式与 biz/sla.py::scan_sla 同源（都从 DEADLINES/WARN_RATIO 来）。

    overdue / warning / ok —— 非终态、非 resolved 的真实剩余时间
    no_deadline —— 没有死限（建单即计时的规则下只应出现在历史数据里）
    resolved    —— 已处理完，只待用户确认关闭，不参与超时计时
    terminal    —— closed / rejected（终态）
    """
    if ticket.state in TERMINAL_STATES:
        return "terminal"
    if ticket.state in _NOT_TIMED:
        return "resolved"
    if ticket.sla_deadline is None:
        return "no_deadline"
    if now >= ticket.sla_deadline:
        return "overdue"
    window = sla_biz.DEADLINES.get(ticket.priority, sla_biz.DEADLINES["P3"])
    if now >= ticket.sla_deadline - window * (1 - sla_biz.WARN_RATIO):
        return "warning"
    return "ok"


def ticket_row(t: Ticket) -> dict[str, Any]:
    """工单摘要行（列表/队列共用）。**不暴露 graph_thread_id**：它是内部编排标识，
    坐席需要知道的只有"这张单是否有一条挂起中的图"（has_graph）。"""
    return {
        "id": t.id,
        "intent": t.intent,
        "category": t.category,
        "priority": t.priority,
        "state": t.state,
        "assignee": t.assignee,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
        "sla_deadline": t.sla_deadline.isoformat() if t.sla_deadline else None,
        "has_graph": bool(t.graph_thread_id),
    }


def list_ticket_rows(session, tenant_id: str, states: list[str] | None = None,
                     priority: str | None = None, limit: int = 20, order: str = "created") -> list[Ticket]:
    """租户内工单列表（可按状态/优先级筛选）。状态值非法由端点层先行 400，此处不静默忽略。"""
    q = select(Ticket).where(Ticket.tenant_id == tenant_id)
    if states:
        q = q.where(Ticket.state.in_(states))
    if priority:
        q = q.where(Ticket.priority == priority)
    if order == "sla":
        q = q.order_by(Ticket.sla_deadline.asc().nulls_last())
    elif order == "updated":
        q = q.order_by(Ticket.updated_at.desc())
    else:
        q = q.order_by(Ticket.id.desc())
    return list(session.scalars(q.limit(limit)).all())


def build_overview(session, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    """租户内运营聚合：状态/优先级/品类分布 + SLA 分桶 + 评价分布 + 待办队列。"""
    now = now or utcnow()
    rows = list(session.scalars(select(Ticket).where(Ticket.tenant_id == tenant_id)).all())

    by_state = {s.value: 0 for s in TicketState}
    by_priority = {p: 0 for p in PRIORITIES}
    by_category = {c: 0 for c in CATEGORIES}
    sla = {k: 0 for k in ("overdue", "warning", "ok", "no_deadline", "resolved", "terminal")}
    auto_upgrade = 0
    for t in rows:
        by_state[t.state] = by_state.get(t.state, 0) + 1
        by_priority[t.priority] = by_priority.get(t.priority, 0) + 1
        by_category[t.category] = by_category.get(t.category, 0) + 1
        bucket = sla_bucket(t, now)
        sla[bucket] += 1
        # 调度器下一趟会真的动它们（escalated 已经不需要自动升级，但仍然是"超时"）
        if bucket == "overdue" and t.state in AUTO_SCAN_STATES:
            auto_upgrade += 1

    ratings = list(session.scalars(
        select(Review.rating).join(Ticket, Ticket.id == Review.ticket_id)
        .where(Ticket.tenant_id == tenant_id)
    ).all())
    by_rating = {str(i): 0 for i in range(1, 6)}
    for r in ratings:
        by_rating[str(r)] = by_rating.get(str(r), 0) + 1

    return {
        "tenant": tenant_id,
        "generated_at": now.isoformat(),
        "tickets": {
            "total": len(rows),
            "by_state": by_state,
            "by_priority": by_priority,
            "by_category": by_category,
        },
        "sla": sla,
        "reviews": {
            "count": len(ratings),
            "avg": round(sum(ratings) / len(ratings), 2) if ratings else None,
            "by_rating": by_rating,
        },
        "queue": {"escalated": by_state.get("escalated", 0)},
        "active_total": sum(by_state.get(s, 0) for s in AUTO_SCAN_STATES),
        "auto_upgrade_pending": auto_upgrade,
    }


# ---------- 端点 ----------

@router.get("/meta")
def meta() -> dict[str, Any]:
    """业务元数据（状态机 / SLA 时限与阈值 / 枚举）。工作台画图所需的一切都在这里。"""
    return meta_payload()


@router.get("/admin/me")
def whoami(principal: Principal = Depends(require_staff)) -> dict[str, Any]:
    """身份回显（坐席端点）：让工作台知道"你是谁、什么角色"，从而收敛可操作项。

    ⚠️ 前端据此隐藏按钮只是体验层 —— **权限永远由服务端裁决**（human-reply 仅 agent）。
    """
    st = get_settings()
    return {
        "tenant": principal.tenant,
        "actor": principal.actor,
        "role": principal.role,
        "key_id": principal.key_id,
        "auth_required": st.require_auth,
    }


@router.get("/tickets")
def tickets(state: str | None = Query(default=None, description="逗号分隔，如 escalated,processing"),
            priority: str | None = Query(default=None),
            limit: int = Query(default=20, ge=1, le=100),
            order: str = Query(default="created", pattern="^(created|updated|sla)$"),
            principal: Principal = Depends(require_staff)) -> dict[str, Any]:
    """坐席工单列表（租户内）。状态值非法 → 400（不静默返回空列表，避免"看板空着以为没单"）。"""
    states = [x.strip() for x in (state or "").split(",") if x.strip()]
    valid = {s.value for s in TicketState}
    bad = [x for x in states if x not in valid]
    if bad:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"未知状态: {bad}（合法值: {sorted(valid)}）")
    if priority and priority not in PRIORITIES:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"未知优先级: {priority}（合法值: {list(PRIORITIES)}）")

    with db.session_scope() as s:
        rows = list_ticket_rows(s, principal.tenant, states, priority, limit, order)
        return {"count": len(rows), "tenant": principal.tenant,
                "items": [ticket_row(t) for t in rows]}


@router.get("/admin/overview")
def overview(principal: Principal = Depends(require_staff)) -> dict[str, Any]:
    """运营聚合（租户内）：状态/优先级/品类分布、SLA 分桶、评价分布、待办队列长度。"""
    with db.session_scope() as s:
        return build_overview(s, principal.tenant)