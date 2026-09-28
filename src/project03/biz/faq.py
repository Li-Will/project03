"""FAQ Answerer：检索 + 置信度分档 → 直答 / 直答带人工提示 / 转人工。

设计（面试口径：客服场景 = 幻觉零容忍）：
- 置信度三档（分数来自 Qdrant cosine）：
  - score >= 0.60 ：直接回答（高置信）；
  - 0.50 <= score < 0.60 ：回答但附"如需人工"提示（中置信，不硬答细节）；
  - score < 0.50   ：不编 ## 转人工（低置信兜底，宁可转人也不答错）；
- 引用编号 [n]：答案一律带来源（为 M3 RA-3 引用归因评测铺路）；
- 产品线识别：文本含产品专属词时限定检索产品（降低跨库歧义）；
- 检索器可注入（_searcher 模块级）——单测用 fake，CI 无需 Qdrant。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from project03.rag import store

CONF_DIRECT = 0.60    # ≥：直接回答
CONF_HINT = 0.50      # ≥：回答 + 人工提示；<：转人工

_CLOUD_KW = ("云盘", "上传", "下载", "同步", "分享", "容量", "存储", "备份", "相册", "网盘")
_MEET_KW = ("会议", "摄像头", "麦克风", "屏幕共享", "录制", "字幕", "入会", "视频会议", "音频")

# 检索器可注入点（测试替换为 fake）
_searcher = store.search_faq


@dataclass
class FaqResult:
    direct: bool                 # True=直答（含带提示档）；False=转人工
    confidence: float            # top1 分数
    answer: str = ""             # 拼好引用编号的答复文案
    citations: list[dict] = field(default_factory=list)  # [{n, id, question, ...}]
    top_hit: dict | None = None


def detect_product(text: str) -> str | None:
    """产品线识别：都命中多个时按词数多的算；都不中返回 None（不过滤）。"""
    cloud_hits = sum(1 for k in _CLOUD_KW if k in text)
    meet_hits = sum(1 for k in _MEET_KW if k in text)
    if meet_hits > cloud_hits:
        return "meetnow"
    if cloud_hits > 0:
        return "clouddrive"
    return None


def _format_answer(citations: list[dict], with_hint: bool) -> str:
    lines = [c["answer"] for c in citations[:1]]  # M1 取 top1 答案（M2 诊断合并多源）
    if with_hint:
        lines.append("如需更详细的人工支持，可回复「转人工」由客服跟进。")
    return "\n\n".join(lines)


def answer_faq(text: str) -> FaqResult:
    """FAQ 直答入口：检索 top3 → 按顶级分档决策。"""
    query = (text or "").strip()
    hits = _searcher(query, top_k=3, product=detect_product(text))
    if not hits:
        return FaqResult(direct=False, confidence=0.0)

    top = hits[0]
    conf = float(top["score"])
    citations = [{"n": i + 1, **h} for i, h in enumerate(hits)]

    if conf >= CONF_DIRECT:
        return FaqResult(
            direct=True, confidence=conf,
            answer=_format_answer(citations, with_hint=False), citations=citations, top_hit=top,
        )
    if conf >= CONF_HINT:
        return FaqResult(
            direct=True, confidence=conf,
            answer=_format_answer(citations, with_hint=True), citations=citations, top_hit=top,
        )
    # 低置信：不编造，转人工（由 API 层建单 escalate）
    return FaqResult(direct=False, confidence=conf, citations=citations, top_hit=top)