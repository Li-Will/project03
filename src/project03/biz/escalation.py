"""Escalation Triage：转人工决策（校验层，规则版）。

设计（面试口径：客服 AI 的命门 = "该转不转"）：
- 判定输入 = 诊断证据质量 + 优先级 + 语义信号，输出 escalate=True/False + 可审计 reason；
- 四路信号（任一命中即转）：证据空 / top1 置信度低于阈值 / P1 事件 / 用户明确要求；
- 这是规则层，不依赖 LLM —— 转人工决策必须确定、可审计、可回归（M3 评测集验证）。
"""
from __future__ import annotations

from dataclasses import dataclass

EVIDENCE_CONF_THRESHOLD = 0.50   # top1 证据低于此分 → 该转人工（与 FAQ 直答阈值一致口径）


@dataclass
class TriageDecision:
    escalate: bool
    reason: str = ""


def triage(
    *,
    evidence_top_score: float | None,
    priority: str,
    text: str = "",
    has_evidence: bool | None = None,
) -> TriageDecision:
    """规则转人工判定。evidence_top_score=None 表示无任何证据。"""
    conf = evidence_top_score
    no_evidence = has_evidence if has_evidence is not None else (conf is None)

    if no_evidence:
        return TriageDecision(True, "诊断无任何证据，宁转人工不编造")
    if conf is not None and conf < EVIDENCE_CONF_THRESHOLD:
        return TriageDecision(True, f"证据置信度 {conf:.3f} 低于阈值 {EVIDENCE_CONF_THRESHOLD}")
    if priority == "P1":
        return TriageDecision(True, "P1 事件（资金/账号/数据安全）需人工介入确认")
    if any(k in text for k in ("人工", "真人", "客服来", "负责人", "说不清楚", "不知道怎么说")):
        return TriageDecision(True, "用户文本出现人工诉求/表达模糊信号")
    return TriageDecision(False, "证据充足，可生成方案直接回复")