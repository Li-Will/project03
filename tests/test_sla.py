"""SLA 调度器单测：deadline 写入 / 超时升级（迁移+审计）/ 临近提醒（消息）/ 管理队列。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from project03.biz.sla import DEADLINES, list_escalations, scan_sla, set_deadline
from project03.biz.states import TicketState
from project03.biz.tickets import create_ticket, update_state
from project03.db import models as db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """每个 SLA 测试独立临时库（避免污染真实 data/tickets.db）。"""
    db.configure(tmp_path / "sla_test.db")
    yield


def _make_active_ticket(session, priority="P3", category="云盘服务", text="test") -> int:
    from project03.biz.tickets import get_or_create_customer
    cust = get_or_create_customer(session, "SLA测试用户")
    t = create_ticket(session, cust.id, text, "ticket", category=category, priority=priority)
    set_deadline(session, t)
    update_state(session, t.id, TicketState.IN_TRIAGE, "system", "t")
    return t.id


def test_deadline_per_priority():
    with db.session_scope() as s:
        t1 = _make_active_ticket(s, "P1")
        t3 = _make_active_ticket(s, "P3")
        from project03.db.models import Ticket
        a = s.get(Ticket, t1).sla_deadline
        b = s.get(Ticket, t3).sla_deadline
        assert (b - a) >= (DEADLINES["P3"] - DEADLINES["P1"]) - timedelta(seconds=1)


def test_scan_escalates_on_deadline():
    with db.session_scope() as s:
        t1 = _make_active_ticket(s, "P1")
        result = scan_sla(s, now=datetime.now() + timedelta(minutes=35))   # 已过 30min
        assert t1 in result["escalated"]
        from project03.db.models import Ticket
        assert s.get(Ticket, t1).state == "escalated"
        # 审计带超时原因
        from project03.biz.tickets import get_ticket_history
        assert get_ticket_history(s, t1)["events"][-1]["reason"].startswith("SLA 超时")


def test_scan_warns_before_deadline():
    with db.session_scope() as s:
        t3 = _make_active_ticket(s, "P3")
        # 统一 UTC naive 时钟（set_deadline 内部用 db.utcnow()，测试不得混用本地时区）
        result = scan_sla(s, now=db.utcnow() + DEADLINES["P3"] * 0.9)   # 90% 时限，未过期
        assert t3 not in result["escalated"]
        assert t3 in result["warned"]
        from project03.db.models import Ticket
        from project03.biz.tickets import get_ticket_history, get_ticket
        assert get_ticket(s, t3).state == "in_triage"                     # 只提醒不迁移
        assert any(m["source"] == "sla" for m in get_ticket_history(s, t3)["messages"])


def test_scan_skips_terminal():
    with db.session_scope() as s:
        tid = _make_active_ticket(s, "P1")
        for dst, reason in [
            (TicketState.PROCESSING, "t"), (TicketState.RESOLVED, "t"),
            (TicketState.CLOSED, "t"),  # 合法链推进到终态
        ]:
            update_state(s, tid, dst, "system", reason)
        result = scan_sla(s, now=db.utcnow() + timedelta(hours=2))
        assert tid not in result["escalated"] and tid not in result["warned"]


def test_list_escalations_queue():
    with db.session_scope() as s:
        tid = _make_active_ticket(s, "P2")
        update_state(s, tid, TicketState.ESCALATED, "system", "t")
        q = list_escalations(s)
        assert any(x["ticket_id"] == tid for x in q)
        assert q[0]["priority"]  # 结构完整