"""Cross-Encoder 精排（Rerank）—— 粗排召回后的相关性重排。

为什么需要：稠密检索是"双塔"结构（query 与 doc 各自编码后比相似度），精度有限；
Cross-Encoder 把 (query, doc) 拼在一起过一遍模型，能捕捉细粒度相关性，
显著提升 Top-1 排序质量。代价是计算量随候选数线性增长 →
流程固定为"粗排召回 → 精排取 Top-k"。

阈值语义不变：精排只改**排序**，不参与"直答/转人工"门槛判定
（门槛仍用稠密 top1 分数，口径与升级前可比）。精排分数另存 `rerank_score` 供评测校准。

性能（i7-12800HX / 无 GPU，与 project01 同机同模型）：
  ONNX Runtime 默认线程数过低会慢几十倍 → 显式 threads；输入截断 256 字符（注意力复杂度平方增长）。
"""
from __future__ import annotations

from project03.config import get_settings

_reranker = None


def get_reranker():
    """模块级懒加载：模型加载耗时只付一次。"""
    global _reranker
    if _reranker is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        st = get_settings()
        _reranker = TextCrossEncoder(
            model_name=st.rerank_model,
            cache_dir=st.model_cache,
            threads=st.rerank_threads,
        )
    return _reranker


def rerank(query: str, hits: list[dict], top_k: int = 3) -> list[dict]:
    """对候选 hits（含 question/answer）重排：写回 rerank_score，返回按精排分降序的前 top_k。"""
    st = get_settings()
    if not hits:
        return []

    fields = st.rerank_doc_fields

    def _doc(h: dict) -> str:
        parts = [h.get("question", "")]
        if "aliases" in fields:
            parts.append(" ".join(h.get("aliases") or []))
        if "answer" in fields:
            parts.append(h.get("answer", ""))
        return " ".join(p for p in parts if p)[: st.rerank_max_chars]

    docs = [_doc(h) for h in hits]
    try:
        scores = [float(s) for s in get_reranker().rerank(query, docs, batch_size=st.rerank_batch_size)]
    except Exception:  # noqa: BLE001 —— 精排是增强项：模型不可用时应降级为原序，而不是让检索失败
        return hits[:top_k]

    ranked = sorted(zip(hits, scores), key=lambda kv: -kv[1])
    out: list[dict] = []
    for h, s in ranked[:top_k]:
        item = dict(h)
        item["rerank_score"] = round(s, 4)
        out.append(item)
    return out