"""FAQ Answerer 单测：置信度三档决策（注入 fake 检索器，CI 无需 Qdrant）。"""
from __future__ import annotations

import pytest

from project03.biz import faq


def make_hit(score: float, faq_id: str = "cd-01", question: str = "免费用户有多少存储容量？") -> dict:
    return {
        "id": faq_id, "product": "clouddrive", "category": "容量与套餐",
        "question": question, "answer": "免费用户默认获得 5GB 云盘容量。",
        "score": score,
    }


@pytest.fixture(autouse=True)
def fake_searcher(monkeypatch):
    hits: list[dict] = []
    monkeypatch.setattr(faq, "_searcher", lambda q, top_k=5, product=None: hits)
    return hits


def test_high_confidence_direct(fake_searcher):
    fake_searcher.append(make_hit(0.72))
    r = faq.answer_faq("免费空间多大？")
    assert r.direct is True
    assert r.confidence == 0.72
    assert "5GB" in r.answer
    assert r.citations[0]["n"] == 1
    assert "转人工" not in r.answer  # 高置信不附人工提示


def test_mid_confidence_hint(fake_searcher):
    fake_searcher.append(make_hit(0.55))
    r = faq.answer_faq("空间不太够？")
    assert r.direct is True
    assert "人工" in r.answer  # 中置信附提示


def test_low_confidence_escalate(fake_searcher):
    fake_searcher.append(make_hit(0.42))
    r = faq.answer_faq("我今天状态不太好")
    assert r.direct is False
    assert r.answer == ""  # 低置信：不编答案


def test_no_hits_escalate(fake_searcher):
    r = faq.answer_faq("今天天气怎么样")
    assert r.direct is False
    assert r.confidence == 0.0


def test_citations_numbered_in_order(fake_searcher):
    fake_searcher.extend([make_hit(0.80, "cd-01"), make_hit(0.70, "cd-02", "如何扩容？")])
    r = faq.answer_faq("扩容怎么弄？")
    assert [c["n"] for c in r.citations] == [1, 2]


def test_detect_product():
    assert faq.detect_product("会议打不开") == "meetnow"
    assert faq.detect_product("网盘上传很慢") == "clouddrive"
    assert faq.detect_product("今天心情不错") is None