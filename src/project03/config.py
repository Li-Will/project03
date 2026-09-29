"""应用配置：环境变量 → Settings 单一来源（对齐 project02 config.py 模式）。

- .env 不存在 / key 为空 → 规则分支确定性降级（核心链路不依赖 LLM）。
- 所有配置项在此声明，避免散落在各模块魔法字符串。
- 检索增强（混合检索 / 精排）与置信度阈值都可配置：**阈值必须能改，才能被数据校准**
  （见 scripts/tune_threshold.py），而不是拍一个数写死在代码里。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_cache_dir() -> str:
    """embedding / reranker 模型缓存：优先 HF_HOME（与 project01 复用同一份缓存）。"""
    return os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "p408qa")


class Settings(BaseSettings):
    # ===== LLM（OpenAI 兼容端点，可空 = 不使用 LLM，走规则分支）=====
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # ===== 检索（Qdrant）=====
    qdrant_mode: str = "local"          # local | server
    qdrant_path: Path = Path("data/qdrant")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "faq_articles"

    # ===== 检索增强（P0-2）=====
    # 索引文本字段策略：question | question+aliases | question+aliases+answer
    # 口语变体（aliases）是语料里已有的资产 —— 升级前没进索引，等于白存
    faq_index_fields: str = "question+aliases"
    recall_k: int = 20                  # 稠密粗排召回条数（精排候选来源）
    bm25_top_k: int = 20                # 关键词侧（字符 bigram BM25）召回条数
    use_bm25: bool = True               # 混合检索开关（RRF 融合稠密 + 关键词）
    use_rerank: bool = True             # Cross-Encoder 精排开关
    # 级联精排闸门：稠密 top1 余弦 ≥ 该值 → 跳过精排（省 ~470ms，见评测矩阵 G5）
    # 0 = 不启用级联（每个查询都精排）；0.60 = 与直答门槛同档（实测最优，见 docs 评测报告 G5b）
    rerank_gate: float = 0.60

    # 精排模型与性能参数（实测口径见 rag/rerank.py 注释）
    rerank_model: str = "jinaai/jina-reranker-v2-base-multilingual"
    rerank_threads: int = 4
    rerank_max_chars: int = 256
    # 精排对比文本的字段策略：question | question+aliases | question+aliases+answer
    # 实测：把 answer 全文塞进对比文本会稀释"问-问"匹配（见 docs 评测报告 G4/G4a/G4b）
    rerank_doc_fields: str = "question+aliases"
    rerank_batch_size: int = 16
    model_cache: str = _default_cache_dir()

    # 置信度阈值（直答 / 带人工提示）——**由 scripts/tune_threshold.py 扫描校准**，不手拍。
    # 实测（99 条 gold set，2026-09-29）：可行区间 [0.49, 0.62]（正例零漏放、负例零误放），
    # 取区间中点 0.55 作 hint（两侧留余量），direct 保持 0.60 与精排闸门同档。
    conf_direct: float = 0.60
    conf_hint: float = 0.55

    # ===== 业务 =====
    db_path: Path = Path("data/tickets.db")
    sla_scan_seconds: int = 300

    # ===== 鉴权与租户（P0-3）=====
    # 空值 = 不启用鉴权（本地演示/评测/测试默认路径，保持离线确定性）
    api_keys: str = ""                  # 形如 "key1:tenantA:agent,key2:tenantB:agent2"
    default_tenant: str = "default"     # 无鉴权模式下的租户归属
    require_auth: bool = False          # True = 所有业务端点必须带 X-API-Key

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()