"""P0-3 高危事件前置闸单测（入口层安全）。

回归的真实缺陷（2026-09-29 容器冒烟抓到）：
    「云盘里的照片好像被弄丢了，你们必须给个说法」经 /api/v1/chat 得到的是 FAQ 直答
    （检索到 0.69 分的无关文章），而不是转人工 —— 因为意图分类没把它判成 TICKET，
    而 P1 词表只在图内的 classify 节点里生效。评测集看不到这类问题（评测直接调图），
    但线上天天发生。见 biz/safety.py。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from project03.biz.safety import high_risk_reason
from project03.db import models as db

HIGH_HIT = {"id": "cd-27", "product": "clouddrive", "category": "账号与安全",
            "question": "如何保护隐私？", "answer": "请勿在公开场合分享账号信息。", "score": 0.69}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.configure(tmp_path / "safety.db")
    from project03.api.main import app
    from project03.biz import faq

    monkeypatch.setattr(faq, "_searcher", lambda q, top_k=5, product=None: [HIGH_HIT])
    with TestClient(app) as c:
        yield c


# ---------- 判定函数直测 ----------

@pytest.mark.parametrize("text", [
    "云盘里的照片好像被弄丢了，你们必须给个说法",
    "我的账号好像被盗了，快帮我处理",
    "你们的会员费多扣了一次，订单号 12345",
    "备份没了，里面有公司的资料，我要起诉你们",
])
def test_high_risk_hits(text):
    assert high_risk_reason(text)


@pytest.mark.parametrize("text", [
    "误删的文件还能找回来吗",
    "文件删除了，回收站在哪里，能找回几天内的？",
    "录制内容找不到了",                  # 口语"找不到了"是正常找回咨询，不能拦
    "怎么关闭自动续费",
    "登录失败怎么办",
    "免费用户有多少存储容量？",           # 常见 FAQ
    "",
])
def test_high_risk_no_false_positive(text):
    assert high_risk_reason(text) is None


# ---------- 入口层端点 ----------

def test_p1_event_escalates_instead_of_faq(client):
    """高分 FAQ 也不能替高危事件作答：必须转人工建单。"""
    r = client.post("/api/v1/chat", json={"text": "云盘里的照片好像被弄丢了，你们必须给个说法",
                                          "customer_name": "张三"})
    body = r.json()
    assert body["type"] == "escalated"            # 升级前这里是 "faq"
    detail = client.get(f"/api/v1/tickets/{body['ticket_id']}").json()
    assert detail["assignee"] == "human"
    assert "高危事件" in detail["events"][-1]["reason"]


def test_normal_faq_still_answered(client):
    """闸门不误伤：普通容量咨询仍然 FAQ 直答。"""
    body = client.post("/api/v1/chat", json={"text": "免费用户有多少存储容量？"}).json()
    assert body["type"] == "faq"
    assert body["citations"][0]["id"] == "cd-27"


def test_high_risk_status_requires_human(client):
    """高危建单后人工接管链路可用。"""
    tid = client.post("/api/v1/chat", json={"text": "账号被盗了，冻结一下"}).json()["ticket_id"]
    r = client.post(f"/api/v1/tickets/{tid}/human-reply", json={"content": "已为您冻结，请改密码。"})
    assert r.status_code == 200
    states = [e["to"] for e in client.get(f"/api/v1/tickets/{tid}").json()["events"]]
    assert states == ["new", "escalated", "pending_user"]