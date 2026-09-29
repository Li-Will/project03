"""M3 评估体系验收（mock 模式 = 逻辑链路自检，无 Qdrant/LLM 外部依赖）。

验收口径（docs/项目计划书.md §1.2）：
- 意图准确率 ≥ 90%（30 条）
- FAQ 解决率 ≥ 85%（25 条）
- 引用命中率 100%（直答且引用含 gold_id）
- 转人工 F1 ≥ 0.85（diagnose + escalate 共 25 条的人工标注）
mock 模式下检索按 gold 固定高分，逐条数字_应该_全绿——
若有失败即逻辑链路 bug（而非检索质量），这正是 mock 的意义。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

import run_eval  # noqa: E402

GOLD = run_eval.load_gold()


def test_gold_set_shape():
    """99 条、五类分布、关键字段齐备（P0-2 扩容：+15 条口语变体 + 4 条负例）。"""
    from collections import Counter

    c = Counter(it["cls"] for it in GOLD)
    assert c == {"intent": 30, "faq": 40, "diagnose": 15, "escalate": 10, "faq_negative": 4}
    for it in GOLD:
        assert it["text"].strip()
        if it["cls"] == "intent":
            assert it["gold"] in ("faq", "ticket", "complaint", "chat", "need_human")
        if it["cls"] == "faq":
            assert it["gold_id"].startswith(("cd-", "mn-"))
            assert it["direct"] is True
        if it["cls"] == "faq_negative":
            # 知识库外诉求：显式标注"期望不直答 + 期望转人工"，口径可复核
            assert it["expect_direct"] is False
            assert it["expect_escalate"] is True
        if it["cls"] in ("diagnose", "escalate"):
            assert "escalate" in it and isinstance(it["escalate"], bool)
        if it["cls"] == "diagnose":
            assert it["category"] in ("云盘服务", "会议支持", "综合")
            assert it["priority"] in ("P1", "P2", "P3")


def test_mock_acceptance_lines():
    """计划书验收线在 mock 模式必须全绿（逻辑链路自检）。"""
    m = run_eval.main(mode="mock", write=False)
    assert m["n"] == 99
    assert m["intent_acc"] >= 0.90, f"意图准确率 {m['intent_acc']} < 0.90"
    assert m["faq_resolved"] >= 0.85, f"FAQ 解决率 {m['faq_resolved']} < 0.85"
    assert m["faq_cite_hit_rate"] >= 0.999, f"引用命中率 {m['faq_cite_hit_rate']} < 100%"
    assert m["faq_neg_blocked"] >= 0.999, f"负例拦截率 {m['faq_neg_blocked']} < 100%"
    assert m["escalate_f1"] >= 0.85, f"转人工 F1 {m['escalate_f1']} < 0.85"
    assert m["diag_cat_acc"] >= 0.90, f"诊断分类 {m['diag_cat_acc']}"
    assert m["diag_pri_acc"] >= 0.90, f"诊断定级 {m['diag_pri_acc']}"
    assert m["quality_ok"] >= 23, f"方案质量通过 {m['quality_ok']}/25"


def test_snapshot_mode_requires_snapshot(monkeypatch, tmp_path):
    """snapshot 模式无快照文件时必须有清晰报错（防误用）。"""
    monkeypatch.setattr(run_eval, "SNAPSHOT_PATH", tmp_path / "no_snapshot.json")
    with pytest.raises(SystemExit):
        run_eval.main(mode="snapshot", write=False)


def test_judge_attribution_smoke():
    """引用归因：方案里的 [n] 必须可对应 citations；乱引号报 missing。"""
    import judge

    ok = judge.cite_attribution(
        [{"n": 1, "id": "cd-01"}, {"n": 2, "id": "mn-13"}],
        "结论[1] 与补充[2]",
    )
    assert ok["ok"] and ok["missing"] == []
    bad = judge.cite_attribution(
        [{"n": 1, "id": "cd-01"}],
        "结论[1] 与不存在引用[9]",
    )
    assert not bad["ok"] and bad["missing"] == [9]