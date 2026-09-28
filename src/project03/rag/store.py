"""FAQ 向量库（Qdrant 本地/服务端双模式，M0 验证 → M1 起作为检索工具）。

设计要点：
- local 模式：单文件目录（data/qdrant），零容器依赖，评测/演示默认路径；
- 稠密向量：fastembed 加载 bge-small-zh-v1.5（512 维，中文专用，CPU 秒级）；
- 幂等建集：ensure_collection(force=True) 可重建（评测基线隔离用）；
- 检索返回 payload 全文（answer 等），供 FAQ 直答与引用编号使用。
- 离线模型：cache_dir 读 HF_HOME（对齐 project01：~/.cache/p408qa +
  HF_HUB_OFFLINE=1 完全离线，避免 fastembed 联网 401）。
"""
from __future__ import annotations

import os
import time

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from project03.config import get_settings

EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
EMBED_DIM = 512

_embedder: TextEmbedding | None = None


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


def index_faq(records: list[dict], force: bool = False) -> dict:
    """把语料记录向量化并写入集合。返回写入统计（评分/评测可复现用）。"""
    st = get_settings()
    client = get_client()
    ensure_collection(client, force=force)
    model = get_embedder()

    texts = [f"{r['question']}\n{'+'.join(r.get('tags', []))}" for r in records]
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
            },
        )
        for idx, (rec, vector) in enumerate(zip(records, vectors))
    ]
    t0 = time.perf_counter()
    client.upsert(collection_name=st.qdrant_collection, points=points)
    elapsed = time.perf_counter() - t0
    return {"indexed": len(points), "elapsed_s": round(elapsed, 2), "collection": st.qdrant_collection}


def search_faq(query: str, top_k: int = 5, product: str | None = None) -> list[dict]:
    """FAQ 语义检索：query 向量 → 余弦 Top-K（可按产品线过滤）。"""
    st = get_settings()
    client = get_client()
    model = get_embedder()
    query_vector = next(iter(model.embed([query]))).tolist()
    flt = qm.Filter(must=[qm.FieldCondition(key="product", match=qm.MatchValue(value=product))]) if product else None
    hits = client.query_points(
        collection_name=st.qdrant_collection,
        query=query_vector,
        query_filter=flt,
        limit=top_k,
    ).points
    return [
        {
            "id": h.payload.get("id"),
            "product": h.payload.get("product"),
            "category": h.payload.get("category"),
            "question": h.payload.get("question"),
            "answer": h.payload.get("answer"),
            "score": round(h.score, 4),
        }
        for h in hits
    ]