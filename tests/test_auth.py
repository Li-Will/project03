"""P0-3 鉴权与租户单测：坐席端点鉴权 / 身份来源 / 跨租户隔离 / 未启用鉴权的诚实降级。

覆盖的真实漏洞（升级前）：
- `POST /tickets/{id}/human-reply` 的 actor 来自请求体 → 任何人可冒充人工回消息；
- `GET /admin/escalations` 无鉴权；
- 无租户概念 → 能看到别家租户的工单。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from project03.api.auth import parse_api_keys
from project03.biz.tickets import get_ticket
from project03.config import get_settings
from project03.db import models as db

HIGH_HIT = {"id": "cd-01", "product": "clouddrive", "category": "容量与套餐",
            "question": "免费用户有多少存储容量？", "answer": "免费用户默认获得 5GB 云盘容量。", "score": 0.80}
LOW_HIT = {"id": "cd-05", "product": "clouddrive", "category": "容量与套餐",
           "question": "如何查看已用容量？", "answer": "打开存储管理即可查看。", "score": 0.30}

KEY_A = "sk-aaa-tenant-a"
KEY_B = "sk-bbb-tenant-b"
KEY_VIEWER = "sk-ccc-viewer"
API_KEYS = f"{KEY_A}:tenantA:坐席甲,{KEY_B}:tenantB:坐席乙,{KEY_VIEWER}:tenantA:观察者:viewer"


@pytest.fixture()
def strict(tmp_path, monkeypatch):
    """启用鉴权（REQUIRE_AUTH=true + API_KEYS）的隔离 client。"""
    monkeypatch.setenv("REQUIRE_AUTH", "true")
    monkeypatch.setenv("API_KEYS", API_KEYS)
    get_settings.cache_clear()
    db.configure(tmp_path / "auth.db")
    from project03.api.main import app
    from project03.biz import faq

    mode = {"value": "high"}

    def searcher(q, top_k=5, product=None):
        return [HIGH_HIT] if mode["value"] == "high" else [LOW_HIT]

    monkeypatch.setattr(faq, "_searcher", searcher)
    with TestClient(app) as c:
        c._mode = mode          # low → 图内 triage 转人工（走 interrupt/resume 路径）
        yield c
    get_settings.cache_clear()


def _open_ticket(client, api_key=None, text="我要转人工！", customer_name="张三") -> dict:
    headers = {"X-API-Key": api_key} if api_key else {}
    r = client.post("/api/v1/chat", json={"text": text, "customer_name": customer_name}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 配置解析 ----------

def test_parse_api_keys_roles_and_bad_format():
    keys = parse_api_keys(API_KEYS)
    assert keys[KEY_A].tenant == "tenantA" and keys[KEY_A].actor == "坐席甲"
    assert keys[KEY_A].role == "agent"          # 角色缺省 = agent
    assert keys[KEY_VIEWER].role == "viewer"
    assert keys[KEY_A].key_id == KEY_A[:6]      # 只留指纹，不落全量 key
    with pytest.raises(ValueError):
        parse_api_keys("sk-x:onlytwo")          # 配置写错 → 报错，不静默降级为无鉴权


# ---------- 坐席端点鉴权 ----------

def test_human_reply_requires_key(strict):
    tid = _open_ticket(strict, KEY_A)["ticket_id"]
    r = strict.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "您好"})
    assert r.status_code == 401


def test_human_reply_rejects_unknown_key(strict):
    tid = _open_ticket(strict, KEY_A)["ticket_id"]
    r = strict.post(f"/api/v1/tickets/{tid}/human-reply",
                    json={"content": "您好"}, headers={"X-API-Key": "sk-not-exist"})
    assert r.status_code == 401


def test_viewer_cannot_reply(strict):
    tid = _open_ticket(strict, KEY_A)["ticket_id"]
    r = strict.post(f"/api/v1/tickets/{tid}/human-reply",
                    json={"content": "您好"}, headers={"X-API-Key": KEY_VIEWER})
    assert r.status_code == 403


def test_chat_open_but_anonymous_stays_in_default_tenant(strict):
    """用户入口不强制鉴权：无 key 仍可用，但归默认租户、看不到 tenantA 的单。"""
    tid = _open_ticket(strict, KEY_A)["ticket_id"]
    anon = strict.post("/api/v1/chat", json={"text": "免费空间多大？"})
    assert anon.status_code == 200
    assert strict.get(f"/api/v1/tickets/{tid}").status_code == 404      # 匿名 = 默认租户
    assert strict.get(f"/api/v1/tickets/{tid}", headers={"X-API-Key": KEY_A}).status_code == 200


def test_admin_escalations_requires_key(strict):
    assert strict.get("/api/v1/admin/escalations").status_code == 401
    ok = strict.get("/api/v1/admin/escalations", headers={"X-API-Key": KEY_A})
    assert ok.status_code == 200 and ok.json()["tenant"] == "tenantA"


# ---------- 身份来源：凭据而非请求体 ----------

def test_actor_comes_from_key_not_body(strict):
    """冒充测试：body 里写 actor="root"，审计里必须记成 key 对应的坐席。"""
    tid = _open_ticket(strict, KEY_A)["ticket_id"]
    r = strict.post(f"/api/v1/tickets/{tid}/human-reply",
                    json={"content": "您好，我是人工。", "actor": "root"},
                    headers={"X-API-Key": KEY_A})
    assert r.status_code == 200
    events = strict.get(f"/api/v1/tickets/{tid}", headers={"X-API-Key": KEY_A}).json()["events"]
    assert events[-1]["actor"] == "坐席甲"
    assert all(e["actor"] != "root" for e in events)


def test_graph_resume_audits_key_actor(strict):
    """图 resume 路径（escalated + 挂起诊断）同样记凭据里的坐席名，不记 body。"""
    strict._mode["value"] = "low"     # 低置信 → 图内 triage 转人工
    tid = _open_ticket(strict, KEY_A, text="我的云盘容量少了很多还报错，到底咋回事")["ticket_id"]
    r = strict.post(f"/api/v1/tickets/{tid}/human-reply",
                    json={"content": "人工已处理", "actor": "root"},
                    headers={"X-API-Key": KEY_A})
    assert r.status_code == 200 and r.json()["state"] == "pending_user"
    events = strict.get(f"/api/v1/tickets/{tid}", headers={"X-API-Key": KEY_A}).json()["events"]
    assert events[-1]["to"] == "pending_user"
    assert events[-1]["actor"] == "坐席甲"


# ---------- 租户隔离 ----------

def test_ticket_isolated_between_tenants(strict):
    tid = _open_ticket(strict, KEY_A, customer_name="张三")["ticket_id"]
    with db.session_scope() as s:
        assert get_ticket(s, tid).tenant_id == "tenantA"     # 建单带上了租户

    # 同租户可读
    assert strict.get(f"/api/v1/tickets/{tid}", headers={"X-API-Key": KEY_A}).status_code == 200
    # 跨租户：404（不泄露"这单存在"）
    assert strict.get(f"/api/v1/tickets/{tid}", headers={"X-API-Key": KEY_B}).status_code == 404
    # 跨租户回复：404
    assert strict.post(f"/api/v1/tickets/{tid}/human-reply",
                       json={"content": "越权回复"}, headers={"X-API-Key": KEY_B}).status_code == 404
    # 跨租户 ack：404
    assert strict.post(f"/api/v1/tickets/{tid}/ack", headers={"X-API-Key": KEY_B}).status_code == 404


def test_customer_name_reusable_across_tenants(strict):
    """同名客户在不同租户是两条记录（客户名只在租户内唯一）。"""
    a = _open_ticket(strict, KEY_A, customer_name="张三")["ticket_id"]
    b = _open_ticket(strict, KEY_B, customer_name="张三")["ticket_id"]
    with db.session_scope() as s:
        ta, tb = get_ticket(s, a), get_ticket(s, b)
        assert ta.tenant_id == "tenantA" and tb.tenant_id == "tenantB"
        assert ta.customer_id != tb.customer_id


def test_admin_queue_scoped_by_tenant(strict):
    _open_ticket(strict, KEY_A, text="我要转人工！")
    a = strict.get("/api/v1/admin/escalations", headers={"X-API-Key": KEY_A}).json()
    b = strict.get("/api/v1/admin/escalations", headers={"X-API-Key": KEY_B}).json()
    assert a["count"] >= 1 and all(i["ticket_id"] for i in a["items"])
    assert b["count"] == 0                    # B 租户看不到 A 的队列


# ---------- 未启用鉴权：明确降级，不撒谎 ----------

def test_auth_disabled_uses_local_actor(tmp_path, monkeypatch):
    monkeypatch.delenv("REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("API_KEYS", raising=False)
    get_settings.cache_clear()
    db.configure(tmp_path / "open.db")
    from project03.api.main import app
    from project03.biz import faq

    monkeypatch.setattr(faq, "_searcher", lambda q, top_k=5, product=None: [HIGH_HIT])
    with TestClient(app) as c:
        tid = _open_ticket(c, text="我要转人工！")["ticket_id"]
        r = c.post(f"/api/v1/tickets/{tid}/human-reply",
                   json={"content": "本地演示回复", "actor": "冒充者"})
        assert r.status_code == 200
        detail = c.get(f"/api/v1/tickets/{tid}").json()
        assert detail["events"][-1]["actor"] == "local-agent"   # 不采信 body
        assert c.get("/api/v1/admin/escalations").status_code == 200
    get_settings.cache_clear()

# ---------- 老库迁移（无 tenant_id + name UNIQUE）----------

def test_legacy_db_migrates_to_multi_tenant(tmp_path):
    """老库经 init_db 后支持多租户：补列 + 放宽唯一约束 + 数据不丢。

    升级前真实故障：老库 customers.name 有 UNIQUE，给新租户建同名客户直接
    `UNIQUE constraint failed: customers.name`（500）。
    """
    from sqlalchemy import create_engine, text

    legacy = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{legacy}")
    with eng.begin() as c:
        c.execute(text("""CREATE TABLE customers (
            id INTEGER NOT NULL PRIMARY KEY,
            name VARCHAR(64) NOT NULL UNIQUE,
            tier VARCHAR(16),
            created_at DATETIME)"""))
        c.execute(text("INSERT INTO customers (id, name, tier, created_at) "
                       "VALUES (1, '老客户', 'VIP', '2026-01-01 00:00:00')"))
    eng.dispose()

    db.configure(legacy)
    db.init_db()
    with db.session_scope() as s:
        old = s.query(db.Customer).filter_by(name="老客户").one()
        assert old.tenant_id == "default" and old.tier == "VIP"    # 老数据保留 + 回填默认租户
        s.add(db.Customer(tenant_id="tenantX", name="老客户"))       # 同名跨租户不再撞
        s.flush()
