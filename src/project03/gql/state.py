"""LangGraph 图状态（GraphState）——工单 Agent 编排层的运行状态。

与 biz/states.py（业务状态机）职责分离：
- GraphState：LangGraph 运行时数据（证据/诊断步/方案/转人工标记……），活在 checkpoint；
- TicketState：业务状态机（new→…→closed），真值在数据库；
- 节点内写业务状态必须走 biz.tickets.update_state（校验+审计），"状态真值永远在库"。
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class EvidenceItem(TypedDict):
    id: str            # 语料 id 或工单号
    source: str        # faq | history
    question: str
    answer: str
    score: float


class DiagnosisStep(TypedDict):
    step: int
    query: str
    strategy: str          # faq_dense | faq_open | history
    top_score: float
    evidence_count: int


class GraphState(TypedDict, total=False):
    text: str                    # 用户原话
    customer_name: str
    ticket_id: int
    category: str
    priority: str
    evidence: Annotated[list[EvidenceItem], operator.add]       # 证据（追加语义）
    diagnosis_steps: Annotated[list[DiagnosisStep], operator.add]  # 诊断轨迹（评估用）
    solution: str                # 方案正文（带 [n] 引用）
    citations: list[dict]        # [{n, id, question, score}]
    escalate: bool               # Escalation Triage 结论
    escalate_reason: str
    human_note: str              # interrupt resume 时人工回复内容
    human_actor: str
    error: str