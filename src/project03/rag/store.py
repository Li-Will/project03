"""FAQ 向量库（Qdrant 本地/服务端双模式）+ 混合检索 + 精排。

检索链路（P0-2 升级后）：
    稠密召回（bge-small-zh-v1.5，语义）
      + 关键词召回（字符 bigram BM25，见 rag/bm25.py，零依赖）
      → RRF 融合（粗排排序）
      → Cross-Encoder 精排（可选，见 rag/rerank.py）
      → Top-k

**阈值语义不变**：每条结果保留 `score` = 稠密余弦分（互为可比的"证据强度"），
精排分另存 `rerank_score`。直答/转人工门槛仍看稠密证据（见 biz/faq.py），
所以升级前后门槛口径可比 —— 排序换算法，判据不换。

索引文本策略由 `FAQ_INDEX_FIELDS` 决定（question / question+aliases / question+aliases+answer）：
语料里 `aliases`（口语化变体）是给检索用的资产，升级前没进索引等于白存。

其他设计要点：
- local 模式：单文件目录（data/qdrant），零容器依赖，评测/演示默认路径；
- 幂等建集：ensure_collection(force=True) 可重建（评测基线隔离用）；
- 检索返回 payload 全文（answer 等），供 FAQ 直答与引用编号使用；
- 离线模型：cache_dir 读 HF_HOME（对齐 project01：~/.cache/p408qa + HF_HUB_OFFLINE=1）。
"""
from __future__ import annotations

import os
import threading
import time

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from project03.config import get_settings
from project03.rag.bm25 import BM25Index, rrf_fuse

EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
EMBED_DIM = 512
RRF_K = 60

_embedder: TextEmbedding | None = None
_bm25_lock = threading.Lock()
_bm25_cache: dict[str, tuple[BM25Index, dict[str, dict]]] = {}   # collection → (索引, payload 表)


def get_embedder() -> TextEmbedding:
    """模块级懒加载单例：embedding 模型加载耗时（首次下载缓存），只做一次。

    cache_dir：优先读 HF_HOME（复用 project01 已下载的 Qdrant/bge-small-zh-v1.5
    模型缓存，配 HF_HUB_OFFLINE=1 可完全离线）；未设置时 fastembed 默认缓存。
    """
    global _embedder
    if _embedder is None:
        cache_dir = os.environ.get("HF_HOME") or None
        _embedder = TextEmbedding(EMBED_MODEL, cache_dir=cache_dir)
    return _embedder


def get_client() -> QdrantClient:
    st = get_settings()
    if st.qdrant_mode == "server":
        return QdrantClient(url=st.qdrant_url)
    return QdrantClient(path=str(st.qdrant_path))


def ensure_collection(client: QdrantClient | None = None, force: bool = False) -> bool:
    """确保集合存在；force=True 时重建（旧数据可被覆盖，评测隔离用）。"""
    st = get_settings()
    client = client or get_client()
    name = st.qdrant_collection
    exists = client.collection_exists(name)
    if exists and not force:
        return False
    if exists:
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config=qm.VectorParams(size=EMBED_DIM, distance=qm.Distance.COSINE),
    )
    return True


def index_text(rec: dict) -> str:
    """按 `FAQ_INDEX_FIELDS` 生成参与向量化的文本（默认：标准问 + 口语变体 + 标签）。"""
    fields = get_settings().faq_index_fields
    parts: list[str] = [rec["question"]]
    if "aliases" in fields:
        parts.extend(rec.get("aliases", []))
    if "answer" in fields:
        parts.append(rec["answer"])
    tags = rec.get("tags") or []
    if tags:
        parts.append("+".join(tags))
    return "\n".join(p for p in parts if p)


def index_faq(records: list[dict], force: bool = False) -> dict:
    """把语料记录向量化并写入集合。返回写入统计（评分/评测可复现用）。"""
    st = get_settings()
    client = get_client()
    ensure_collection(client, force=force)
    model = get_embedder()

    texts = [index_text(r) for r in records]
    vectors = list(model.embed(texts))
    points = [
        qm.PointStruct(
            id=idx + 1,  # 1..N 稳定序号，演示/评测可复现
            vector=vector.tolist(),
            payload={
                "id": rec["id"],
                "product": rec["product"],
                "category": rec["category"],
                "question": rec["question"],
                "answer": rec["answer"],
                "tags": rec.get("tags", []),
                # 以下两字段让混合检索（BM25 侧）与索引文本口径一致，无需回读语料文件
                "aliases": rec.get("aliases", []),
                "index_text": texts[idx],
            },
        )
        for idx, (rec, vector) in enumerate(zip(records, vectors))
    ]
    t0 = time.perf_counter()
    client.upsert(collection_name=st.qdrant_collection, points=points)
    elapsed = time.perf_counter() - t0
    _bm25_cache.pop(st.qdrant_collection, None)   # 索引变了 → 关键词侧缓存失效
    return {
        "indexed": len(points),
        "elapsed_s": round(elapsed, 2),
        "collection": st.qdrant_collection,
        "index_fields": st.faq_index_fields,
    }


def _get_bm25(client: QdrantClient) -> tuple[BM25Index, dict[str, dict]]:
    """构建（并缓存）关键词侧索引：从集合 payload 取索引文本，进程内只建一次。"""
    st = get_settings()
    key = st.qdrant_collection
    with _bm25_lock:
        cached = _bm25_cache.get(key)
        if cached is not None:
            return cached
        payloads: dict[str, str] = {}
        by_id: dict[str, dict] = {}
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=key, limit=256, offset=offset, with_payload=True, with_vectors=False
            )
            for p in points:
                pid = str((p.payload or {}).get("id"))
                payloads[pid] = (p.payload or {}).get("index_text") or (p.payload or {}).get("question", "")
                by_id[pid] = p.payload or {}
            if offset is None:
                break
        built = (BM25Index(payloads), by_id)
        _bm25_cache[key] = built
        return built


def _to_hit(payload: dict, cosine: float | None, agg: float | None = None) -> dict:
    return {
        "id": payload.get("id"),
        "product": payload.get("product"),
        "category": payload.get("category"),
        "question": payload.get("question"),
        "answer": payload.get("answer"),
        "aliases": payload.get("aliases", []),
        # score = 稠密余弦（证据强度，门槛判据）；BM25-only 命中没有余弦分 → 0.0
        "score": round(cosine, 4) if cosine is not None else 0.0,
        "rank_score": round(agg, 6) if agg is not None else None,
    }


def search_faq(query: str, top_k: int = 5, product: str | None = None) -> list[dict]:
    """FAQ 混合检索：稠密 + 关键词 → RRF 融合 →（可选）精排 → Top-k。"""
    st = get_settings()
    client = get_client()
    model = get_embedder()
    query_vector = next(iter(model.embed([query]))).tolist()
    flt = (
        qm.Filter(must=[qm.FieldCondition(key="product", match=qm.MatchValue(value=product))])
        if product else None
    )

    dense_hits = client.query_points(
        collection_name=st.qdrant_collection,
        query=query_vector,
        query_filter=flt,
        limit=st.recall_k,
    ).points
    dense_payload: dict[str, dict] = {}
    dense_cos: dict[str, float] = {}
    dense_ranking: list[str] = []
    for h in dense_hits:
        pid = str((h.payload or {}).get("id"))
        dense_payload[pid] = h.payload or {}
        dense_cos[pid] = float(h.score)
        dense_ranking.append(pid)

    rankings: list[list[str]] = [dense_ranking]
    kw_payload: dict[str, dict] = {}
    if st.use_bm25:
        bm25_index, by_id = _get_bm25(client)
        kw_scores = bm25_index.score(query, top_k=st.bm25_top_k)
        kw_ranking = [pid for pid, _ in kw_scores if product is None or by_id.get(pid, {}).get("product") == product]
        if kw_ranking:
            rankings.append(kw_ranking)
        kw_payload = by_id

    fused = rrf_fuse(rankings, k=RRF_K)
    if not fused:
        return []

    # 精排候选：融合排名前 N（N=recall_k）；未启用精排则直接取 top_k
    cand_n = st.recall_k if st.use_rerank else top_k
    candidates: list[dict] = []
    for pid, agg in fused[:cand_n]:
        payload = dense_payload.get(pid) or kw_payload.get(pid) or {}
        if not payload:
            continue
        candidates.append(_to_hit(payload, dense_cos.get(pid), agg))

    # 级联精排（cascade）：稠密 top1 已足够自信时跳过精排 —— 常见问题保持毫秒级，
    # 疑难/模糊查询才付精排的代价（评测矩阵 G5 量化了这条闸门对延迟与质量的影响）
    if st.use_rerank and len(candidates) > 1:
        confident = st.rerank_gate > 0 and (candidates[0].get("score") or 0.0) >= st.rerank_gate
        if not confident:
            from project03.rag.rerank import rerank

            candidates = rerank(query, candidates, top_k=top_k)
    return candidates[:top_k]