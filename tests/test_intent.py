"""意图路由单测：五类意图 + 优先级序 + 置信度档位。"""
from __future__ import annotations

import pytest

from project03.biz import intent


def test_need_human_priority():
    """用户明确要求人工 → 最高优先（即使文本还带了别的词）。"""
    r = intent.classify_intent("别用机器人了，给我转人工！")
    assert r.intent == intent.NEED_HUMAN
    assert r.confidence >= 0.9


def test_complaint():
    r = intent.classify_intent("你们的云盘太垃圾了，我要投诉退钱！")
    assert r.intent == intent.COMPLAINT
    assert r.triggers


def test_chat_only_when_no_question():
    assert intent.classify_intent("你好呀").intent == intent.CHAT
    # 带问题的寒暄不被吞
    assert intent.classify_intent("你好，怎么开票？").intent == intent.FAQ


def test_ticket_fault_words():
    r = intent.classify_intent("我的会议打不开了，一直报错卡死")
    assert r.intent == intent.TICKET
    assert r.confidence >= 0.8


def test_faq_fallback():
    """问句兜底 FAQ（无强触发词）。"""
    r = intent.classify_intent("免费用户有多少容量？")
    assert r.intent == intent.FAQ
    assert 0.5 <= r.confidence <= 0.7


def test_faq_never_hard_reject():
    """没有问句形态也不拒绝——让 FAQ 检索分数决定（宁转人工不误伤）。"""
    r = intent.classify_intent("看起来怪怪的")
    assert r.intent in (intent.FAQ, intent.TICKET)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("免费用户有多少容量？", intent.FAQ),
        ("转人工", intent.NEED_HUMAN),
        ("我要真人回答", intent.NEED_HUMAN),
        ("坏了好几天了也没人管", intent.TICKET),
        ("谢谢", intent.CHAT),
    ],
)
def test_parametrized(text, expected):
    assert intent.classify_intent(text).intent == expected