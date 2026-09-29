"""置信度阈值校准：从评测记录扫描"直答/转人工"门槛，不手拍。

为什么要脚本：`CONF_HINT` 直接决定两件互斥的事 —— 正例被漏放（该答的转了人工）与
负例被误放（知识库外诉求被硬答）。凭感觉设 0.5 是"看起来舒服"，用数据扫才是判据。

口径：
- 正例（cls=faq）：要求 top_conf >= CONF_HINT（至少"带人工提示直答"），否则算漏放；
- 负例（cls=faq_negative）：要求 top_conf <  CONF_HINT，否则算误放（不该答的答了）；
- 可行区间 = 同时满足"漏放 0 且误放 0"的阈值集合；推荐值取区间中点。

用法：
    python scripts/tune_threshold.py                      # 用最新一份 live 评测报告
    python scripts/tune_threshold.py --report eval/results/eval_report_live_xxx.json
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STEP = 0.01


def load_records(report: str | None) -> tuple[str, list[dict]]:
    if report:
        path = Path(report)
    else:
        cands = sorted(glob.glob(str(ROOT / "eval" / "results" / "eval_report_live_*.json")))
        if not cands:
            raise SystemExit("没有 live 评测报告可分析：先跑 `python eval/run_eval.py --mode live`")
        path = Path(cands[-1])
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(path), data["records"]


def main() -> int:
    ap = argparse.ArgumentParser(description="置信度阈值校准（基于评测记录扫描）")
    ap.add_argument("--report", default=None, help="评测报告 json 路径（默认最新一份 live）")
    ap.add_argument("--lo", type=float, default=0.30)
    ap.add_argument("--hi", type=float, default=0.70)
    args = ap.parse_args()

    path, records = load_records(args.report)
    pos = [r["top_conf"] for r in records if r["cls"] == "faq"]
    neg = [r["top_conf"] for r in records if r["cls"] == "faq_negative"]
    if not pos or not neg:
        raise SystemExit(f"报告缺少 faq / faq_negative 记录（报告={path}）")

    print(f"报告：{path}")
    print(f"正例 {len(pos)} 条：min={min(pos):.4f}  p10={sorted(pos)[max(0, len(pos)//10)]:.4f}  max={max(pos):.4f}")
    print(f"负例 {len(neg)} 条：min={min(neg):.4f}              max={max(neg):.4f}")
    print("")
    print(f"{'阈值':>6s} {'漏放(正例转人工)':>16s} {'误放(负例被硬答)':>16s}")
    feasible: list[float] = []
    rows: list[tuple[float, int, int]] = []
    h = args.lo
    while h <= args.hi + 1e-9:
        thr = round(h, 2)
        miss = sum(1 for c in pos if c < thr)
        leak = sum(1 for c in neg if c >= thr)
        rows.append((thr, miss, leak))
        if miss == 0 and leak == 0:
            feasible.append(thr)
        h += STEP

    for thr, miss, leak in rows:
        mark = " <- 可行" if (miss == 0 and leak == 0) else ""
        if thr in (args.lo, round((args.lo + args.hi) / 2, 2), args.hi) or mark:
            print(f"{thr:6.2f} {miss:16d} {leak:16d}{mark}")

    print("")
    if feasible:
        rec = round((min(feasible) + max(feasible)) / 2, 2)
        print(f"可行阈值区间：[{min(feasible):.2f}, {max(feasible):.2f}]（漏放 0 且误放 0）")
        print(f"推荐 CONF_HINT = {rec}（区间中点，两侧都留余量）")
        print(f"对照 CONF_DIRECT：建议 ≥ {rec:.2f}，且不高于正例 p10（{sorted(pos)[max(0, len(pos)//10)]:.2f}）")
    else:
        print("可行区间为空：正例最低分与负例最高分重叠 → 单靠阈值无法同时满足，")
        print("需要改检索（加召回/权重）或改判据（如负例用独立的高危判定，见 biz/safety.py）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())