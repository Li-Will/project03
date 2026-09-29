"""API e2e 单测：/chat 全分支 + 工单详情审计 + human-reply 接管 + SSE + ack + escalations。

隔离策略：db.configure(tmp_path) 换库（图 checkpoint 自动跟随 tmp）+ fake 检索器
（测试里同时喂高/低置信两套命中）。
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from project03.biz.tickets import get_ticket_history
from project03.db import models as db

HIGH_HIT = {"id": "cd-01", "product": "clouddrive", "category": "容量与套餐",
            "question": "免费用户有多少存储容量？", "answer": "免费用户默认获得 5GB 云盘容量。", "score": 0.80}
LOW_HIT = {"id": "cd-05", "product": "clouddrive", "category": "容量与套餐",
           "question": "如何查看已用容量？", "answer": "打开存储管理即可查看。", "score": 0.30}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.configure(tmp_path / "test.db")
    from project03.api.main import app
    from project03.biz import faq

    mode = {"value": "high"}
    def searcher(q, top_k=5, product=None):
        return [HIGH_HIT] if mode["value"] == "high" else [LOW_HIT]
    monkeypatch.setattr(faq, "_searcher", searcher)

    with TestClient(app) as c:
        c._mode = mode
        yield c


def _events(client, ticket_id: int) -> list[str]:
    return [e["to"] for e in client.get(f"/api/v1/tickets/{ticket_id}").json()["events"]]


# ---------- FAQ 直答 ----------

def test_faq_direct_reply(client):
    r = client.post("/api/v1/chat", json={"text": "免费空间多大？", "customer_name": "张三"})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "faq"
    assert "5GB" in body["reply"]
    assert body["citations"][0]["id"] == "cd-01"
    assert r.headers.get("X-Request-ID")


def test_low_confidence_faq_escalates(client):
    client._mode["value"] = "low"
    r = client.post("/api/v1/chat", json={"text": "我今天状态不太好，帮我看看"}).json()
    assert r["type"] == "escalated"
    assert "置信度" in client.get(f"/api/v1/tickets/{r['ticket_id']}").json()["events"][-1]["reason"]


# ---------- TICKET 走 LangGraph 图 ----------

def test_ticket_graph_resolved(client):
    r = client.post("/api/v1/chat", json={"text": "会议打不开了，一直报错卡死！", "customer_name": "张三"})
    body = r.json()
    assert body["type"] == "ticket"
    assert body["state"] == "resolved"          # 高置信 → 方案直接回复
    assert body["category"] == "会议支持"
    assert body["priority"] in ("P1", "P2", "P3")
    assert "5GB" in body["reply"]               # 方案来自诊断证据
    assert body["citations"]
    d = client.get(f"/api/v1/tickets/{body['ticket_id']}").json()
    assert _events(client, body["ticket_id"]) == ["new", "in_triage", "processing", "resolved"]
    assert d["messages"][-1]["source"] == "diagnosis"


def test_ticket_graph_escalate_then_resume(client):
    client._mode["value"] = "low"
    r = client.post("/api/v1/chat", json={"text": "我的云盘容量少了很多还报错，到底咋回事", "customer_name": "李总"}).json()
    assert r["type"] == "escalated"
    tid = r["ticket_id"]
    assert r["reason"].startswith("证据置信度")
    assert _events(client, tid)[-1] == "escalated"

    # 人工回复 → resume 图恢复（apply_human 落库 + PENDING_USER）
    rr = client.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "您好，我是人工小美，请提供账号。"})
    assert rr.status_code == 200
    assert rr.json()["state"] == "pending_user"
    states = _events(client, tid)
    assert states == ["new", "in_triage", "processing", "escalated", "pending_user"]
    d = client.get(f"/api/v1/tickets/{tid}").json()
    assert d["messages"][-1]["source"] == "human"


# ---------- 转人工（快速路径：投诉/要求人工）----------

def test_complaint_escalates(client):
    r = client.post("/api/v1/chat", json={"text": "云盘太垃圾了，我要投诉！", "customer_name": "张三"}).json()
    assert r["type"] == "escalated"
    d = client.get(f"/api/v1/tickets/{r['ticket_id']}").json()
    assert d["assignee"] == "human"
    assert "投诉" in d["events"][-1]["reason"]


def test_human_reply_fast_path(client):
    tid = client.post("/api/v1/chat", json={"text": "我要转人工！"}).json()["ticket_id"]
    r = client.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "您好，请问具体什么情况？"})
    assert r.json()["state"] == "pending_user"
    assert _events(client, tid) == ["new", "escalated", "pending_user"]


# ---------- 终态红线 / 404 ----------

def test_terminal_ticket_422(client):
    tid = client.post("/api/v1/chat", json={"text": "会议打不开，急！处理一下"}).json()["ticket_id"]
    with db.session_scope() as s:
        from project03.biz.states import TicketState
        from project03.biz.tickets import update_state
        for dst in (TicketState.PROCESSING, TicketState.RESOLVED, TicketState.CLOSED):
            update_state(s, tid, dst, "system", "t")
    r = client.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "再处理"})
    assert r.status_code == 422


def test_ticket_404(client):
    assert client.get("/api/v1/tickets/99999").status_code == 404


# ---------- SSE / ack / escalations ----------

def test_chat_stream_events(client):
    """SSE：TICKET 意图应依次产出 start/intent/node/done 事件。"""
    resp = client.get("/api/v1/chat/stream", params={"text": "会议打不开了，一直报错卡死！"})
    assert resp.status_code == 200
    body = resp.text
    assert body.startswith("retry: 3000")
    assert "event: start" in body
    assert "event: intent" in body
    assert 'intent": "ticket"' in body
    assert "event: done" in body
    assert '"type": "ticket"' in body


def test_ack_closes_ticket(client):
    tid = client.post("/api/v1/chat", json={"text": "云盘上传功能坏了，一直失败怎么处理"}).json()["ticket_id"]
    r = client.post(f"/api/v1/tickets/{tid}/ack", params={"rating": 5})
    assert r.status_code == 200
    assert r.json()["state"] == "closed"
    assert _events(client, tid)[-1] == "closed"


def test_admin_escalations(client):
    client._mode["value"] = "low"
    client.post("/api/v1/chat", json={"text": "云盘坏了报错不断，咋处理", "customer_name": "李总"})
    r = client.get("/api/v1/admin/escalations")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    for item in body["items"]:
        assert {"ticket_id", "category", "priority", "created_at"} <= set(item)  # 结构完整可序列化

# ---------- 脚本可直接调用（回归：P0-3 加鉴权后演示脚本崩过一次）----------

def test_impl_entrypoints_avoid_depends_defaults():
    """业务入口函数的参数默认值里不能出现 `Depends`。

    背景：`human_reply(...)` 加上 `principal: Principal = Depends(require_agent)` 后，
    **直接调用端点函数拿到的是 Depends 对象**（`'Depends' object has no attribute 'actor'`），
    `scripts/demo_cli.py` 当场崩——而 pytest 全绿（测试全走 HTTP，绕过了这条路）。
    纪律：端点保持薄壳（只做鉴权 + 转发），逻辑放 `*_impl`；本用例是这条纪律的结构护栏。
    """
    from fastapi.params import Depends as DependsClass

    from project03.api import main as api_main

    for name in ("handle_chat", "human_reply_impl", "ack_ticket_impl"):
        fn = getattr(api_main, name)
        for p in inspect.signature(fn).parameters.values():
            assert not isinstance(p.default, DependsClass), (
                f"{name} 的参数 {p.name} 用了 Depends，脚本无法直接调用它"
            )


def test_impl_functions_callable_directly(client):
    """不经 HTTP 真调一次：建单 → human_reply_impl（actor 显式传入）→ ack_ticket_impl。"""
    from project03.api.main import HumanReplyRequest, ack_ticket_impl, handle_chat, human_reply_impl

    r = handle_chat("会议打不开了，一直报错卡死！", "张三")
    tid = r["ticket_id"]
    out = human_reply_impl(tid, HumanReplyRequest(content="人工已处理，请重试。"), actor="坐席甲")
    assert out["ok"] is True and out["state"] in ("pending_user", "resolved")
    out2 = ack_ticket_impl(tid, rating=5)
    assert out2["ok"] is True and out2["state"] == "closed"
    with db.session_scope() as s:
        assert get_ticket_history(s, tid)["events"][-1]["actor"] == "customer"
