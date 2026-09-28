"""M1 演示脚本：一条命令跑通五大场景（FAQ 直答/患喧/报障/投诉/转人工）。

用法：
    python scripts/demo_chat.py
    （首次运行前请先 python scripts/init_index.py 建好 FAQ 索引；脚本会自行 init_db）
"""
from __future__ import annotations

import time

from project03.api.main import handle_chat
from project03.biz import faq as faq_biz
from project03.db import models as db


def timed(label: str, text: str, name: str = "演示用户") -> dict:
    t0 = time.perf_counter()
    res = handle_chat(text, name)
    dt = (time.perf_counter() - t0) * 1000
    print(f"\n▶ {label}（{dt:.0f}ms）")
    print(f"  输入: {text}")
    print(f"  类型: {res.get('type')} | 置信度: {res.get('confidence', '-')}")
    print(f"  回复: {res.get('reply', res.get('answer', res.get('ticket_id', '-'))) if res.get('type') not in ('faq',) else res.get('reply')[:60] + '…'}")
    if res.get("citations"):
        for c in res["citations"]:
            print(f"    [引用 {c['n']}] {c['question']}（{c['id']}）score={c['score']}")
    if res.get("ticket_id"):
        print(f"  工单: #{res['ticket_id']} state={res.get('state')}")
    return res


def main() -> None:
    db.init_db()
    print("=" * 60)
    print("M1 MVP 演示：企业客服多 Agent 工单系统（规则版）")
    print("=" * 60)

    timed("FAQ 高置信直答", "免费用户有多少存储容量呀？")
    timed("FAQ 中置信（带人工提示）", "上传文件老失败是为什么？")
    timed("闲聊", "你好呀")
    timed("报障建单", "我的会议打不开了，一直报错卡死，很急！", "张三")
    timed("投诉转人工", "你们的云盘太差了，分享链接打不开，我要投诉！", "张三")
    r = timed("低置信转人工（模糊提问）", "我今天状态不太好", "张三")
    print("\n" + "=" * 60)
    print("审计回放：查询演示工单的事件链")
    print("=" * 60)
    tid = r.get("ticket_id")
    if not tid:
        # 低置信那条应是工单；保险起见列出前两张工单
        from sqlalchemy import select
        with db.session_scope() as s:
            ticket = s.scalar(select(db.Ticket).order_by(db.Ticket.id.desc()).limit(1))
        tid = ticket.id if ticket else None
    if tid:
        with db.session_scope() as s:
            hist = db_get_history(s, tid)
        for e in hist["events"]:
            print(f"  {e['at'][11:19]}  {e['from'] or '-'} → {e['to']}  actor={e['actor']}  reason={e['reason']}")
    print("\n小贴士：FAQ 直答命中分数阈值 <0.50 时系统不会硬答，直接转人工。")


def db_get_history(s, tid):
    from project03.biz.tickets import get_ticket_history
    return get_ticket_history(s, tid)


if __name__ == "__main__":
    main()