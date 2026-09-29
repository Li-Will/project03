"""规则版 Intent Router（M1 确定性版本；M2 起与 LLM 双模式对比）。

设计（面试口径）：
- 强触发词优先：need_human > complaint > chat > ticket（业务安全优先：宁可转人工不可答错）；
- 疑问句式（怎么办/如何/为什么/是不是）优先归 FAQ/工单，寒暄词不主导带问句的文本；
- confidence 按命中强度分档：无规则命中且非问句 → 低置信（0.50，交 FAQ 检索分数定生死）；
- 原则："低置信不许猜，返回 need_human" —— 该转不转是事故，宁高勿低。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 意图枚举（与 tickets.intent 字段值一致）
FAQ = "faq"
TICKET = "ticket"          # 报障/疑难 → 建单
COMPLAINT = "complaint"    # 投诉 → 建单并转人工
CHAT = "chat"              # 寒暄 → 模板话术
NEED_HUMAN = "need_human"  # 用户明确要求人工 / 低置信兜底

# 意图枚举的唯一来源：工作台的 /api/v1/meta 直接暴露本元组给前端（前端不另抄一份）
INTENTS: tuple[str, ...] = (FAQ, TICKET, COMPLAINT, CHAT, NEED_HUMAN)

# 强触发词表（按长度降序匹配避免短词误吞）
_NEED_HUMAN_KW = ("我要真人", "转人工", "人工介入", "人工处理", "人工回答", "接人工", "别用机器人", "真人来", "叫客服主管来")
_COMPLAINT_KW = ("投诉", "差评", "太差", "垃圾", "废物", "骗子", "坑人", "气死", "退钱", "赔钱", "讨个说法")
_CHAT_KW = ("你好", "您好", "你好呀", "hello", "hi", "在吗", "谢谢", "感谢", "再见", "拜拜", "辛苦了", "早上好", "下午好")
_TICKET_KW = ("坏了", "故障", "报错", "不工作", "闪退", "崩溃", "宕机", "卡死", "蓝屏", "打不开", "无法使用", "一直失败", "还没修好", "没人管", "处理一下", "紧急", "上传失败", "下载失败", "同步失败", "传输失败", "登录失败", "加载失败")

_QUESTION_PATTERN = re.compile(r"(怎么|如何|为什么|能不能|可以吗|能.*吗|是不是|可否|请问|多少|在哪|怎么弄)")


@dataclass
class IntentResult:
    intent: str
    confidence: float
    triggers: list[str] = field(default_factory=list)


def _hit(text: str, kws: tuple[str, ...]) -> list[str]:
    """返回命中的关键词（不重复）。"""
    return [k for k in kws if k in text]


def _has_question_form(text: str) -> bool:
    return bool(_QUESTION_PATTERN.search(text))


def classify_intent(text: str) -> IntentResult:
    """规则意图分类：返回 intent + confidence + 命中触发词。"""
    t = (text or "").strip().lower()
    if not t:
        return IntentResult(FAQ, 0.40, [])

    # 1) 用户明确要求人工 —— 最高优先（"人工客服电话"这类 FAQ 问法不在此列）
    hit = _hit(t, _NEED_HUMAN_KW)
    if hit:
        return IntentResult(NEED_HUMAN, 0.95, hit)

    # 2) 投诉：带强烈否定语义的词
    hit = _hit(t, _COMPLAINT_KW)
    if hit:
        return IntentResult(COMPLAINT, 0.90, hit)

    # 3) 纯寒暄（无问题形态才算，避免"谢谢，怎么开票"被吞）
    hit = _hit(t, _CHAT_KW)
    if hit and not _has_question_form(t) and len(t) <= 20:
        return IntentResult(CHAT, 0.85, hit)

    # 4) 报障：故障词汇（含疑问句式的"坏了怎么修"也算工单而非 FAQ——需要人工+诊断）
    hit = _hit(t, _TICKET_KW)
    if hit:
        return IntentResult(TICKET, 0.80, hit)

    # 5) 兜底 FAQ（置信度留给 FAQ 检索分数二次判定；无问句形态且无触发 → 还是 FAQ，
    #    由 answer 的检索低分会转人工兜底，不在此处硬拒）
    if _has_question_form(t):
        return IntentResult(FAQ, 0.65, [])
    return IntentResult(FAQ, 0.50, [])


def escalate_reason(intent: str, triggers: list[str]) -> str:
    """转人工/建单的审计 reason 文案（写进 ticket_events）。"""
    if intent == NEED_HUMAN:
        return f"用户要求人工: {triggers[0] if triggers else '请求人工'}"
    if intent == COMPLAINT:
        return f"投诉转人工: {triggers[0] if triggers else '投诉'}"
    return f"意图: {intent}"