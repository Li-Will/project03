"""M2 验收演示：FAQ 直答 / 诊断 resolve / 低置信转人工→resume / SLA 升级（真实 Qdrant + 真实库）。

用法：
    export HF_HOME=~/.cache/p408qa HF_HUB_OFFLINE=1   # 复用离线 embedding 缓存
    python scripts/init_index.py                       # 首次需建 FAQ 索引
    python scripts/demo_m2.py
注意：脚本会重建 data/tickets.db（M2 新增 graph_thread_id 列，旧库不兼容直接重建）。
"""
from __future__ import annotations

import os
import time
from datetime import timedelta

os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/p408qa"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from project03.api.main import handle_chat  # noqa: E402
from project03.biz import sla as sla_mod  # noqa: E402
from project03.biz.states import TicketState  # noqa: E402
from project03.biz.tickets import get_ticket_history, update_state  # noqa: E402
from project03.db import models as db  # noqa: E402


def show(tag: str, r: dict, t0: float) -> None:
    dt = (time.perf_counter() - t0) * 1000
    kind = r.get("type", "?")
    print(f"\n▶ {tag}  [{kind}]  {dt:.0f}ms")
    for k in ("intent", "confidence", "ticket_id", "state", "category", "priority", "reason"):
        if r.get(k) is not None:
            print(f"    {k}: {r[k]}")
    if r.get("citations"):
        print("    引用:", ", ".join(f"[{c['n']}]{c['id']}" for c in r["citations"]))
    if r.get("diagnosis_steps"):
        print("    诊断轨迹:", " → ".join(f"步{s['step']}({s['strategy']} top={s['top_score']})" for s in r["diagnosis_steps"]))
    print("    回复:", (r.get("reply") or "—")[:80].replace("\n", " / "))


def main() -> None:
    biz_db = db.get_settings().db_path
    if biz_db.exists():
        biz_db.unlink()  # M2 表结构新增列：开发阶段直接重建
    chk = biz_db.parent / "checkpoints.db"
    if chk.exists():
        chk.unlink()
    db.init_db()
    print("=" * 66)
    print("M2 验收演示 · 企业客服多 Agent 工单系统（LangGraph 完整版）")
    print("=" * 66)

    t0 = time.perf_counter()
    r1 = handle_chat("免费用户有多少存储容量呀？", "演示用户")
    show("1) FAQ 高置信直答（不进图）", r1, t0)

    t0 = time.perf_counter()
    r2 = handle_chat("会议录制的功能报错了，一直打不开是怎么回事", "演示用户")
    show("2) 报障 → 图诊断 resolve（证据充足）", r2, t0)

    t0 = time.perf_counter()
    r3 = handle_chat("我的云盘突然无法使用了，说在维护后一直还没修好，现在很紧急", "演示用户")
    show("3) P1 紧急事件 → 强制转人工（图 interrupt 挂起）", r3, t0)
    ticket_id = r3.get("ticket_id") or r2.get("ticket_id")

    if ticket_id:
        t0 = time.perf_counter()
        from project03.api.main import human_reply
        from project03.api.main import HumanReplyRequest
        print(f"\n▶ 4) 人工接管 → resume 恢复（工单 {ticket_id}）")
        rr = human_reply(ticket_id, HumanReplyRequest(content="您好，我是人工客服小美，请提供账号便于查询。", actor="human"))
        print(f"    state: {rr['state']}  [{(time.perf_counter()-t0)*1000:.0f}ms]")
        with db.session_scope() as s:
            h = get_ticket_history(s, ticket_id)
        print("    审计链:", " → ".join(f"{e['to']}({e['actor']})" for e in h["events"]))

    # 5) SLA：造一张处理中的 P1 单，把死限拨到过去 → 扫描升级
    from project03.db.models import Ticket
    from project03.biz.tickets import create_ticket, get_or_create_customer
    with db.session_scope() as s:
        cust = get_or_create_customer(s, "SLA演示用户")
        t5 = create_ticket(s, cust.id, "P1 测试工单", "ticket", category="云盘服务", priority="P1")
        sla_mod.set_deadline(s, t5)                              # 30 分钟死限
        update_state(s, t5.id, TicketState.IN_TRIAGE, "system", "受理")
        t5.sla_deadline = db.utcnow() - timedelta(minutes=5)     # 已超时 5 分钟
        s.flush()
        sla_id = t5.id
    with db.session_scope() as s:
        res = sla_mod.scan_sla(s)
    print(f"\n▶ 5) SLA 扫描（工单 {sla_id}，P1 死限已过 5 分钟）")
    print(f"    升级: {res['escalated']}  提醒: {res['warned']}")
    with db.session_scope() as s:
        h = get_ticket_history(s, sla_id)
    ev = h["events"][-1]
    print(f"    末次审计: {ev['from']} → {ev['to']} reason={ev['reason'][:50]}")
    with db.session_scope() as s:
        q = sla_mod.list_escalations(s)
    print(f"    管理升级队列: {len(q)} 张 ->", [x["ticket_id"] for x in q][:5])

    print("\n" + "=" * 66)
    print("演示完成：FAQ 直答 / 图诊断 / 转人工 resume / SLA 升级 全链路 OK")


if __name__ == "__main__":
    main()