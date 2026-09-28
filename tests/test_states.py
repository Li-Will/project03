"""状态机单测：合法迁移放行、非法迁移 100% 拒绝（M1 验收项 1）。"""
from __future__ import annotations

import pytest

from project03.biz.states import (
    TRANSITIONS,
    StateTransitionError,
    TicketState,
    can_transition,
    validate_transition,
)


def test_happy_path_chain():
    """主流程合法链：new → in_triage → processing → pending_user → resolved → closed。"""
    chain = [
        (TicketState.NEW, TicketState.IN_TRIAGE),
        (TicketState.IN_TRIAGE, TicketState.PROCESSING),
        (TicketState.PROCESSING, TicketState.PENDING_USER),
        (TicketState.PENDING_USER, TicketState.RESOLVED),
        (TicketState.RESOLVED, TicketState.CLOSED),
    ]
    for src, dst in chain:
        assert can_transition(src, dst), f"{src} -> {dst} 应合法"
        validate_transition(src, dst)  # 不应抛


@pytest.mark.parametrize(
    "src,dst",
    [
        (TicketState.CLOSED, TicketState.PROCESSING),     # 终态回退
        (TicketState.CLOSED, TicketState.RESOLVED),       # 终态任何迁移
        (TicketState.NEW, TicketState.RESOLVED),          # 跳级（跳过受理）
        (TicketState.REJECTED, TicketState.NEW),          # 拒绝后复活
        (TicketState.NEW, TicketState.CLOSED),            # 大跳级
    ],
)
def test_illegal_transitions_rejected(src, dst):
    assert not can_transition(src, dst), f"{src} -> {dst} 应是非法迁移"
    with pytest.raises(StateTransitionError):
        validate_transition(src, dst)


def test_error_message_contains_allowed_targets():
    with pytest.raises(StateTransitionError) as ei:
        validate_transition(TicketState.CLOSED, TicketState.PROCESSING)
    assert "非法状态迁移" in str(ei.value)
    assert "closed -> processing" in str(ei.value)


def test_all_states_covered_in_table():
    """TRANSITIONS 必须覆盖全部状态（漏配 = 该状态无法迁移/无终态）。"""
    for s in TicketState:
        assert s in TRANSITIONS, f"状态 {s} 未在迁移表中登记"


def test_escalation_paths():
    """转人工/人工接管后可继续推进（业务关键路径）。"""
    assert can_transition(TicketState.NEW, TicketState.ESCALATED)
    assert can_transition(TicketState.IN_TRIAGE, TicketState.ESCALATED)
    assert can_transition(TicketState.PROCESSING, TicketState.ESCALATED)
    assert can_transition(TicketState.ESCALATED, TicketState.PENDING_USER)
    assert can_transition(TicketState.ESCALATED, TicketState.RESOLVED)
    assert not can_transition(TicketState.ESCALATED, TicketState.CLOSED)  # 人工处理后需先落 pending/resolved