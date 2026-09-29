"""工作台读端点单测：/meta 契约护栏 + 工单列表筛选与租户隔离 + 运营聚合 + 身份回显。

重点在 `/meta` 的**契约护栏**：
    工作台把状态机、SLA 时限、置信度阈值画在界面上。后端一旦改了迁移表或时限，
    而 meta 端点没跟着变，**界面就会对用户说谎** —— 所以这里逐项断言 meta 是从 biz 层
    真实派生的，而不是另抄的一份常量（前端不复制业务规则的前提，就是这个端点忠实）。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from project03.biz import intent as intent_biz
from project03.biz import sla as sla_biz
from project03.biz.states import TRANSITIONS, TicketState, all_states
from project03.biz.tickets import CATEGORIES
from project03.config import get_settings
from project03.db import models as db
from project03.db.models import Customer, Review, Ticket, TicketEvent, utcnow

HIGH_HIT = {"id": "cd-01", "product": "clouddrive", "category": "容量与套餐",
            "question": "免费用户有多少存储容量？", "answer": "免费用户默认获得 5GB 云盘容量。", "score": 0.80}

# 三把互不相同的测试 key（必须是不同字符串 —— 相同则解析成同一个 Principal，
# "viewer 不能写 / agent 能写"这类断言会退化成只测到最后一个 key 的角色）
KEY_A = "sk-wb-tenantA-agent"
KEY_B = "sk-wb-tenantB-agent"
KEY_VIEWER = "sk-wb-tenantA-viewer"
API_KEYS = f"{KEY_A}:tenantA:坐席甲,{KEY_B}:tenantB:坐席乙,{KEY_VIEWER}:tenantA:观察者:viewer"


def _client(tmp_path, monkeypatch, *, strict_auth: bool) -> TestClient:
    if strict_auth:
        monkeypatch.setenv("REQUIRE_AUTH", "true")
        monkeypatch.setenv("API_KEYS", API_KEYS)
    get_settings.cache_clear()
    db.configure(tmp_path / ("auth.db" if strict_auth else "wb.db"))
    from project03.api.main import app
    from project03.biz import faq
    monkeypatch.setattr(faq, "_searcher", lambda q, top_k=5, product=None: [HIGH_HIT])
    return TestClient(app)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, strict_auth=False) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def strict(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, strict_auth=True) as c:
        yield c
    get_settings.cache_clear()


def _ticket(state: str = "new", priority: str = "P3", *, deadline_seconds: float | None = None,
            tenant: str = "default", category: str = "云盘服务", rating: int | None = None) -> int:
    """直接写库造前提（绕过状态机是**有意**的：测试要的是"库里已经是这个状态"，
    状态机的合法性由 tests/test_states.py 单独覆盖）。"""
    with db.session_scope() as s:
        cust = Customer(tenant_id=tenant, name=f"c-{uuid.uuid4().hex[:8]}", tier="普通")
        s.add(cust)
        s.flush()
        t = Ticket(tenant_id=tenant, customer_id=cust.id, intent="ticket",
                   category=category, priority=priority, state=state)
        if deadline_seconds is not None:
            t.sla_deadline = utcnow() + timedelta(seconds=deadline_seconds)
        s.add(t)
        s.flush()
        s.add(TicketEvent(ticket_id=t.id, from_state=None, to_state=state, actor="system", reason="测试造数"))
        if rating is not None:
            s.add(Review(ticket_id=t.id, rating=rating, comment=""))
        return t.id


# ---------- /meta：契约护栏（前端不复制业务规则的前提） ----------

def test_meta_matches_business_layer(client):
    m = client.get("/api/v1/meta").json()
    assert m["states"] == all_states()
    assert m["transitions"] == {s.value: sorted(d.value for d in TRANSITIONS[s]) for s in TicketState}
    assert m["terminal_states"] == [s.value for s in TicketState if not TRANSITIONS[s]]
    assert m["sla_deadlines_seconds"] == {p: int(td.total_seconds()) for p, td in sla_biz.DEADLINES.items()}
    assert m["sla_warn_ratio"] == sla_biz.WARN_RATIO
    assert m["categories"] == list(CATEGORIES)
    assert m["intents"] == list(intent_biz.INTENTS)
    st = get_settings()
    assert (m["conf_direct"], m["conf_hint"]) == (st.conf_direct, st.conf_hint)
    assert m["auth_required"] is st.require_auth


def test_meta_needs_no_auth_and_has_no_business_data(client):
    r = client.get("/api/v1/meta")
    assert r.status_code == 200
    body = r.json()
    assert "tickets" not in body and "tenant" not in body   # 纯规则元数据，不含任何业务数据


# ---------- 工单列表 ----------

def test_tickets_list_sorted_and_filtered(client):
    for _ in range(3):
        _ticket("escalated", "P1")
    _ticket("closed", "P3")
    body = client.get("/api/v1/tickets").json()
    assert body["count"] == 4
    assert [i["id"] for i in body["items"]] == sorted([i["id"] for i in body["items"]], reverse=True)

    esc = client.get("/api/v1/tickets?state=escalated").json()
    assert esc["count"] == 3 and {i["state"] for i in esc["items"]} == {"escalated"}

    multi = client.get("/api/v1/tickets?state=escalated,closed").json()
    assert multi["count"] == 4

    p1 = client.get("/api/v1/tickets?priority=P1").json()
    assert p1["count"] == 3

    assert client.get("/api/v1/tickets?limit=2").json()["count"] == 2


def test_tickets_list_rejects_unknown_state_and_priority(client):
    r = client.get("/api/v1/tickets?state=done")
    assert r.status_code == 400 and "未知状态" in r.json()["detail"]
    assert client.get("/api/v1/tickets?priority=P9").status_code == 400


def test_tickets_list_requires_staff_key(strict):
    assert strict.get("/api/v1/tickets").status_code == 401          # 无 key
    assert strict.get("/api/v1/tickets", headers={"X-API-Key": "sk-nope"}).status_code == 401
    ok = strict.get("/api/v1/tickets", headers={"X-API-Key": KEY_VIEWER})
    assert ok.status_code == 200 and ok.json()["tenant"] == "tenantA"   # viewer 只读端点可读


def test_tickets_list_tenant_isolation(strict):
    a = _ticket("escalated", "P1", tenant="tenantA")
    b = _ticket("escalated", "P1", tenant="tenantB")
    _ticket("escalated", "P1", tenant="default")
    ids_a = {i["id"] for i in strict.get("/api/v1/tickets", headers={"X-API-Key": KEY_A}).json()["items"]}
    ids_b = {i["id"] for i in strict.get("/api/v1/tickets", headers={"X-API-Key": KEY_B}).json()["items"]}
    assert ids_a == {a} and ids_b == {b}          # 各看各的，互不串号


# ---------- 运营聚合 ----------

def test_overview_buckets_and_rating(client):
    _ticket("new", "P1", deadline_seconds=1000)          # P1 窗口 1800s，剩 1000 > 360 → ok
    _ticket("processing", "P1", deadline_seconds=200)    # 剩 200 < 360 → warning（尚未超时）
    _ticket("in_triage", "P1", deadline_seconds=-10)     # 已过期 → overdue
    _ticket("escalated", "P1", deadline_seconds=-600)    # 已转人工且超时 → **也算 overdue**
    _ticket("new", "P3", deadline_seconds=None)          # 没有死限（历史数据形态）
    _ticket("resolved", "P3", deadline_seconds=60)       # 已处理完待关闭 → resolved（不参与计时）
    _ticket("closed", "P3", deadline_seconds=-9999)      # 终态 → terminal
    _ticket("processing", "P2", deadline_seconds=9999, rating=5)
    _ticket("processing", "P2", deadline_seconds=9999, rating=4)

    o = client.get("/api/v1/admin/overview").json()
    assert o["tickets"]["total"] == 9
    assert o["sla"] == {"ok": 3, "warning": 1, "overdue": 2, "no_deadline": 1,
                        "resolved": 1, "terminal": 1}
    # 调度器下一趟只会动 in_triage 那张：escalated 已经不需要自动升级，但它仍然"超时"
    assert o["auto_upgrade_pending"] == 1
    assert o["tickets"]["by_state"]["processing"] == 3
    assert o["tickets"]["by_priority"] == {"P1": 4, "P2": 2, "P3": 3}
    assert o["tickets"]["by_category"]["云盘服务"] == 9
    assert o["reviews"] == {"count": 2, "avg": 4.5, "by_rating": {"1": 0, "2": 0, "3": 0, "4": 1, "5": 1}}
    assert o["queue"]["escalated"] == 1
    assert o["active_total"] == 6           # new/in_triage/processing/pending_user 之和
    assert o["generated_at"]


def test_overview_is_tenant_scoped_and_needs_key(strict):
    _ticket("escalated", "P1", tenant="tenantA")
    _ticket("escalated", "P1", tenant="tenantB")
    assert strict.get("/api/v1/admin/overview").status_code == 401
    a = strict.get("/api/v1/admin/overview", headers={"X-API-Key": KEY_A}).json()
    assert a["tenant"] == "tenantA" and a["tickets"]["total"] == 1 and a["queue"]["escalated"] == 1


def test_every_new_ticket_starts_its_sla_clock(client):
    """建单即计时 —— 包括"快速转人工"这类不经过图的路径。

    这是一处真实的缺口：死限此前只在图内受理节点写，而投诉/低置信/高危事件走的是
    `_escalate_fast`（不经过图）→ 这些单 `sla_deadline` 永远为 NULL，
    被 `scan_sla` 的 `if t.sla_deadline is None: continue` 跳过 ——
    **最该被 SLA 兜住的单，反而永远不会超时升级**。
    """
    for text in ("我要转人工", "云盘里的照片好像被弄丢了，你们必须给个说法"):
        r = client.post("/api/v1/chat", json={"text": text, "customer_name": "甲"}).json()
        assert r["type"] == "escalated"
        d = client.get(f"/api/v1/tickets/{r['ticket_id']}").json()
        assert d["sla_deadline"] is not None, f"{text} 建单后没有死限"
        assert d["priority"] in ("P1", "P2", "P3")


def test_sla_bucket_treats_escalated_as_overdue_not_out_of_scope(client):
    """escalated 超时必须算超时 —— 归成"不参与计时"等于告诉运营一切正常。"""
    _ticket("escalated", "P1", deadline_seconds=-1)
    o = client.get("/api/v1/admin/overview").json()
    assert o["sla"]["overdue"] == 1
    assert o["sla"]["resolved"] == 0
    assert o["auto_upgrade_pending"] == 0     # escalated 不再需要调度器动它


# ---------- 身份回显 ----------

def test_admin_me_without_auth_returns_local_agent(client):
    me = client.get("/api/v1/admin/me").json()
    assert me["actor"] == "local-agent" and me["tenant"] == "default" and me["role"] == "agent"
    assert me["auth_required"] is False


def test_admin_me_echoes_key_and_viewer_can_read(strict):
    a = strict.get("/api/v1/admin/me", headers={"X-API-Key": KEY_A}).json()
    assert (a["tenant"], a["actor"], a["role"]) == ("tenantA", "坐席甲", "agent")
    assert a["key_id"] == KEY_A[:6] and a["auth_required"] is True
    v = strict.get("/api/v1/admin/me", headers={"X-API-Key": KEY_VIEWER}).json()
    assert v["role"] == "viewer"
    assert strict.get("/api/v1/admin/me").status_code == 401


def test_workbench_write_endpoint_still_agent_only(strict):
    """工作台加了只读端点，**不该顺手放宽写端点**：human-reply 仍然只有 agent 能调。"""
    tid = _ticket("escalated", "P1", tenant="tenantA")
    assert strict.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "x"},
                       headers={"X-API-Key": KEY_VIEWER}).status_code == 403
    assert strict.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "x"},
                       headers={"X-API-Key": KEY_A}).status_code == 200