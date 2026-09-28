"""FAQ 语料加载（M0 数据资产入口）。

语料规则（维护约定）：
- 版权安全：全部为自造客服问答，不引入任何第三方版权教材/文档；
- 每产品线 40 条，question 为标准问法，aliases 为口语化变体（评测集"问法等价"复用的来源）；
- tags 供规则版 Intent Router 关键词命中（M1 使用）。
"""
from __future__ import annotations

import json
from pathlib import Path

FAQ_SEED_PATH = Path(__file__).parent / "faq_seed.jsonl"
REQUIRED_FIELDS = ("id", "product", "category", "question", "answer", "aliases", "tags")


def load_faq_seed(path: Path | str | None = None) -> list[dict]:
    """读取 faq_seed.jsonl，逐行校验必需字段并去空白，返回记录列表。

    校验失败（字段缺失/空）直接抛错——语料是检索与评测的地基，宁缺勿烂。
    """
    src = Path(path) if path else FAQ_SEED_PATH
    if not src.exists():
        raise FileNotFoundError(f"FAQ 语料不存在: {src}")
    records: list[dict] = []
    seen: set[str] = set()
    for lineno, line in enumerate(src.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{src}:{lineno} JSON 解析失败: {exc}") from exc
        missing = [f for f in REQUIRED_FIELDS if not rec.get(f)]
        if missing:
            raise ValueError(f"{src}:{lineno} 缺少必填字段 {missing}: id={rec.get('id')}")
        if rec["id"] in seen:
            raise ValueError(f"{src}:{lineno} id 重复: {rec['id']}")
        seen.add(rec["id"])
        for field in ("aliases", "tags"):
            rec[field] = [str(x).strip() for x in rec[field] if str(x).strip()]
        if not rec["aliases"] or not rec["tags"]:
            raise ValueError(f"{src}:{lineno} aliases/tags 不能为空: {rec['id']}")
        rec["answer"] = rec["answer"].strip()
        records.append(rec)
    return records


def faq_by_id(records: list[dict]) -> dict[str, dict]:
    """id → record 索引（查引用/去重用）。"""
    return {rec["id"]: rec for rec in records}