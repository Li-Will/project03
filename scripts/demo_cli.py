"""M4 一键演示：FAQ 直答 / 报障建单·图诊断 / P1 转人工→resume / 满意度闭环 / SLA 升级。

一条命令跑通五个场景（真实 Qdrant 检索 + 真实 SQLite 库）：
    1) FAQ 高置信直答（不进图，<1s）
    2) 报障 → LangGraph 图诊断 → 方案 resolve（证据充足，工单建单到闭环）
    3) P1 紧急事件 → 强制转人工（图 interrupt 挂起，等待人工）
    4) 人工接管回复 → resume 恢复 → 用户 5 星满意度 → 工单关闭
    5) SLA 扫描：处理中 P1 单死限超时 → 自动升级 + 管理队列可见

用法：
    export HF_HOME=~/.cache/p408qa HF_HUB_OFFLINE=1   # 复用离线 embedding 缓存
    python scripts/init_index.py                       # 首次需建 FAQ 索引
    python scripts/demo_cli.py
注意：脚本会重建 data/tickets.db + checkpoint（开发演示库，幂等可重复跑）。
脚本直接调用 `*_impl` 业务函数（不经 HTTP）：端点用 Depends 注入身份，直接调端点会拿到
Depends 对象——这正是 P0-3 加鉴权后演示脚本崩过一次的原因，回归护栏见 tests/test_api.py。
"""
from __future__ import annotations

import os
import time
from datetime import timedelta

os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/p408qa"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from sqlalchemy import select  # noqa: E402

from project03.api.main import HumanReplyRequest  # noqa: E402
from project03.api.main import ack_ticket_impl, handle_chat, human_reply_impl  # noqa: E402
from project03.biz import sla as sla_mod  # noqa: E402
from project03.biz.states import TicketState  # noqa: E402
from project03.biz.tickets import create_ticket, get_or_create_customer, get_ticket_history, update_state  # noqa: E402
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
    print("    回复:", (r.get("reply") or "—")[:88].replace("\n", " / "))


def main() -> None:
    biz_db = db.get_settings().db_path
    for f in (biz_db, biz_db.parent / f"checkpoints_{biz_db.stem}.db"):
        if f.exists():
            f.unlink()  # 演示库幂等重建（M3 起 checkpoint 按业务库 stem 隔离命名）
    db.init_db()
    print("=" * 66)
    print("M4 一键演示 · 企业客服多 Agent 工单系统（Ticket Agent）")
    print("=" * 66)

    t0 = time.perf_counter()
    r1 = handle_chat("免费用户有多少存储容量呀？", "演示用户")
    show("1) FAQ 高置信直答（简单问题不进图）", r1, t0)

    t0 = time.perf_counter()
    r2 = handle_chat("会议录制的功能报错了，一直打不开是怎么回事", "演示用户")
    show("2) 报障 → 图诊断 → 方案 resolve（建单到闭环）", r2, t0)

    t0 = time.perf_counter()
    r3 = handle_chat("我的云盘突然无法使用了，说在维护后一直还没修好，现在很紧急", "演示用户")
    show("3) P1 紧急事件 → 强制转人工（图 interrupt 挂起）", r3, t0)
    ticket_id = r3.get("ticket_id") or r2.get("ticket_id")

    if ticket_id:
        t0 = time.perf_counter()
        print(f"\n▶ 4) 人工接管 → resume 恢复（工单 {ticket_id}）")
        rr = human_reply_impl(ticket_id, HumanReplyRequest(content="您好，我是人工客服小美，已为您核查：云盘维护已完成，请重试上传。如仍异常我帮您升级。"), actor="human")
        print(f"    state: {rr['state']}  [{(time.perf_counter()-t0)*1000:.0f}ms]")
        with db.session_scope() as s:
            h = get_ticket_history(s, ticket_id)
        print("    审计链:", " → ".join(f"{e['to']}({e['actor']})" for e in h["events"]))

        t0 = time.perf_counter()
        rr2 = ack_ticket_impl(ticket_id, rating=5)
        print(f"▶ 5) 用户 5 星满意度 → 工单关闭（{ticket_id}）")
        print(f"    state: {rr2['state']}  [{(time.perf_counter()-t0)*1000:.0f}ms]")
        with db.session_scope() as s:
            rev = s.scalar(select(db.Review).where(db.Review.ticket_id == ticket_id))
        print(f"    满意度入库: rating={rev.rating}  （Review #{rev.id}）" if rev else "    满意度入库: 无")

    # 6) SLA：造一张处理中的 P1 单，把死限拨到过去 → 扫描升级
    with db.session_scope() as s:
        cust = get_or_create_customer(s, "SLA演示用户")
        t6 = create_ticket(s, cust.id, "P1 测试工单", "ticket", category="云盘服务", priority="P1")
        sla_mod.set_deadline(s, t6)                              # 30 分钟死限
        update_state(s, t6.id, TicketState.IN_TRIAGE, "system", "受理")
        t6.sla_deadline = db.utcnow() - timedelta(minutes=5)     # 已超时 5 分钟
        s.flush()
        sla_id = t6.id
    with db.session_scope() as s:
        res = sla_mod.scan_sla(s)
    print(f"\n▶ 6) SLA 扫描（工单 {sla_id}，P1 死限已过 5 分钟）")
    print(f"    升级: {res['escalated']}  提醒: {res['warned']}")
    with db.session_scope() as s:
        h = get_ticket_history(s, sla_id)
    ev = h["events"][-1]
    print(f"    末次审计: {ev['from']} → {ev['to']} reason={ev['reason'][:50]}")
    with db.session_scope() as s:
        q = sla_mod.list_escalations(s)
    print(f"    管理升级队列: {len(q)} 张 ->", [x["ticket_id"] for x in q][:5])

    print("\n" + "=" * 66)
    print("演示完成：FAQ 直答 / 报障建单 / 转人工 resume / 满意度闭环 / SLA 升级 全链路 OK")


if __name__ == "__main__":
    main()