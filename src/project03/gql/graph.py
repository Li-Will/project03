"""LangGraph 工单图：建单 → 分类 → 诊断 → 方案 → Triage →（finalize | 转人工 interrupt）。

设计（面试口径）：
- 图只做"决策"，状态真值永远在库里：节点内业务落库走 biz.tickets.update_state（校验+审计）；
- 转人工 = 节点内 interrupt()，挂起在 checkpoint；人工回复 = Command(resume=...) 从挂起点继续
  （apply_human 节点把回复落库 + 状态 → pending_user）—— 完整 HITL，与 M1 的 API 模拟不同；
- 诊断 = 证据收集循环（最多 2 步策略切换：FAQ 稠密 → 不限产品重检 → 历史已解决工单），
  每步写入 diagnosis_steps（M3 评估轨迹）；
- checkpoint 用 SqliteSaver（data/checkpoints.db），业务库仍是 data/tickets.db —— 双库分离：
  运行时状态可回放（时间旅行），业务实体可查询（审计）。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from project03.biz import faq as faq_biz
from project03.biz.escalation import triage
from project03.biz.sla import set_deadline
from project03.biz.states import TicketState
from project03.biz.tickets import (
    add_message,
    categorize_ticket,
    create_ticket,
    get_or_create_customer,
    resolved_solutions,
    update_state,
)
from project03.db import models as db
from project03.gql.state import GraphState

MAX_DIAGNOSE_EVIDENCE = 3     # 方案引用上限（引用编号对应）
HISTORY_LIMIT = 2             # 历史已解决工单最多补 2 条

_GRAPH_LOCK = threading.Lock()   # SqliteSaver 连接非线程安全：invoke/resume 串行化
# 按业务库 key 缓存：db.configure 换库（测试）自动隔离 checkpoint 与编译图
_checkpointers: dict[str, SqliteSaver] = {}
_compiled_cache: dict[str, "StateGraph"] = {}


def _db_key() -> str:
    return str(db.get_engine().url.database)


def get_checkpointer() -> SqliteSaver:
    """惰性缓存：连接保持（SqliteSaver 要求连接存活）。

    checkpoint 路径跟随业务库（engine.url.database）：
    - 生产：data/tickets.db → data/checkpoints.db；
    - 测试：configure(tmp_path/xxx.db) 后自动隔离（key 不同 = 独立 checkpoint 文件）。
    """
    key = _db_key()
    if key not in _checkpointers:
        path = Path(key).parent / "checkpoints.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        _checkpointers[key] = SqliteSaver(conn)
    return _checkpointers[key]


# ---------- 节点 ----------

def build_ticket_node(state: GraphState, config: RunnableConfig) -> dict:
    """建单：NEW 落库 + 首条消息 + create 审计 + graph_thread_id 回写（resume 用）。"""
    thread_id = (config or {}).get("configurable", {}).get("thread_id", "")
    with db.session_scope() as s:
        cust = get_or_create_customer(s, state["customer_name"])
        ticket = create_ticket(s, cust.id, state["text"], intent="ticket", reason="LangGraph 自动建单（诊断流程）")
        if thread_id:
            t = s.get(db.Ticket, ticket.id)
            t.graph_thread_id = thread_id
    return {"ticket_id": ticket.id}


def classify_node(state: GraphState) -> dict:
    """受理：分类 + 优先级 + SLA 死限 → IN_TRIAGE（均有审计）。"""
    from project03.biz.tickets import prioritize_ticket

    with db.session_scope() as s:
        text = state["text"]
        category = categorize_ticket(text)
        priority = prioritize_ticket(text)
        t = s.get(db.Ticket, state["ticket_id"])
        t.category = category
        t.priority = priority
        set_deadline(s, t)
        update_state(s, t.id, TicketState.IN_TRIAGE, "system", "受理：分类+定级完成")
    return {"category": category, "priority": priority}


def diagnose_node(state: GraphState) -> dict:
    """证据收集（ReAct 思想，规则化）：策略逐级放宽，轨迹入 diagnosis_steps。"""
    text = state["text"]
    steps: list[dict] = []
    collected: list[dict] = []
    category = state.get("category", "")

    # step1: FAQ 稠密检索（带产品过滤）
    hits = faq_biz._searcher(text, top_k=3, product=faq_biz.detect_product(text))
    s1_score = hits[0]["score"] if hits else 0.0
    collected.extend(
        {"id": h["id"], "source": "faq", "question": h["question"], "answer": h["answer"], "score": h["score"]}
        for h in hits
    )
    steps.append({"step": 1, "query": text, "strategy": "faq_dense", "top_score": round(s1_score, 4), "evidence_count": len(hits)})

    # step2: top1 不足 → 不限产品重检（排除误过滤）
    if (not hits or s1_score < 0.55) and len(collected) < MAX_DIAGNOSE_EVIDENCE:
        hits2 = faq_biz._searcher(text, top_k=3)
        s2_score = hits2[0]["score"] if hits2 else 0.0
        seen = {c["id"] for c in collected}
        collected.extend(
            {"id": h["id"], "source": "faq", "question": h["question"], "answer": h["answer"], "score": h["score"]}
            for h in hits2 if h["id"] not in seen
        )
        steps.append({"step": 2, "query": text, "strategy": "faq_open", "top_score": round(s2_score, 4), "evidence_count": len(hits2)})

    # step3: 历史已解决工单补充（同分类下人工/AI 处理过的真实结论）
    with db.session_scope() as s:
        history = resolved_solutions(s, category, limit=HISTORY_LIMIT) if category else []
    for h in history[:HISTORY_LIMIT]:
        collected.append({"id": f"t{ h['ticket_id'] }", "source": "history", "question": h.get("question", ""), "answer": h["content"], "score": 0.55})
    if history:
        steps.append({"step": 3, "query": f"category={category}", "strategy": "history", "top_score": 0.55, "evidence_count": len(history)})

    collected = collected[:MAX_DIAGNOSE_EVIDENCE]
    with db.session_scope() as s:
        update_state(s, state["ticket_id"], TicketState.PROCESSING, "system", "诊断中：证据收集完成")
    return {"evidence": collected, "diagnosis_steps": steps}


def write_solution_node(state: GraphState) -> dict:
    """方案生成：证据 → 带 [n] 引用的文本（无证据时留给 triage 转人工，不编造）。"""
    evidence = state.get("evidence") or []
    if not evidence:
        return {"solution": "", "citations": []}
    citations = [{"n": i + 1, "id": e["id"], "question": e["question"], "source": e["source"], "score": e["score"]}
                 for i, e in enumerate(evidence)]
    top = evidence[0]
    lines = [
        f"排查结论与处理方案（基于 {len(evidence)} 条知识依据）：",
        top["answer"],
    ]
    if len(evidence) > 1:
        extra = "\n".join(f"[{c['n']}] {e['answer']}" for c, e in zip(citations, evidence[1:]))
        lines.append("补充依据：\n" + extra)
    solution = "\n\n".join(lines)
    return {"solution": solution, "citations": citations}


def triage_node(state: GraphState) -> dict:
    """Escalation Triage：该转人工 / 直接回复。"""
    evidence = state.get("evidence") or []
    top_conf = evidence[0]["score"] if evidence else None
    decision = triage(
        evidence_top_score=top_conf,
        priority=state.get("priority", "P3"),
        text=state.get("text", ""),
    )
    return {"escalate": decision.escalate, "escalate_reason": decision.reason}


def route_after_triage(state: GraphState) -> str:
    return "escalate" if state.get("escalate") else "finalize"


def finalize_node(state: GraphState) -> dict:
    """证据充足：方案回复落库 + RESOLVED（等用户 ack 关闭）。"""
    with db.session_scope() as s:
        add_message(s, state["ticket_id"], "agent", state["solution"], "diagnosis")
        update_state(s, state["ticket_id"], TicketState.RESOLVED, "agent", "方案已生成并回复用户")
    return {}


def escalate_mark_node(state: GraphState) -> dict:
    """转人工前置（幂等副作用）：业务落库 ESCALATED + 转人工模板消息。

    ⚠️ 与 interrupt 的边界：LangGraph 节点内 interrupt() 在 resume 时会重放该节点，
    所以「带副作用的落库」必须放在 interrupt 之前的独立节点（本节点），
    interrupt 节点（escalate_node）保持纯函数 —— 重放零副作用。
    """
    with db.session_scope() as s:
        add_message(s, state["ticket_id"], "agent",
                    f"已为您转接人工客服（原因：{state.get('escalate_reason', '')}）。", "template")
        update_state(s, state["ticket_id"], TicketState.ESCALATED, "system", state.get("escalate_reason", "转人工"))
    return {}


def escalate_node(state: GraphState) -> dict:
    """转人工语义中断：挂起等人工回复（纯函数，无副作用，可安全重放）。"""
    decision = interrupt({
        "type": "ticket_escalated",
        "ticket_id": state["ticket_id"],
        "reason": state.get("escalate_reason", ""),
    })
    # resume 后从这里继续：decision = Command(resume=...) 的载荷
    return {"human_note": decision.get("human_reply", ""), "human_actor": decision.get("actor", "human")}


def apply_human_node(state: GraphState) -> dict:
    """人工回复落地：消息入库 + 状态 → PENDING_USER（等用户确认）。"""
    with db.session_scope() as s:
        add_message(s, state["ticket_id"], "agent", state.get("human_note", ""), "human")
        update_state(s, state["ticket_id"], TicketState.PENDING_USER, state.get("human_actor", "human"),
                     "人工已回复，等待用户确认")
    return {}


# ---------- 图 ----------

def build_graph() -> StateGraph:
    g = StateGraph(GraphState)
    g.add_node("build_ticket", build_ticket_node)
    g.add_node("classify", classify_node)
    g.add_node("diagnose", diagnose_node)
    g.add_node("write_solution", write_solution_node)
    g.add_node("triage", triage_node)
    g.add_node("finalize", finalize_node)
    g.add_node("escalate_mark", escalate_mark_node)
    g.add_node("escalate", escalate_node)
    g.add_node("apply_human", apply_human_node)

    g.add_edge(START, "build_ticket")
    g.add_edge("build_ticket", "classify")
    g.add_edge("classify", "diagnose")
    g.add_edge("diagnose", "write_solution")
    g.add_edge("write_solution", "triage")
    g.add_conditional_edges("triage", route_after_triage, {"escalate": "escalate_mark", "finalize": "finalize"})
    g.add_edge("finalize", END)
    g.add_edge("escalate_mark", "escalate")  # 副作用先落库，再 interrupt 挂起
    g.add_edge("escalate", "apply_human")    # interrupt resume 后走 apply_human
    g.add_edge("apply_human", END)
    return g


def get_graph():
    """按业务库 key 缓存的编译图（checkpointer 与库一一对应）。"""
    key = _db_key()
    if key not in _compiled_cache:
        _compiled_cache[key] = build_graph().compile(checkpointer=get_checkpointer())
    return _compiled_cache[key]


def run_ticket_graph(text: str, customer_name: str = "访客") -> dict:
    """同步跑图（不遇 interrupt 则一路到 END）。返回 thread_id + 最终状态。"""
    return _run_first_pass(text, customer_name)


def stream_ticket_graph(text: str, customer_name: str = "访客"):
    """流式跑图：逐节点 yield (thread_id, node_name, update)（SSE 用）。

    遇 interrupt 时流自然结束（checkpoint 已保存），由 /human-reply resume。
    """
    tid = _next_thread_id()
    config = {"configurable": {"thread_id": tid}}
    g = get_graph()
    with _GRAPH_LOCK:
        for chunk in g.stream(
            {"text": text, "customer_name": customer_name},
            config,
            stream_mode="updates",
        ):
            for node_name, update in chunk.items():
                yield tid, node_name, update


_TICKET_SEQ = 0
_TICKET_SEQ_LOCK = threading.Lock()


def _next_thread_id() -> str:
    """进程内自增 thread_id（无并发写冲突；restart 可复用 checkpoints 目录）。"""
    global _TICKET_SEQ
    with _TICKET_SEQ_LOCK:
        _TICKET_SEQ += 1
        return f"t{ _TICKET_SEQ:06d}"


def _run_first_pass(text: str, customer_name: str) -> dict:
    tid = _next_thread_id()
    config = {"configurable": {"thread_id": tid}}
    g = get_graph()
    with _GRAPH_LOCK:
        result = g.invoke({"text": text, "customer_name": customer_name}, config)
    return {"thread_id": tid, "result": result}


def resume_ticket_graph(thread_id: str, human_reply: str, actor: str = "human") -> dict:
    """人工回复 → Command(resume=...) 从 interrupt 点恢复图，返回最终状态。"""
    config = {"configurable": {"thread_id": thread_id}}
    g = get_graph()
    with _GRAPH_LOCK:
        result = g.invoke(
            Command(resume={"human_reply": human_reply, "actor": actor}),
            config,
        )
    return {"thread_id": thread_id, "result": result}