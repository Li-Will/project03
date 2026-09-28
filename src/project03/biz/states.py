"""工单状态机（本项目最核心的确定性资产）。

- 状态枚举 + 合法迁移表写死；任何 Agent/API 只能通过 validate_transition 走合法迁移；
- 非法迁移（如 CLOSED -> PROCESSING）抛 StateTransitionError，绝不静默放行；
- 状态真值始终在数据库（Ticket.state），本模块只负责"合法性"，不负责存储。
"""
from __future__ import annotations

from enum import Enum


class TicketState(str, Enum):
    NEW = "new"                      # 新单（刚创建）
    IN_TRIAGE = "in_triage"          # 受理中（分类/定优先级）
    PROCESSING = "processing"        # 处理中（Agent 诊断/人工处理）
    PENDING_USER = "pending_user"    # 待用户补充/确认
    RESOLVED = "resolved"            # 已解决（等用户确认或直接关闭）
    CLOSED = "closed"                # 已关闭（终态）
    ESCALATED = "escalated"          # 已升级/转人工（人工接管中）
    REJECTED = "rejected"            # 已拒绝（意图误判/无效请求，终态）

    def __str__(self) -> str:  # 日志与审计里用短名，避免 <TicketState.NEW: 'new'>
        return self.value


# 合法迁移表：<当前状态> -> <可达状态集合>
# 依据计划书 4.1：线路上推进（new→…→closed），旁路转人工（escalated），
# 前置误判拒绝（rejected）；escalated 人工处理后可继续到 pending_user/resolved/processing。
TRANSITIONS: dict[TicketState, frozenset[TicketState]] = {
    TicketState.NEW: frozenset({TicketState.IN_TRIAGE, TicketState.ESCALATED, TicketState.REJECTED}),
    TicketState.IN_TRIAGE: frozenset({TicketState.PROCESSING, TicketState.PENDING_USER, TicketState.ESCALATED, TicketState.REJECTED}),
    TicketState.PROCESSING: frozenset({TicketState.PENDING_USER, TicketState.RESOLVED, TicketState.ESCALATED}),
    TicketState.PENDING_USER: frozenset({TicketState.PROCESSING, TicketState.RESOLVED, TicketState.ESCALATED}),
    TicketState.RESOLVED: frozenset({TicketState.CLOSED, TicketState.PROCESSING}),
    TicketState.ESCALATED: frozenset({TicketState.PENDING_USER, TicketState.RESOLVED, TicketState.PROCESSING}),
    TicketState.CLOSED: frozenset(),     # 终态
    TicketState.REJECTED: frozenset(),   # 终态
}


class StateTransitionError(ValueError):
    """非法状态迁移：抛出时不允许落库（调用方必须保证原子性）。"""

    def __init__(self, src: TicketState, dst: TicketState) -> None:
        self.src = src
        self.dst = dst
        allowed = sorted(s.value for s in TRANSITIONS[src])
        super().__init__(f"非法状态迁移: {src.value} -> {dst.value}（合法目标: {allowed or '无（终态）'}）")


def can_transition(src: TicketState, dst: TicketState) -> bool:
    """两个状态之间是否存在合法迁移。"""
    return dst in TRANSITIONS[src]


def validate_transition(src: TicketState, dst: TicketState) -> None:
    """不合法直接抛 StateTransitionError；合法则无副作用（不落库）。"""
    if not can_transition(src, dst):
        raise StateTransitionError(src, dst)


def all_states() -> list[str]:
    return [s.value for s in TicketState]