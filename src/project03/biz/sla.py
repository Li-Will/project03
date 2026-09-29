"""SLA（服务等级协议）：优先级 → 时限 → 升级调度。

设计（面试口径：客服运营向）：
- 时限：P1 ≤30min / P2 ≤4h / P3 ≤24h（建单受理时写入 sla_deadline，UTC naive）；
- 扫描（scan_sla，可注入 now 便于测试）：未终态工单
  - 已过 deadline → 合法迁移到 ESCALATED（in_triage/processing/pending_user 皆可转）+ 审计"SLA 超时升级"；
  - 临近 80% → 只写审计事件"SLA 临近提醒"（不迁移，给坐席缓冲）；
- 升级也是一次状态迁移，走状态机合法迁移表 —— SLA 与状态机合流。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from project03.biz.states import TicketState
from project03.biz.tickets import add_message
from project03.db.models import Ticket, utcnow
from project03.db import models as db

logger = logging.getLogger("project03.sla")

DEADLINES: dict[str, timedelta] = {
    "P1": timedelta(minutes=30),
    "P2": timedelta(hours=4),
    "P3": timedelta(hours=24),
}
WARN_RATIO = 0.8   # 时限 80% 时发临近提醒


def set_deadline(session, ticket: Ticket) -> Ticket:
    """受理节点调用：按当前 priority 写入死限（幂等）。"""
    ticket.sla_deadline = utcnow() + DEADLINES.get(ticket.priority, DEADLINES["P3"])
    session.flush()
    return ticket


def scan_sla(session, now: datetime | None = None, category: str | None = None,
             tenant_id: str | None = None) -> dict:
    """扫描【仍在处理中】的工单：超时 → 升级（ESCALATED+审计）；临近 → 提醒消息。

    active 状态：new / in_triage / processing / pending_user —— 已解决(resolved)不属于
    超时范围（已处理完，只待关闭），终态(closed/rejected)跳过。
    返回 {"escalated": [ticket_id...], "warned": [ticket_id...]}（测试断言用）。
    """
    now = now or utcnow()
    q = select(Ticket).where(Ticket.state.in_(["new", "in_triage", "processing", "pending_user"]))
    if category:
        q = q.where(Ticket.category == category)
    if tenant_id is not None:
        q = q.where(Ticket.tenant_id == tenant_id)
    rows = session.scalars(q).all()
    escalated: list[int] = []
    warned: list[int] = []
    for t in rows:
        if t.sla_deadline is None:
            continue
        if now >= t.sla_deadline:
            from project03.biz.tickets import update_state
            update_state(session, t.id, TicketState.ESCALATED, "system",
                         f"SLA 超时升级（deadline={t.sla_deadline.isoformat()}）")
            escalated.append(t.id)
        elif now >= t.sla_deadline - DEADLINES.get(t.priority, DEADLINES["P3"]) * (1 - WARN_RATIO):
            # 临近提醒：写系统消息（不伪造状态迁移，迁移留给超时升级）
            add_message(session, t.id, "system",
                        f"SLA 临近提醒：工单剩余处理时间不足 20%（deadline={t.sla_deadline.isoformat()}）", "sla")
            warned.append(t.id)
    return {"escalated": escalated, "warned": warned}


def list_escalations(session, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    """管理面队列：ESCALATED 工单（SLA 超时 + 转人工），按死限升序；可按租户过滤。"""
    q = select(Ticket).where(Ticket.state == "escalated")
    if tenant_id is not None:
        q = q.where(Ticket.tenant_id == tenant_id)
    rows = session.scalars(q.order_by(Ticket.sla_deadline.asc().nulls_last()).limit(limit)).all()
    return [
        {"ticket_id": t.id, "category": t.category, "priority": t.priority,
         "created_at": t.created_at.isoformat(),
         "sla_deadline": t.sla_deadline.isoformat() if t.sla_deadline else None}
        for t in rows
    ]


class SlaScheduler:
    """后台调度器：asyncio 任务，周期扫描（测试可注入 interval 与/或手动跑 scan）。"""

    def __init__(self, interval_seconds: int | None = None) -> None:
        self.interval = interval_seconds or getattr(db.get_settings(), "sla_scan_seconds", 300)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                with db.session_scope() as s:
                    result = scan_sla(s)
                if result["escalated"]:
                    logger.warning("SLA 扫描: %d 张工单超时升级", len(result["escalated"]))
            except Exception:  # noqa: BLE001 —— 调度器不能因单次异常退出
                logger.exception("SLA 扫描异常")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()