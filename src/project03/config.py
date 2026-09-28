"""应用配置：环境变量 → Settings 单一来源（对齐 project02 config.py 模式）。

- .env 不存在 / key 为空 → 规则分支确定性降级（核心链路不依赖 LLM）。
- 所有配置项在此声明，避免散落在各模块魔法字符串。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ===== 业务 =====
    db_path: Path = Path("data/tickets.db")
    sla_scan_seconds: int = 300

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()