"""字符 bigram BM25 —— 中文关键词侧召回（混合检索的稀疏侧，零新依赖）。

为什么不用 `fastembed` 的 `Qdrant/bm25` 稀疏模型（实测结论，2026-09-29）：
  · 它的 tokenizer 是 `SimpleTokenizer`（空白/标点切分）+ 强制 `SnowballStemmer(language)`，
    而 snowball 不提供中文词干器 → 中文没有可用语言档；
  · 本机模型缓存 `~/.cache/p408qa/local/bm25` 缺 `chinese.txt`（只有 18 种西文/阿语词表）；
  · 强行加载后实测：中文文档只产出 **1 个非零项**（整句被当成一个 token），
    `search_faq` 的稀疏路命中为空 —— 混合检索若接它会"看着是混合、实际只有稠密在干活"。

自实现字符 bigram 是 CJK 检索的常规做法：不依赖分词器与词典，
对术语 / 编号 / 专有名词（"5GB"、"cd-01"、"三次握手"）这类必须精确匹配的查询有效。

与稠密（语义）侧互补后交给 RRF 融合：本模块只负责"关键词侧召回"，不改变阈值语义。
"""
from __future__ import annotations

import math
import re
from collections import Counter

K1 = 1.2      # 词频饱和系数（BM25 默认）
B = 0.75      # 文档长度归一化（BM25 默认）

_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*|\d+(?:\.\d+)?")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """中文按字符 bigram（附单字）+ 英文/数字按词小写 —— 无分词器依赖。"""
    t = (text or "").lower()
    tokens: list[str] = [m.group(0) for m in _ASCII_WORD.finditer(t)]

    cjk = _CJK.findall(t)
    tokens.extend(cjk)                                            # 单字：兜住单字查询
    tokens.extend("".join(pair) for pair in zip(cjk, cjk[1:]))    # bigram：主召回单元
    return tokens


class BM25Index:
    """内存 BM25（语料规模小：几十~几千条，构建与查询都是微秒级）。"""

    def __init__(self, docs: dict[str, str]) -> None:
        self._ids: list[str] = []
        self._tf: dict[str, Counter] = {}
        self._len: dict[str, int] = {}
        self._df: Counter = Counter()
        for doc_id, text in docs.items():
            tf = Counter(tokenize(text))
            self._ids.append(doc_id)
            self._tf[doc_id] = tf
            self._len[doc_id] = sum(tf.values()) or 1
            for term in tf:
                self._df[term] += 1
        self.n = len(self._ids)
        self.avg_len = (sum(self._len.values()) / self.n) if self.n else 1.0

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """返回 [(doc_id, bm25_score)] 降序；全零分不入榜（避免噪声注入 RRF）。"""
        q_terms = set(tokenize(query))
        if not q_terms or not self.n:
            return []
        scored: list[tuple[str, float]] = []
        for doc_id in self._ids:
            tf = self._tf[doc_id]
            dl = self._len[doc_id]
            s = 0.0
            for term in q_terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                s += self._idf(term) * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / self.avg_len))
            if s > 0:
                scored.append((doc_id, s))
        scored.sort(key=lambda kv: -kv[1])
        return scored[:top_k]


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """RRF 融合多路排名（1/(k+rank)），返回 [(doc_id, rrf_score)] 降序。

    与 project01 的融合口径一致（粗排融合、精排在后），k=60 为 RRF 论文默认值。
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(fused.items(), key=lambda kv: -kv[1])