"""M0 数据资产冒烟测试：语料完整性 + 配置默认值（离线可跑，CI 友好）。"""
from __future__ import annotations

from project03.biz.seed import faq_by_id, load_faq_seed
from project03.config import get_settings


def test_seed_total_and_products():
    records = load_faq_seed()
    assert len(records) == 80
    by_product = {}
    for r in records:
        by_product[r["product"]] = by_product.get(r["product"], 0) + 1
    assert sorted(by_product) == ["clouddrive", "meetnow"]
    assert by_product["clouddrive"] == 40
    assert by_product["meetnow"] == 40


def test_seed_fields_complete():
    records = load_faq_seed()
    for r in records:
        assert len(r["question"]) >= 5
        assert len(r["answer"]) >= 30, f"{r['id']} answer 过短，无引用价值"
        assert r["aliases"], f"{r['id']} 缺少口语化变体"
        assert r["tags"], f"{r['id']} 缺少规则标签"
        assert r["category"], f"{r['id']} 缺少分类"


def test_seed_id_unique_and_indexer():
    records = load_faq_seed()
    index = faq_by_id(records)
    assert len(index) == len(records)
    assert index["cd-01"]["question"].startswith("免费用户")


def test_config_defaults_without_env():
    """无 .env（CI/演示环境）时默认值可加载，核心链路不依赖外部配置。"""
    st = get_settings()
    assert st.qdrant_mode == "local"
    assert str(st.qdrant_path) == "data/qdrant"
    assert st.qdrant_collection == "faq_articles"
    assert st.db_path == get_settings().db_path