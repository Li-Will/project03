"""前端托管契约单测：壳页语义 / 静态产物 / 404 不撒谎 / 降级页。

三条被断言到底的纪律：
  1) **产物缺失 → 200 构建指引页**，不是 500（"没构建前端"不该看起来像"服务坏了"）；
  2) **未匹配路径是 404**（不用 StaticFiles(html=True)：它会把任意路径渲染成 index.html）
     —— 与"跨租户一律 404"同源：界面的语义不能比 API 更宽松；
  3) **/assets 路径穿越一律 404**。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from project03.config import get_settings
from project03.db import models as db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    get_settings.cache_clear()
    db.configure(tmp_path / "web.db")
    from project03.api.main import app
    from project03.biz import faq
    monkeypatch.setattr(faq, "_searcher", lambda q, top_k=5, product=None: [])
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def built(tmp_path, monkeypatch):
    """造一个"已构建"的假产物目录，并把它接到 main.WEB_DIR 上。"""
    from project03.api import main as main_mod
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(
        '<!doctype html><html><body><div id="app"></div>'
        '<script type="module" src="/assets/index-B2qi8ukV.js"></script></body></html>',
        encoding="utf-8",
    )
    (d / "assets" / "index-B2qi8ukV.js").write_text("console.log(1)", encoding="utf-8")
    (d / "assets" / "index-SzemFVjV.css").write_text("body{}", encoding="utf-8")
    (d / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setattr(main_mod, "WEB_DIR", d)
    return d


# ---------- 壳页 ----------

def test_root_is_200_html_even_without_build(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    if "尚未构建" in r.text:
        assert "npm run build" in r.text          # 降级页必须给出可执行的下一步
        assert "/docs" in r.text                  # 并说清 API 仍然可用


def test_root_serves_built_shell(client, built):
    r = client.get("/")
    assert r.status_code == 200
    assert "/assets/index-B2qi8ukV.js" in r.text


def test_root_reflects_disk_content(client, built):
    """壳页必须与磁盘逐字节一致（不能缓存旧壳，否则升级后界面回退）。"""
    r = client.get("/")
    assert r.text == (built / "index.html").read_text(encoding="utf-8")


# ---------- 静态产物 ----------

def test_assets_served_with_immutable_cache_when_hashed(client, built):
    r = client.get("/assets/index-B2qi8ukV.js")
    assert r.status_code == 200 and r.text == "console.log(1)"
    assert "immutable" in r.headers.get("cache-control", "")


def test_assets_missing_is_404(client, built):
    assert client.get("/assets/index-nope.js").status_code == 404


@pytest.mark.parametrize("path", [
    "/assets/../../src/project03/config.py",
    "/assets/%2e%2e/%2e%2e/src/project03/config.py",
    "/assets/../index.html",
])
def test_assets_traversal_is_404(client, built, path):
    assert client.get(path).status_code == 404


# ---------- 404 语义不被吞 ----------

@pytest.mark.parametrize("path", ["/definitely-not-a-route", "/api/v1/nope", "/statc/typo.css"])
def test_unknown_paths_are_404(client, built, path):
    assert client.get(path).status_code == 404


def test_api_and_legacy_demo_page_still_reachable(client, built):
    assert client.get("/api/v1/meta").status_code == 200
    assert client.get("/api/v1/health/live").status_code == 200
    # 旧的单页演示（M4）保留在 /static/demo.html，不因为新工作台而消失
    r = client.get("/static/demo.html")
    assert r.status_code == 200 and "Ticket Agent" in r.text


# ---------- 详情端点契约（工作台画 SLA 条要用） ----------

def test_ticket_detail_exposes_sla_deadline(client):
    body = client.post("/api/v1/chat", json={"text": "我要转人工！", "customer_name": "甲"}).json()
    d = client.get(f"/api/v1/tickets/{body['ticket_id']}").json()
    assert "sla_deadline" in d and d["sla_deadline"] is not None   # 转人工路径已设死限
    assert set(d.keys()) >= {"id", "state", "priority", "messages", "events", "sla_deadline"}