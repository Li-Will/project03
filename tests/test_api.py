"""API e2e 单测：/chat 全分支 + 工单详情审计 + human-reply 接管（临时 SQLite + fake 检索）。

隔离策略：db.configure(tmp_path) 换库 + monkeypatch faq._searcher —— 不依赖 Qdrant/真实索引。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from project03.db import models as db
    db.configure(tmp_path / "test.db")  # 临时库（建表+种子）
    from project03.api.main import app
    from project03.biz import faq
    monkeypatch.setattr(
        faq, "_searcher",
        lambda q, top_k=5, product=None: [
            {"id": "cd-01", "product": "clouddrive", "category": "容量与套餐",
             "question": "免费用户有多少存储容量？", "answer": "免费用户默认获得 5GB 云盘容量。",
             "score": 0.80},
        ],
    )
    with TestClient(app) as c:
        yield c


def test_faq_direct_reply(client):
    r = client.post("/api/v1/chat", json={"text": "免费空间多大？", "customer_name": "张三"})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "faq"
    assert body["intent"] == "faq"
    assert "5GB" in body["reply"]
    assert body["citations"][0]["id"] == "cd-01"
    assert r.headers.get("X-Request-ID")  # 全链路可追踪


def test_ticket_created_with_audit(client):
    r = client.post("/api/v1/chat", json={"text": "会议打不开了，一直报错卡死，很急！", "customer_name": "张三"})
    body = r.json()
    assert body["type"] == "ticket"
    assert body["priority"] in ("P1", "P2", "P3")
    assert body["category"] == "会议支持"

    # 工单历史：含消息 + 审计事件（new→in_triage）
    d = client.get(f"/api/v1/tickets/{body['ticket_id']}").json()
    assert d["state"] == "in_triage"
    assert d["messages"][0]["sender"] == "customer"
    assert d["events"][0]["to"] == "new"
    assert d["events"][-1]["to"] == "in_triage"


def test_complaint_escalates(client):
    r = client.post("/api/v1/chat", json={"text": "云盘太垃圾了，我要投诉！", "customer_name": "张三"}).json()
    assert r["type"] == "escalated"
    assert r["state"] == "escalated"
    d = client.get(f"/api/v1/tickets/{r['ticket_id']}").json()
    assert d["assignee"] == "human"   # 转人工即指派给人类坐席
    assert d["events"][-1]["to"] == "escalated"
    assert "投诉" in d["events"][-1]["reason"]


def test_low_confidence_faq_escalates(client, monkeypatch):
    # 把检索分数降到低置信 → FAQ 分支应转人工而非硬答
    from project03.api import main as api_main
    from project03.biz import faq
    monkeypatch.setattr(
        faq, "_searcher",
        lambda q, top_k=5, product=None: [{
            "id": "cd-05", "product": "clouddrive", "category": "容量",
            "question": "如何查看已用容量？", "answer": "打开存储管理即可查看。", "score": 0.30,
        }],
    )
    r = client.post("/api/v1/chat", json={"text": "我今天状态不太好，帮我看看"}).json()
    assert r["type"] == "escalated"
    assert "置信度" in client.get(f"/api/v1/tickets/{r['ticket_id']}").json()["events"][-1]["reason"]


def test_human_reply_resumes_ticket(client):
    tid = client.post("/api/v1/chat", json={"text": "我要转人工！"}).json()["ticket_id"]
    r = client.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "您好，我是人工客服小美，请问具体什么情况？"})
    assert r.status_code == 200
    assert r.json()["state"] == "pending_user"
    d = client.get(f"/api/v1/tickets/{tid}").json()
    assert d["messages"][-1]["source"] == "human"
    assert d["messages"][-1]["sender"] == "agent"
    # 审计链：new → escalated → pending_user
    states = [e["to"] for e in d["events"]]
    assert states == ["new", "escalated", "pending_user"]


def test_illegal_state_transition_422(client):
    """终态（closed）工单不允许再人工回复 → 422（状态机红线）。"""
    tid = client.post("/api/v1/chat", json={"text": "上传文件坏了，处理一下！"}).json()["ticket_id"]
    from project03.biz.states import TicketState
    from project03.db import models as db
    from project03.biz.tickets import update_state
    with db.session_scope() as s:
        # 合法链推进到终态（工单已是 in_triage：→processing→resolved→closed）
        for dst, reason in [
            (TicketState.PROCESSING, "t"),
            (TicketState.RESOLVED, "t"),
            (TicketState.CLOSED, "测试关闭"),
        ]:
            update_state(s, tid, dst, "system", reason)
    # 已关闭工单再回复 → 422
    r = client.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "再处理一下"})
    assert r.status_code == 422


def test_ticket_404(client):
    assert client.get("/api/v1/tickets/99999").status_code == 404