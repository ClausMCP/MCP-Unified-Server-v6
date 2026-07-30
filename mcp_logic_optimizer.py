#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Logic & Context Optimizer v3.1 (Context-Isolated)
Dialog summarization and logic consistency checking with persistent memory
and enhanced analysis. Uses contextvars for secure dialog isolation.
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

# ─── Pre-compiled Patterns ───────────────────────────────────────────────────
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?…])\s+')
_WORD_SPLIT = re.compile(r'\b\w+\b')
_NUM_PATTERN = re.compile(r'(-?\d+(?:\.\d+)?)\s*([a-zA-Z_\u0400-\u04FF]+)')
_STOP_WORDS = {
    'the','a','an','is','are','was','were','in','on','at','to','of','for',
    'with','and','or','it','that','this','be','as','by','not','but','from',
    'has','have','had','will','would','can','could','should','may','might',
    'do','does','did','been','being','am','so','if','then','than','only',
    'just','also','very','too','much','many','more','most','some','any',
    'no','yes','ok','well','oh','ah','um','uh','like','you','i','we','they',
    'he','she','his','her','its','our','their','my','me','us','them','him'
}

# ─── Summarize Context ───────────────────────────────────────────────────────
def summarize_context(dialog_history: List[str], max_sentences: int = 10,
                     dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    
    if not dialog_history:
        return {"summary": "", "original_sentences": 0, "note": "Empty history"}
    
    full_text = " ".join(dialog_history)
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(full_text) if s.strip()]
    
    if len(sentences) <= max_sentences:
        summary = " ".join(sentences)
        conversation_memory.add(
            op="summarize_context",
            paths={"dialog": d_id},
            status="under_limit",
            dialog=d_id,
            context=f"Context summary (under limit): {summary[:200]}"
        )
        return {
            "summary": summary,
            "original_sentences": len(sentences),
            "summary_sentences": len(sentences),
            "note": "Under limit, returned as-is"
        }

    # TF-IDF scoring
    words = _WORD_SPLIT.findall(full_text.lower())
    word_freq = Counter(
        w for w in words
        if w not in _STOP_WORDS and len(w) > 2
    )

    def score(sent: str) -> float:
        sent_words = _WORD_SPLIT.findall(sent.lower())
        if not sent_words:
            return 0.0
        score_val = sum(word_freq.get(w, 0) for w in sent_words if w not in _STOP_WORDS)
        # Normalize by length
        return score_val / max(len(sent_words), 1)

    scored = [(i, s, score(s)) for i, s in enumerate(sentences)]
    scored.sort(key=lambda x: x[2], reverse=True)
    
    # Select top preserving order
    top_indices = {x[0] for x in scored[:max_sentences]}
    selected = [sentences[i] for i in sorted(top_indices)]
    summary = " ".join(selected)
    
    # Detect key topics
    topic_words = [w for w, c in word_freq.most_common(5)]
    
    conversation_memory.add(
        op="summarize_context",
        paths={"dialog": d_id},
        status="compressed",
        dialog=d_id,
        context=f"Compressed context: {summary[:300]} | Topics: {', '.join(topic_words)}"
    )
    return {
        "summary": summary,
        "original_sentences": len(sentences),
        "summary_sentences": len(selected),
        "compression_ratio": round(len(selected) / len(sentences), 2),
        "key_topics": topic_words,
        "note": "TF-IDF extractive summarization"
    }

# ─── Check Logic Consistency ─────────────────────────────────────────────────
def check_logic_consistency(statements: List[str]) -> Dict:
    contradictions = []
    # Numeric analysis
    num_data = {}
    for idx, stmt in enumerate(statements):
        for match in _NUM_PATTERN.finditer(stmt):
            value = float(match.group(1))
            unit = match.group(2).lower()
            num_data.setdefault(unit, []).append((value, idx, match.group(0)))
            
    for unit, vals in num_data.items():
        unique_vals = set(v[0] for v in vals)
        if len(unique_vals) > 1:
            occurrences = [
                f"[{i+1}] '{statements[i]}'"
                for _, i, _ in vals
            ]
            contradictions.append({
                "type": "numeric_mismatch",
                "unit": unit,
                "values": sorted(unique_vals),
                "detail": f"Different numbers for unit '{unit}'",
                "source_assertions": occurrences
            })

    # Negation and semantic conflicts
    for i in range(len(statements)):
        for j in range(i + 1, len(statements)):
            s1 = statements[i].lower()
            s2 = statements[j].lower()
            
            # Negation
            if " not " in s1 and " not " not in s2:
                positive = s1.replace(" not ", " ")
                if _sentence_similarity(positive, s2) > 0.7:
                    contradictions.append({
                        "type": "negation_conflict",
                        "indices": [i, j],
                        "detail": f"Opposite statements: [{i+1}] and [{j+1}]",
                        "statements": [statements[i], statements[j]]
                    })
                    
            # Antonyms
            antonyms = [
                ("increase", "decrease"), ("up", "down"), ("more", "less"),
                ("higher", "lower"), ("before", "after"), ("add", "remove"),
                ("true", "false"), ("yes", "no"), ("all", "none"),
                ("always", "never"), ("everyone", "no one")
            ]
            words1 = set(_WORD_SPLIT.findall(s1))
            words2 = set(_WORD_SPLIT.findall(s2))
            
            for a, b in antonyms:
                if (a in words1 and b in words2) or (b in words1 and a in words2):
                    overlap = len(words1.intersection(words2)) / max(len(words1), len(words2))
                    if overlap > 0.4:
                        contradictions.append({
                            "type": "antonym_conflict",
                            "indices": [i, j],
                            "detail": f"Antonyms: '{a}' vs '{b}'",
                            "statements": [statements[i], statements[j]]
                        })
                        break

    # Store analysis in memory
    conversation_memory.add(
        op="check_logic_consistency",
        paths={"statements": len(statements)},
        status="consistent" if not contradictions else "inconsistent",
        dialog=dialog_ctx.get(),
        context=f"Checked {len(statements)} statements, found {len(contradictions)} contradictions"
    )
    return {
        "consistent": len(contradictions) == 0,
        "contradictions": contradictions,
        "statement_count": len(statements),
        "numeric_units_found": len(num_data),
        "notes": "Checks numeric, negation, and antonym conflicts"
    }

def _sentence_similarity(s1: str, s2: str, threshold: float = 0.7) -> float:
    words1 = set(_WORD_SPLIT.findall(s1.lower()))
    words2 = set(_WORD_SPLIT.findall(s2.lower()))
    if not words1 or not words2:
        return 0.0
    intersection = words1.intersection(words2)
    return len(intersection) / min(len(words1), len(words2))

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("logic-optimizer", "3.1")
server.register_tool("summarize_context", {
    "description": "Compress dialog history with TF-IDF scoring",
    "inputSchema": {
        "type": "object",
        "properties": {
            "dialog_history": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of messages"
            },
            "max_sentences": {"type": "integer", "default": 10},
            "dialog_id": {"type": "string"}
        },
        "required": ["dialog_history"]
    }
}, lambda **kw: summarize_context(
    kw["dialog_history"], kw.get("max_sentences", 10), kw.get("dialog_id")
))

server.register_tool("check_logic_consistency", {
    "description": "Check statements for contradictions",
    "inputSchema": {
        "type": "object",
        "properties": {
            "statements": {
                "type": "array",
                "items": {"type": "string"}
            }
        },
        "required": ["statements"]
    }
}, lambda **kw: check_logic_consistency(kw["statements"]))

if __name__ == "__main__":
    server.run()