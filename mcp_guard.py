#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Guard v1.0 — защита от повторов и галлюцинаций (офлайн).

Два инструмента для повышения надёжности ответов нейросети:
  • check_repetition — обнаруживает, что новый ответ почти дублирует недавний
    ответ ассистента (защита от зацикливания/повторов).
  • check_grounding — проверяет, есть ли в памяти подтверждение утверждения
    (защита от галлюцинаций: помечает неподтверждённые/противоречивые заявления).

Использует difflib (stdlib) — без интернета и тяжёлых зависимостей.
"""
import re
import json
import difflib
from typing import Dict, List, Optional, Any

from mcp_shared import BaseMCPServer, _log, conversation_memory, dialog_ctx


def _norm(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or "").strip().lower())


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def check_repetition(text: str, dialog_id: Optional[str] = None,
                     threshold: float = 0.92, lookback: int = 20) -> Dict[str, Any]:
    """
    Сравнивает text с последними ответами ассистента в памяти. Если сходство
    >= threshold — это вероятный повтор (модель зациклилась). Защита от повторов.
    """
    d_id = dialog_id or dialog_ctx.get()
    norm_new = _norm(text)
    if len(norm_new) < 8:
        return {"status": "success", "is_repetition": False, "reason": "слишком короткий текст"}

    conn = conversation_memory._open_conn()
    try:
        # последние записи ассистента (или диалога), новые сверху
        params = []
        where = "op = 'conversation' AND context IS NOT NULL AND TRIM(context) != ''"
        if d_id:
            where += " AND dialog = ?"
            params.append(d_id)
        rows = conn.execute(
            f"SELECT id, context, paths_json, ts FROM entries WHERE {where} "
            f"ORDER BY ts DESC LIMIT ?",
            (*params, max(1, lookback))
        ).fetchall()
    finally:
        conn.close()

    best = {"sim": 0.0, "id": None, "text": None}
    for r in rows:
        # приоритет — записи ассистента
        pj = r["paths_json"] or ""
        if "assistant" not in pj:
            continue
        sim = _similar(norm_new, _norm(r["context"]))
        if sim > best["sim"]:
            best = {"sim": sim, "id": r["id"], "text": r["context"]}

    is_rep = best["sim"] >= threshold
    return {
        "status": "success",
        "is_repetition": is_rep,
        "similarity": round(best["sim"], 4),
        "threshold": threshold,
        "matched_entry_id": best["id"] if is_rep else None,
        "matched_preview": (best["text"] or "")[:160] if is_rep else None,
        "advice": "Ответ почти дублирует недавний — переформулируйте или добавьте новое."
                  if is_rep else "Повтор не обнаружен.",
    }


def check_grounding(claim: str, dialog_id: Optional[str] = None,
                    min_similarity: float = 0.55) -> Dict[str, Any]:
    """
    Проверяет, подтверждается ли утверждение памятью (защита от галлюцинаций).
    Возвращает verdict: supported / contradicted / unknown.
      • supported   — есть похожий факт со статусом verified/unverified;
      • contradicted — похожий факт помечен deprecated/contradicted;
      • unknown      — подтверждений в памяти нет (возможна галлюцинация — осторожно).
    """
    norm_claim = _norm(claim)
    if len(norm_claim) < 8:
        return {"status": "success", "verdict": "unknown", "reason": "слишком короткое утверждение"}

    conn = conversation_memory._open_conn()
    try:
        rows = conn.execute(
            "SELECT id, context, confidence, verification_status FROM entries "
            "WHERE memory_type = 'fact' AND context IS NOT NULL AND TRIM(context) != '' "
            "ORDER BY ts DESC LIMIT 500"
        ).fetchall()
    finally:
        conn.close()

    support, against = [], []
    for r in rows:
        sim = _similar(norm_claim, _norm(r["context"]))
        if sim < min_similarity:
            continue
        item = {
            "entry_id": r["id"],
            "similarity": round(sim, 4),
            "confidence": r["confidence"],
            "status": r["verification_status"],
            "preview": (r["context"] or "")[:160],
        }
        if r["verification_status"] in ("deprecated", "contradicted"):
            against.append(item)
        else:
            support.append(item)

    support.sort(key=lambda x: -x["similarity"])
    against.sort(key=lambda x: -x["similarity"])

    if support:
        verdict = "supported"
    elif against:
        verdict = "contradicted"
    else:
        verdict = "unknown"

    advice = {
        "supported": "Утверждение подтверждается памятью.",
        "contradicted": "Память содержит ПРОТИВОРЕЧАЩИЙ/устаревший факт — перепроверьте.",
        "unknown": "Подтверждений в памяти нет — возможна галлюцинация, будьте осторожны или проверьте источники.",
    }[verdict]

    return {
        "status": "success",
        "claim": claim,
        "verdict": verdict,
        "advice": advice,
        "support": support[:5],
        "contradictions": against[:5],
    }


def register_tools(server: BaseMCPServer):
    server.register_tool("check_repetition", {
        "description": "Detect if a candidate reply nearly duplicates a recent assistant reply (loop/repeat "
                       "guard). Returns similarity and whether it's a repetition. Offline.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Candidate reply to check"},
            "dialog_id": {"type": "string"},
            "threshold": {"type": "number", "description": "Similarity threshold 0..1 (default 0.92)"},
            "lookback": {"type": "integer", "description": "How many recent entries to scan (default 20)"}
        }, "required": ["text"]}
    }, lambda **kw: check_repetition(kw["text"], kw.get("dialog_id"), kw.get("threshold", 0.92), kw.get("lookback", 20)))

    server.register_tool("check_grounding", {
        "description": "Anti-hallucination check: is a claim supported by memory? Returns verdict "
                       "supported/contradicted/unknown with matching facts. Offline.",
        "inputSchema": {"type": "object", "properties": {
            "claim": {"type": "string"},
            "dialog_id": {"type": "string"},
            "min_similarity": {"type": "number", "description": "Match threshold 0..1 (default 0.55)"}
        }, "required": ["claim"]}
    }, lambda **kw: check_grounding(kw["claim"], kw.get("dialog_id"), kw.get("min_similarity", 0.55)))


__mcp_plugin__ = {
    "name": "guard",
    "version": "1.0.0",
    "description": "Anti-repetition and anti-hallucination guards (check_repetition, check_grounding)",
    "dependencies": [],
    "on_load": lambda: _log("[guard] v1.0 loaded — tools: check_repetition, check_grounding"),
}

if __name__ == "__main__":
    print(json.dumps({"loaded": True}, indent=2))
