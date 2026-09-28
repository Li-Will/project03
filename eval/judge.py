"""规则版 judge：引用归因 + 方案质量判定（确定性，无 LLM）。

计划书原设计为 LLM-as-judge（方案质量 + 引用归因，人工抽 10 条校准）。
当前无 LLM key（规则版系统，LLM 双模式是 M2 之后计划），本模块以
确定性规则替代，人工校准职责并入 gold_set 标注（每条带 note 标注理由，
口径为「谁标注谁负责」）。若启用 LLM 模式，本模块换为 LLM-as-judge 即可，
规则版结果可作基线对照（诚实分两层）。

界定（面试口径）：
- 引用归因 = 方案文本里出现的每个 [n] 都能在 citations 列表找到唯一对应项；
- 方案质量 = 必须包含结论/方案句 + 至少 1 条可归因引用；转人工则必须有 reason。
"""
from __future__ import annotations

import re

_N = re.compile(r"\[(\d+)\]")


def cite_attribution(citations: list[dict], solution: str) -> dict:
    """方案文本中出现的 [n] 是否都能对应 citations 里的条目。"""
    cited = sorted({int(m) for m in _N.findall(solution or "")})
    valid = {c["n"] for c in (citations or [])}
    missing = [n for n in cited if n not in valid]
    return {
        "ok": not missing,
        "cited": cited,
        "missing": missing,
    }


def solution_quality(solution: str, citations: list[dict], *, escalate: bool, escalate_reason: str = "") -> dict:
    """规则版方案质量。

    非转人工：必须有结论句 + 可归因引用（answer 有据可查）；
    转人工：本就不该生成方案（客服铁律：不编造），只要求 reason 可审计。
    """
    checks: dict[str, bool] = {}
    if escalate:
        checks["escalate_has_reason"] = bool(escalate_reason)
    else:
        checks["has_solution"] = bool(solution and solution.strip())
        checks["has_conclusion"] = ("排查结论" in (solution or "")) or ("处理方案" in (solution or "")) or ("建议" in (solution or ""))
        checks["has_citation"] = bool(citations)
        checks["attribution_ok"] = cite_attribution(citations, solution)["ok"]
    return {"ok": all(checks.values()), "checks": checks}


def grade_diagnose(item: dict, record: dict) -> dict:
    """单条诊断/转人工类的复合评分：语义主分（该转/不该转）+ 质量分。"""
    pred = record["predict_escalate"]
    gold = bool(item.get("escalate"))
    esc = {
        "tp": pred and gold,
        "fp": pred and not gold,
        "fn": (not pred) and gold,
        "tn": (not pred) and not gold,
    }
    esc_type = next(k for k, v in esc.items() if v)
    quality = solution_quality(
        record.get("solution", ""),
        record.get("citations", []),
        escalate=pred,
        escalate_reason=record.get("escalate_reason", ""),
    )
    return {
        "escalate_type": esc_type,
        "escalate_ok": pred == gold,
        "quality": quality,
    }