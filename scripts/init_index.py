"""M0 验证脚本：建集 + 索引 80 条 FAQ + 检索样例。

用法：
    uv run python scripts/init_index.py                 # 幂等：已建集则跳过索引
    uv run python scripts/init_index.py --force          # 重建集合并重新索引
    uv run python scripts/init_index.py --query "免费空间多大"   # 建集后现场检索演示
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project03.biz.seed import load_faq_seed  # noqa: E402
from project03.rag import store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="FAQ 向量索引初始化与检索验证")
    parser.add_argument("--force", action="store_true", help="重建集合并重新索引")
    parser.add_argument("--query", type=str, default=None, help="建集后现场检索该问题")
    args = parser.parse_args()

    records = load_faq_seed()
    print(f"[语料] 加载 {len(records)} 条 FAQ（{len(set(r['product'] for r in records))} 个产品线）")

    result = store.index_faq(records, force=args.force)
    print(f"[索引] {result['indexed']} 条写入 集合={result['collection']} 耗时={result['elapsed_s']}s")

    if args.query:
        hits = store.search_faq(args.query, top_k=3)
        print(f"\n[检索] query: {args.query}")
        for h in hits:
            print(f"  #{h['score']:.4f} [{h['product']}/{h['category']}] {h['id']} {h['question']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())