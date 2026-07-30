#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Context Manager v3.0 (Context-Isolated)
Dialog compression, text chunking, and persistent fact recall
with conversation memory integration.
Uses contextvars for automatic dialog isolation.
"""
import os
import sys
import re
import json
from collections import Counter
from typing import List, Dict, Any
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Common NLP Resources (Extracted for maintainability) ────────────────────
STOP_WORDS = {
    'the','a','an','is','are','was','were','in','on','at','to','of','for',
    'with','and','or','it','that','this','be','as','by','not','but','from',
    'has','have','had','will','would','can','could','should','may','might',
    'do','does','did','been','being','am','so','if','then','than','only',
    'just','also','very','too','much','many','more','most','some','any',
    'no','yes','ok','well','oh','ah','um','uh','like','you','i','we','they',
    'he','she','his','her','its','our','their','my','me','us','them','him'
}
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?…])\s+')
_WORD_SPLIT = re.compile(r'\b\w+\b')

# ─── Compress History ────────────────────────────────────────────────────────
def compress_history(history: List[str], max_sentences: int = 10,
                     dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    if not history:
        return {"summary": "", "original_sentences": 0, "note": "Empty history"}
    
    text = " ".join(history)
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    
    if len(sentences) <= max_sentences:
        summary = " ".join(sentences)
        conversation_memory.add(
            op="compress_history",
            paths={"dialog": d_id},
            status="under_limit",
            dialog=d_id,
            context=f"Summary (under limit): {summary[:200]}"
        )
        return {
            "summary": summary,
            "original_sentences": len(sentences),
            "summary_sentences": len(sentences),
            "note": "Under limit, returned as-is"
        }

    # TF-IDF-like scoring
    words = _WORD_SPLIT.findall(text.lower())
    freq = Counter(w for w in words if w not in STOP_WORDS and len(w) > 2)
    
    def score(sent: str) -> float:
        sent_words = _WORD_SPLIT.findall(sent.lower())
        if not sent_words:
            return 0.0
        score_val = sum(freq.get(w, 0) for w in sent_words if w not in STOP_WORDS)
        return score_val / max(len(sent_words), 1)

    scored = [(i, s, score(s)) for i, s in enumerate(sentences)]
    scored.sort(key=lambda x: x[2], reverse=True)
    
    top_indices = {x[0] for x in scored[:max_sentences]}
    selected = [sentences[i] for i in sorted(top_indices)]
    summary = " ".join(selected)
    
    conversation_memory.add(
        op="compress_history",
        paths={"dialog": d_id},
        status="compressed",
        dialog=d_id,
        context=f"Compressed {len(sentences)} sentences to {len(selected)}. Summary: {summary[:300]}"
    )
    return {
        "summary": summary,
        "original_sentences": len(sentences),
        "summary_sentences": len(selected),
        "compression_ratio": round(len(selected) / len(sentences), 2),
        "note": "Extractive summarization with TF scoring"
    }

# ─── Split into Chunks ───────────────────────────────────────────────────────
def split_into_chunks(text: str, chunk_size: int = 1000, overlap: int = 100) -> List[str]:
    if not text:
        return []
    if chunk_size <= 0:
        chunk_size = 1000
    if overlap < 0:
        overlap = 0
    if overlap >= chunk_size:
        overlap = chunk_size // 4
        
    chunks = []
    start = 0
    text_len = len(text)
    step = chunk_size - overlap
    
    while start < text_len:
        end = min(start + chunk_size, text_len)
        if end < text_len:
            search_start = max(start + int(chunk_size * 0.8), start + 1)
            search_text = text[search_start:end]
            match = re.search(r'[.!?…]\s+', search_text)
            if match:
                end = search_start + match.end()
        chunks.append(text[start:end])
        start += step
        if start >= text_len:
            break
        if start <= 0:
            start = end
    return chunks

# ─── Recall Fact (ENHANCED: search by keywords in context) ───────────────────
def _recall_stems(query: str):
    """Основы слов запроса (терпимо к русской морфологии): первые 6 символов слов >=4."""
    return [w[:6] for w in re.findall(r"\w+", (query or "").lower()) if len(w) >= 4]


def _recall_type_score(entry: dict) -> float:
    """Полезность записи как 'вспомненного факта': решения/факты/ответы выше вопросов."""
    op = entry.get("op") or ""
    paths = entry.get("paths") or {}
    role = paths.get("role", "") if isinstance(paths, dict) else ""
    s = 0.0
    if op not in ("conversation",):          # намеренно сохранённый факт/решение
        s += 3.0
    if entry.get("memory_type") == "fact":
        s += 2.0
    if role == "assistant":
        s += 1.5
    if role == "user":                       # сам вопрос — наименее полезен для recall
        s -= 2.0
    try:
        s += float(entry.get("confidence") or 0) * 0.5
    except Exception:
        pass
    return s


def _best_recall(results, stems=None):
    """Выбирает наиболее полезную запись: по пересечению основ, затем по типу."""
    if not results:
        return None
    def overlap(e):
        if not stems:
            return 0
        ctx = (e.get("context") or "").lower()
        return sum(1 for st in stems if st in ctx)
    return max(results, key=lambda e: (overlap(e), _recall_type_score(e)))


def recall_fact(query: str, store_if_missing: bool = False,
                dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    stems = _recall_stems(query)
    candidates = {}   # id -> entry (dedupe)

    def _add(entry):
        eid = entry.get("id")
        if eid and eid not in candidates:
            candidates[eid] = entry

    # 1) Текущий диалог (path-поиск)
    for e in (conversation_memory.query(dialog=d_id, path=query, limit=10) or []):
        _add(e)
    dialog_ids = set(candidates.keys())

    # 2) Глобальный path-поиск
    for e in (conversation_memory.query(path=query, limit=10) or []):
        _add(e)

    # 3) Глобальный стем-поиск по context (между диалогами, терпим к морфологии)
    try:
        conn = conversation_memory._open_conn()
        rows = conn.execute(
            "SELECT * FROM entries WHERE context IS NOT NULL AND TRIM(context) != '' "
            "ORDER BY ts DESC LIMIT 2000"
        ).fetchall()
        conn.close()
        for row in rows:
            ctx = (row["context"] or "").lower()
            if stems and not any(st in ctx for st in stems):
                continue
            keys = row.keys()
            _add({
                "id": row["id"], "op": row["op"],
                "paths": json.loads(row["paths_json"]) if row["paths_json"] else {},
                "status": row["status"], "context": row["context"], "ts": row["ts"],
                "memory_type": row["memory_type"] if "memory_type" in keys else "",
                "confidence": row["confidence"] if "confidence" in keys else 0,
            })
    except Exception as e:
        _log(f"Keyword search in context failed: {e}")

    if candidates:
        cand_list = list(candidates.values())

        def _ov(e):
            if not stems:
                return 0
            ctx = (e.get("context") or "").lower()
            return sum(1 for st in stems if st in ctx)

        # Взвешенная оценка: пересечение основ + тип записи. Тип (намеренно
        # сохранённое решение/факт/ответ) перевешивает то, что вопрос пользователя
        # текстуально ближе к запросу — иначе recall возвращал бы сам вопрос.
        best = max(cand_list, key=lambda e: _ov(e) * 1.0 + _recall_type_score(e))
        source = "dialog_memory" if best.get("id") in dialog_ids else "global_memory"
        return {
            "found": True, "source": source,
            "confidence": "high" if source == "dialog_memory" else "medium",
            "fact": {
                "id": best.get("id"), "operation": best.get("op"),
                "paths": best.get("paths"), "status": best.get("status"),
                "context": best.get("context"), "timestamp": best.get("ts")
            },
            "related_count": len(cand_list)
        }

    # Finally, search in compressed history
    try:
        conn = conversation_memory._open_conn()
        comp = conn.execute(
            "SELECT summary FROM compressed_history WHERE dialog = ? ORDER BY ts DESC LIMIT 1",
            (d_id,)
        ).fetchone()
        conn.close()
        if comp and query.lower() in comp[0].lower():
            return {
                "found": True,
                "source": "compressed_history",
                "confidence": "low",
                "summary": comp[0][:300]
            }
    except Exception:
        pass

    if store_if_missing:
        conversation_memory.add(
            op="recall_fact",
            paths={"query": query},
            status="missing",
            dialog=d_id,
            context=f"Fact query '{query}' not found, stored as placeholder"
        )
    return {
        "found": False,
        "fact": None,
        "source": None,
        "confidence": "none"
    }

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("context-manager", "3.0")
server.register_tool("compress_history", {
    "description": "Compress dialog history with extractive summarization",
    "inputSchema": {
        "type": "object",
        "properties": {
            "history": {"type": "array", "items": {"type": "string"}},
            "max_sentences": {"type": "integer", "default": 10},
            "dialog_id": {"type": "string"}
        },
        "required": ["history"]
    }
}, lambda **kw: compress_history(
    kw["history"], kw.get("max_sentences", 10), kw.get("dialog_id")
))

server.register_tool("split_into_chunks", {
    "description": "Split long text into overlapping chunks with sentence boundary awareness",
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "chunk_size": {"type": "integer", "default": 1000},
            "overlap": {"type": "integer", "default": 100}
        },
        "required": ["text"]
    }
}, lambda **kw: split_into_chunks(
    kw["text"], kw.get("chunk_size", 1000), kw.get("overlap", 100)
))

server.register_tool("recall_fact", {
    "description": "Retrieve fact from persistent conversation memory (supports keyword search in context)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "store_if_missing": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["query"]
    }
}, lambda **kw: recall_fact(
    kw["query"], kw.get("store_if_missing", False), kw.get("dialog_id")
))

if __name__ == "__main__":
    server.run()