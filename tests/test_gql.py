"""LangGraph 工单图集成测试（fake 检索器 + 临时业务库 + Checkpoint 复用临时目录）。

Smart retry：graph.run_ticket_graph/resume 用全局 SqliteSaver（data 目录）；测试注入 monkeypatch
让 get_checkpointer 返回独立临时库，避免污染/被污染。
"""
from __future__ import annotations

import pytest
from langgraph.types import interrupt

from project03 import gql as gql_pkg
from project03.gql import graph as graph_mod
from project03.db import models as db


@pytest.fixture()
def graph_env(tmp_path, monkeypatch):
    """隔离环境：临时业务库（checkpoint 自动跟随 tmp）+ fake 检索器。"""
    db.configure(tmp_path / "biz.db")

    from project03.biz import faq
    hits_high = [
        {"id": "cd-01", "product": "clouddrive", "category": "容量与套餐",
         "question": "免费用户有多少存储容量？", "answer": "免费用户默认获得 5GB 云盘容量。", "score": 0.80},
    ]
    hits_low = [
        {"id": "cd-05", "product": "clouddrive", "category": "容量与套餐",
         "question": "如何查看已用容量？", "answer": "打开存储管理即可查看。", "score": 0.30},
    ]
    state = {"mode": "high"}
    def searcher(q, top_k=5, product=None):
        return hits_high if state["mode"] == "high" else hits_low
    monkeypatch.setattr(faq, "_searcher", searcher)
    return state


def _ticket_state(ticket_id: int) -> str:
    from project03.db.models import Ticket
    from sqlalchemy import select
    with db.session_scope() as s:
        t = s.scalar(select(Ticket).where(Ticket.id == ticket_id))
        return t.state


def test_resolve_high_confidence(graph_env):
    r = graph_mod.run_ticket_graph("免费空间多大？哦还有扩容怎么弄", "李总")
    res = r["result"]
    assert res["ticket_id"]
    assert res["escalate"] is False
    assert "5GB" in res["solution"]
    assert res["citations"][0]["id"] == "cd-01"
    assert res["diagnosis_steps"][0]["strategy"] == "faq_dense"
    assert _ticket_state(res["ticket_id"]) == "resolved"
    # 审计链覆盖受理与解决
    with db.session_scope() as s:
        from project03.biz.tickets import get_ticket_history
        hist = get_ticket_history(s, res["ticket_id"])
    states = [e["to"] for e in hist["events"]]
    assert "in_triage" in states and "resolved" in states
    assert hist["messages"][-1]["source"] == "diagnosis"


def test_escalate_pause_and_resume(graph_env):
    graph_env["mode"] = "low"
    r = graph_mod.run_ticket_graph("我的容量少了，到底咋回事", "李总")
    res = r["result"]
    assert res["escalate"] is True
    assert "置信度" in res["escalate_reason"]
    tid = res["ticket_id"]
    assert _ticket_state(tid) == "escalated"        # 中断已落库（不是只挂在图上）
    # 图应停在 interrupt 点（未跑到 apply_human/END）——状态仍 escaled 且无 human 消息
    with db.session_scope() as s:
        from project03.biz.tickets import get_ticket_history
        hist = get_ticket_history(s, tid)
    assert all(m["source"] != "human" for m in hist["messages"])

    # 人工回复 → resume
    g = graph_mod.resume_ticket_graph(r["thread_id"], "您好，我是人工小美，请提供订单号", "human")
    assert g["result"]["ticket_id"] == tid
    assert _ticket_state(tid) == "pending_user"
    with db.session_scope() as s:
        hist = get_ticket_history(s, tid)
    assert hist["messages"][-1]["source"] == "human"
    states = [e["to"] for e in hist["events"]]
    assert states == ["new", "in_triage", "processing", "escalated", "pending_user"]


def test_sla_deadline_set_on_triage(graph_env):
    r = graph_mod.run_ticket_graph("免费空间多大？", "张三")
    from project03.db.models import Ticket
    with db.session_scope() as s:
        t = s.get(Ticket, r["result"]["ticket_id"])
        assert t.sla_deadline is not None