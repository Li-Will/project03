"""M3 评估 runner：80 条 gold set 四类 → 指标（控制台 + JSON/MD 报告）。

用法:
  python eval/run_eval.py                 # live：真实 Qdrant 检索（需 HF 离线模型 + 索引）
  python eval/run_eval.py --mode mock     # 固定高分 fake：逻辑链路自检（CI 用，无外部依赖）
  python eval/run_eval.py --mode snapshot # 重放上次 live 的检索快照（离线可复现）
  python eval/run_eval.py --no-write      # 只打印不落盘

指标（验收口径见 docs/项目计划书.md §1.2）：
- 意图准确率   = intent 类 gold 命中 / 30（验收 ≥90%）
- FAQ 解决率   = faq 类「直答且引用命中 gold_id」/ 25（验收 ≥85%）
- 引用命中率   = 直答数中含 gold_id 的比例（验收 100%）
- 转人工 F1    = diagnose+escalate 类的该转/不该转预测 vs 人工标注（验收 ≥0.85）
- 分类/定级准确率 = diagnose 类 category/priority 命中
- P50/P95 延迟（按类）
- 成本 = 0 token（规则版；LLM 双模式未实施——诚实标注，不虚报）
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))  # 同目录 judge
sys.path.insert(0, str(ROOT))           # editable 安装已是包的兜底

from project03.biz import faq as faq_biz
from project03.biz.intent import classify_intent
from project03.db import models as db
from project03.gql import graph as gql

import judge

GOLD_PATH = ROOT / "eval" / "gold_set.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"
SNAPSHOT_PATH = RESULTS_DIR / "retrieval_snapshot.json"

FAKE_SCORE = 0.85  # mock 模式固定高分（只测逻辑，不测检索质量）


def load_gold() -> list[dict]:
    items = []
    with open(GOLD_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def load_seed_index() -> dict:
    """id → (question, answer)（mock 模式的真实内容占位）。"""
    idx: dict[str, tuple[str, str]] = {}
    with open(ROOT / "src/project03/biz/faq_seed.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            idx[r["id"]] = (r["question"], r["answer"])
    return idx


def make_fake_searcher(items: list[dict], seed_idx: dict, snapshot: dict | None = None):
    """按 gold 标注构造检索器：标了 gold_id/expect_id → 高分返回；否则无证据。

    snapshot 非 None 时改从快照重放真实 hits（离线可复现）。
    """
    by_query: dict[str, list[dict]] = {}
    for it in items:
        target = it.get("gold_id") or it.get("expect_id")
        if not target:
            continue
        q = it["text"]
        if target in seed_idx:
            question, answer = seed_idx[target]
        else:
            question, answer = "标准答案占位", f"标准答案（{target}）"
        by_query[q] = [{"id": target, "question": question, "answer": answer, "score": FAKE_SCORE}]

    if snapshot:
        snap_by_query = snapshot.get("by_query", {})

        def searcher(query, top_k=3, product=None):
            hits = snap_by_query.get(query, [])
            out: list[dict] = []
            for h in hits:
                item = dict(h)
                # 快照来自图路径 citations（无 answer 字段）→ 用 seed 补全（answer 是回复素材）
                if not item.get("answer") and item.get("id") in seed_idx:
                    item["answer"] = seed_idx[item["id"]][1]
                out.append(item)
            return out

        return searcher

    def searcher(query, top_k=3, product=None):
        return list(by_query.get(query, [])[:top_k])

    return searcher


def run_item(item: dict, searcher, records: list[dict], snapshot_log: dict) -> None:
    cls = item["cls"]
    rec = {"id": item["id"], "cls": cls, "text": item["text"], "gold": item.get("gold") or item.get("escalate")}
    t0 = time.perf_counter()
    rec["ms"] = 0.0  # 在分支内计真实耗时

    if cls == "intent":
        t1 = time.perf_counter()
        r = classify_intent(item["text"])
        rec["ms"] = (time.perf_counter() - t1) * 1000.0
        rec["predict"] = r.intent
        rec["confidence"] = round(r.confidence, 3)
        rec["ok"] = r.intent == item["gold"]

    elif cls == "faq":
        t1 = time.perf_counter()
        r = faq_biz.answer_faq(item["text"])
        rec["ms"] = (time.perf_counter() - t1) * 1000.0
        cite_ids = [c["id"] for c in r.citations]
        hit = item["gold_id"] in cite_ids
        rec["direct"] = bool(r.direct)
        rec["top_conf"] = round(float(r.confidence), 4)
        rec["cite_ids"] = cite_ids
        rec["cite_top1_ok"] = bool(cite_ids and cite_ids[0] == item["gold_id"])
        rec["hit"] = hit
        rec["ok"] = bool(r.direct) and hit
        snapshot_log.setdefault("by_query", {})[item["text"]] = r.citations or []

    elif cls == "faq_negative":
        # 知识库外的诉求（赔付/议价/线下/竞品比较）：理想行为 = 不给具体答案，
        # 由入口层转人工 —— "该转不转是事故"，负例用来量化"不该答的时候会不会答"
        t1 = time.perf_counter()
        r = faq_biz.answer_faq(item["text"])
        rec["ms"] = (time.perf_counter() - t1) * 1000.0
        rec["direct"] = bool(r.direct)
        rec["top_conf"] = round(float(r.confidence), 4)
        rec["cite_ids"] = [c["id"] for c in r.citations]
        rec["ok"] = not bool(r.direct)
        snapshot_log.setdefault("by_query", {})[item["text"]] = r.citations or []

    else:  # diagnose / escalate：走 LangGraph 工单图（真实决策链）
        t1 = time.perf_counter()
        res = gql.run_ticket_graph(item["text"], customer_name="评测用户")
        rec["ms"] = (time.perf_counter() - t1) * 1000.0
        cite_ids = [c["id"] for c in res["result"].get("citations", [])]
        rec["predict_escalate"] = bool(res["result"].get("escalate", False))
        rec["escalate_reason"] = res["result"].get("escalate_reason", "")
        rec["predict_category"] = res["result"].get("category", "")
        rec["predict_priority"] = res["result"].get("priority", "")
        rec["cite_ids"] = cite_ids
        rec["solution"] = res["result"].get("solution", "")
        rec["ticket_id"] = res["result"].get("ticket_id")
        rec["cite_top1_ok"] = bool(cite_ids and cite_ids[0] == item.get("expect_id")) if item.get("expect_id") else None
        rec["ok"] = rec["predict_escalate"] == bool(item["escalate"])
        if cls == "diagnose":
            rec["ok_cat"] = rec["predict_category"] == item["category"]
            rec["ok_pri"] = rec["predict_priority"] == item["priority"]
        # 快照：存检索类 evidence（过滤 history 条目 tN——无 answer 且非检索证据，
        # history 属运行时态，快照只回放检索）
        snapshot_log.setdefault("by_query", {})[item["text"]] = [
            c for c in res["result"].get("citations", []) if not str(c.get("id", "")).startswith("t")
        ]

    rec["ms_total"] = (time.perf_counter() - t0) * 1000.0
    records.append(rec)


def compute_metrics(records: list[dict]) -> dict:
    m = {"n": len(records)}
    by_cls = {c: [r for r in records if r["cls"] == c]
              for c in ("intent", "faq", "faq_negative", "diagnose", "escalate")}

    for cls in by_cls:
        items = by_cls[cls]
        ms = [r["ms"] for r in items]
        m[f"{cls}_n"] = len(items)
        m[f"{cls}_p50_ms"] = round(statistics.median(ms), 1) if ms else 0.0
        m[f"{cls}_p95_ms"] = round(sorted(ms)[int(len(ms) * 0.95) - 1], 1) if len(ms) > 1 else (ms[0] if ms else 0.0)

    intents = by_cls["intent"]
    m["intent_acc"] = round(sum(1 for r in intents if r["ok"]) / max(len(intents), 1), 4)

    faqs = by_cls["faq"]
    direct_n = sum(1 for r in faqs if r["direct"])
    m["faq_direct_n"] = direct_n
    m["faq_resolved"] = round(sum(1 for r in faqs if r["ok"]) / max(len(faqs), 1), 4)
    m["faq_cite_hit_rate"] = round(sum(1 for r in faqs if r["hit"]) / max(direct_n, 1), 4)

    negs = by_cls["faq_negative"]
    m["faq_neg_n"] = len(negs)
    # 负例拦截率：知识库外诉求中"没有给出具体答案"的比例（越高越安全）
    m["faq_neg_blocked"] = round(sum(1 for r in negs if r["ok"]) / max(len(negs), 1), 4)

    esc_items = by_cls["diagnose"] + by_cls["escalate"]
    tp = sum(1 for r in esc_items if r["ok"] and r.get("predict_escalate") and r["gold"])
    fp = sum(1 for r in esc_items if not r["ok"] and r.get("predict_escalate"))
    fn = sum(1 for r in esc_items if not r["ok"] and not r.get("predict_escalate"))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    m["escalate_tp"], m["escalate_fp"], m["escalate_fn"] = tp, fp, fn
    m["escalate_precision"], m["escalate_recall"], m["escalate_f1"] = round(precision, 4), round(recall, 4), round(f1, 4)

    diags = by_cls["diagnose"]
    m["diag_cat_acc"] = round(sum(1 for r in diags if r.get("ok_cat")) / max(len(diags), 1), 4)
    m["diag_pri_acc"] = round(sum(1 for r in diags if r.get("ok_pri")) / max(len(diags), 1), 4)

    # 方案质量（规则版 judge）
    q_ok = [r for r in esc_items if judge.solution_quality(r.get("solution", ""), [{"n": i + 1} for i in range(len(r.get("cite_ids", [])))], escalate=r.get("predict_escalate"), escalate_reason=r.get("escalate_reason", ""))["ok"]]
    m["quality_ok"] = len(q_ok)
    m["cost_tokens"] = 0  # 规则版 0 token；LLM 双模式未实施（诚实标注）
    return m


def _fmt_ms(x: float) -> str:
    return "<1" if x < 1 else f"{x:.0f}"


def render_md(mode: str, gold: list[dict], records: list[dict], m: dict, ts: str) -> str:
    L = []
    from project03.config import get_settings as _gs
    _st = _gs()
    L.append(f"# M3 评测报告（mode={mode} · {ts} · gold set {m['n']} 条）")
    L.append("")
    L.append(f"> 检索配置：index_fields=`{_st.faq_index_fields}` · 混合检索(BM25)=`{_st.use_bm25}` · "
             f"精排=`{_st.use_rerank}` · 阈值 direct={_st.conf_direct} / hint={_st.conf_hint}")
    L.append("")
    L.append("## 1. 总览")
    L.append("| 指标 | 值 | 验收 |")
    L.append("|---|---|---|")
    L.append(f"| 意图准确率 | {m['intent_acc'] * 100:.1f}% ({sum(1 for r in records if r['cls']=='intent' and r['ok'])}/{m['intent_n']}) | ≥90% |")
    L.append(f"| FAQ 解决率 | {m['faq_resolved'] * 100:.1f}% ({sum(1 for r in records if r['cls']=='faq' and r['ok'])}/{m['faq_n']}) | ≥85% |")
    L.append(f"| 引用命中率（直答类）| {m['faq_cite_hit_rate'] * 100:.1f}% ({sum(1 for r in records if r['cls']=='faq' and r['hit'])}/{m['faq_direct_n']}) | 100% |")
    L.append(f"| **负例拦截率**（知识库外诉求不直答）| {m['faq_neg_blocked'] * 100:.1f}% ({sum(1 for r in records if r['cls']=='faq_negative' and r['ok'])}/{m['faq_neg_n']}) | 越高越好 |")
    L.append(f"| 转人工 F1 | {m['escalate_f1']:.4f}（P {m['escalate_precision']:.3f} / R {m['escalate_recall']:.3f}，TP {m['escalate_tp']} / FP {m['escalate_fp']} / FN {m['escalate_fn']}）| ≥0.85 |")
    L.append(f"| 诊断分类准确率 | {m['diag_cat_acc'] * 100:.1f}% | — |")
    L.append(f"| 诊断定级准确率 | {m['diag_pri_acc'] * 100:.1f}% | — |")
    L.append(f"| 方案质量通过（规则版 judge）| {m['quality_ok']}/{m['escalate_n'] + m['diagnose_n']} | — |")
    L.append(f"| P50 / P95（intent）| {_fmt_ms(m['intent_p50_ms'])} / {_fmt_ms(m['intent_p95_ms'])} ms | — |")
    L.append(f"| P50 / P95（faq）| {_fmt_ms(m['faq_p50_ms'])} / {_fmt_ms(m['faq_p95_ms'])} ms | — |")
    L.append(f"| P50 / P95（diagnose）| {_fmt_ms(m['diagnose_p50_ms'])} / {_fmt_ms(m['diagnose_p95_ms'])} ms | — |")
    L.append(f"| P50 / P95（escalate）| {_fmt_ms(m['escalate_p50_ms'])} / {_fmt_ms(m['escalate_p95_ms'])} ms | — |")
    L.append(f"| 成本 | {m['cost_tokens']} token（规则版）| — |")
    L.append("")
    L.append("> 验收口径：docs/项目计划书.md §1.2。**未达标项 = 规则盲区，属评测价值所在（见失败明细与改进路径）。**")
    L.append("")

    L.append("## 2. 失败明细（gold 预测不符）")
    bad = [r for r in records if not r["ok"]]
    if bad:
        for r in bad:
            if r["cls"] in ("faq", "faq_negative"):
                line = f"- {r['id']} [{r['cls']}] 「{r['text']}」→ direct={r.get('direct')} top={r.get('top_conf')} cites={r.get('cite_ids')}（gold={r.get('gold')}）"
            elif r["cls"] == "intent":
                line = f"- {r['id']} [{r['cls']}] 「{r['text']}」→ {r.get('predict')}（gold={r.get('gold')}）"
            else:
                line = f"- {r['id']} [{r['cls']}] 「{r['text']}」→ escalate={r.get('predict_escalate')}（gold={r.get('gold')}）cat={r.get('predict_category')}/pri={r.get('predict_priority')} cites={r.get('cite_ids')}"
            L.append(line)
    else:
        L.append("- 无（全部命中）")
    L.append("")

    L.append("## 3. 已知局限与改进路径（评测实战发现，非脚本构造）")
    L.append("")
    L.append("- **口语化弱同义问法检索误配**：「视频一直转圈」top1=「会议链接被别人转发」（0.5362）而非「开会画面卡顿」；换问法「开会卡顿怎么办」（0.7615）才直答。改进路径：查询改写 / 同义词扩展 / 混合检索（对齐 project01 口语化改写 -4.0pp 的教训）。")
    L.append("- **规则关键词子串盲区**：插入语会拆散触发词（「云盘**一直上传**失败」不命中「一直失败」）。已补复合故障词；本质解是 LLM 路由（M2 后计划）。")
    L.append("- **模糊表达检索分数虚高**：「说不清楚/很严重」类 query 检索 top 可到 0.5+ 跨过放行门槛。Triage 已补表达模糊信号（负责人/说不清楚/不知道怎么说）；本质仍是置信度校准。")
    L.append("- **引用目标语义偏斜（diagnose 类）**：「开会时听不到别人说话」检索 top1=「进不去会议」而非语义目标 mn-11（音频输出切换）。已按语义标注 expect_id 并如实计入（cite_top1 单列，不进验收线）。")
    L.append("- **历史工单只作素材不作证据**：同分类历史解决记录在 triage 判定中被排除（\"该转不转是事故\"，弱先验不放大行）。")
    L.append("- **LLM-as-judge 未实施**（无 key）：方案质量/引用归因为规则版近似，人工校准并入 gold 标注。")
    L.append("")
    L.append("## 4. 诚实边界")
    L.append("- **mock 模式**只验证逻辑链路（检索按 gold 固定高分），不测检索质量；检索质量见 live 模式。")
    L.append("- **LLM-as-judge 未实施**（无 LLM key）：方案质量/引用归因为规则版近似判定，人工校准并入 gold 标注（「谁标注谁负责」）；LLM 双模式启用后可换 LLM-as-judge 并与规则版做差异对比。")
    L.append("- **成本=0 token** 仅对规则版成立；LLM 模式需另测。")
    L.append("- 引用命中率验收线 100% 只覆盖「直答且带引用」的条目；无证据转人工的条目不参与（引用为空是预期行为）。")
    L.append("")
    L.append("## 5. 复现")
    L.append(f"```bash\npython eval/run_eval.py --mode {mode}          # 复现本次运行\npython eval/run_eval.py --mode live            # 真实检索（需 HF 离线模型+索引）\npython eval/run_eval.py --mode snapshot        # 重放 eval/results/retrieval_snapshot.json\n```")
    return "\n".join(L)


def main(mode: str = "live", write: bool = True) -> dict:
    gold = load_gold()
    seed_idx = load_seed_index()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if mode == "mock":
        searcher = make_fake_searcher(gold, seed_idx)
    elif mode == "snapshot":
        if not SNAPSHOT_PATH.exists():
            raise SystemExit(f"快照不存在：先跑 --mode live 生成 {SNAPSHOT_PATH}")
        snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        searcher = make_fake_searcher(gold, seed_idx, snapshot=snap)
    else:
        searcher = faq_biz._searcher  # live：真实检索

    # 评测工作库隔离：固定一次跑内唯一文件（时间戳），不污染 data/tickets.db
    ts = time.strftime("%Y%m%d_%H%M%S")
    work_db = (RESULTS_DIR / f"eval_work_{mode}_{ts}.db").resolve()
    db.configure(work_db)
    faq_biz._searcher = searcher

    records: list[dict] = []
    snapshot_log: dict = {}
    for item in gold:
        run_item(item, searcher, records, snapshot_log)

    m = compute_metrics(records)
    m["mode"] = mode
    m["ts"] = ts

    if write:
        if mode == "live":
            SNAPSHOT_PATH.write_text(json.dumps(snapshot_log, ensure_ascii=False, indent=1), encoding="utf-8")
        report_md = render_md(mode, gold, records, m, ts)
        (RESULTS_DIR / f"eval_report_{mode}_{ts}.md").write_text(report_md, encoding="utf-8")
        (RESULTS_DIR / f"eval_report_{mode}_{ts}.json").write_text(
            json.dumps({"metrics": m, "records": records}, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"mode={mode} n={m['n']} | intent_acc={m['intent_acc']:.3f} faq_resolved={m['faq_resolved']:.3f} "
          f"neg_blocked={m['faq_neg_blocked']:.3f} "
          f"cite_hit={m['faq_cite_hit_rate']:.3f} esc_f1={m['escalate_f1']:.4f} (tp{m['escalate_tp']}/fp{m['escalate_fp']}/fn{m['escalate_fn']}) "
          f"cat={m['diag_cat_acc']:.3f} pri={m['diag_pri_acc']:.3f} quality={m['quality_ok']}/25")
    if write:
        report_path = RESULTS_DIR / f"eval_report_{mode}_{ts}.md"
        print(f"报告: {report_path}")
    return m


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="M3 评估 runner")
    ap.add_argument("--mode", choices=["live", "mock", "snapshot"], default="live")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    main(mode=args.mode, write=not args.no_write)