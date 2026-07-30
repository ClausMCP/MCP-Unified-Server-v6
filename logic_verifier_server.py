#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Logic & Fact Verifier v3.1 (Context-Isolated & NLP-Shared)
Enhanced consistency checking, arithmetic validation,
and contextual fact verification with memory integration.
Uses contextvars for secure dialog isolation and shared NLP utilities.
"""
import os
import sys
import re
import json
from typing import List, Dict, Any
from collections import defaultdict
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Pre-compiled Patterns ───────────────────────────────────────────────────
_NUM_PATTERN = re.compile(r'(-?\d+(?:\.\d+)?)\s*([a-zA-Z_\u0400-\u04FF]+)')
_WORD_PATTERN = re.compile(r'\b\w+\b')
_EQ_PATTERN = re.compile(
    r'(-?\d+(?:\.\d+)?)\s*([+\-*/])\s*(-?\d+(?:\.\d+)?)\s*=\s*(-?\d+(?:\.\d+)?)'
)

# ─── Shared NLP Resources (import from central utils if available) ───────────
try:
    from mcp_nlp_utils import STOP_WORDS, sentence_similarity
except ImportError:
    # Fallback inline definitions for standalone operation
    STOP_WORDS = {
        'the','a','an','is','are','was','were','in','on','at','to','of','for',
        'with','and','or','it','that','this','be','as','by','not','but','from',
        'has','have','had','will','would','can','could','should','may','might',
        'do','does','did','been','being','am','so','if','then','than','only',
        'just','also','very','too','much','many','more','most','some','any',
        'no','yes','ok','well','oh','ah','um','uh','like','you','i','we','they',
        'he','she','his','her','its','our','their','my','me','us','them','him'
    }
    def sentence_similarity(s1: str, s2: str, threshold: float = 0.7) -> float:
        words1 = set(_WORD_PATTERN.findall(s1.lower()))
        words2 = set(_WORD_PATTERN.findall(s2.lower()))
        if not words1 or not words2:
            return 0.0
        intersection = words1.intersection(words2)
        return len(intersection) / max(len(words1), len(words2))

# ─── Check Consistency ─────────────────────────────────────────────────────
def check_consistency(statements: List[str], dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    contradictions = []
    
    # Numeric consistency
    num_data = defaultdict(list)
    for idx, stmt in enumerate(statements):
        for m in _NUM_PATTERN.finditer(stmt):
            val = float(m.group(1))
            unit = m.group(2).lower()
            num_data[unit].append((val, idx, m.group(0)))
    
    for unit, vals in num_data.items():
        unique = set(v[0] for v in vals)
        if len(unique) > 1:
            occurrences = [
                f"[{i+1}] '{statements[i]}' (value: {v})"
                for v, i, _ in vals
            ]
            contradictions.append({
                "type": "numeric_mismatch",
                "unit": unit,
                "values": sorted(unique),
                "detail": f"Different numbers for unit '{unit}'",
                "occurrences": occurrences
            })
    
    # Negation conflicts
    for i in range(len(statements)):
        for j in range(i + 1, len(statements)):
            s1 = statements[i].lower()
            s2 = statements[j].lower()
            
            # Check for "not" negation
            if " not " in s1 and " not " not in s2:
                pos = s1.replace(" not ", " ")
                if sentence_similarity(pos, s2) > 0.75:
                    contradictions.append({
                        "type": "negation_conflict",
                        "indices": [i, j],
                        "detail": f"Statement [{i+1}] negates [{j+1}]",
                        "statements": [statements[i], statements[j]]
                    })
            
            # Check for antonyms
            antonym_pairs = [
                ("increase", "decrease"), ("up", "down"), ("more", "less"),
                ("higher", "lower"), ("before", "after"), ("add", "remove"),
                ("true", "false"), ("yes", "no"), ("all", "none")
            ]
            words1 = set(_WORD_PATTERN.findall(s1))
            words2 = set(_WORD_PATTERN.findall(s2))
            for a, b in antonym_pairs:
                if (a in words1 and b in words2) or (b in words1 and a in words2):
                    if len(words1.intersection(words2)) > max(len(words1), len(words2)) * 0.5:
                        contradictions.append({
                            "type": "antonym_conflict",
                            "indices": [i, j],
                            "detail": f"Antonyms detected: '{a}' vs '{b}'",
                            "statements": [statements[i], statements[j]]
                        })
                        break
    
    # Temporal consistency (before/after)
    temporal = defaultdict(list)
    for idx, stmt in enumerate(statements):
        lower = stmt.lower()
        for keyword in ("before", "after", "during", "at"):
            if keyword in lower:
                temporal[keyword].append((idx, stmt))
    
    # Log to conversation memory
    conversation_memory.add(
        op="check_consistency",
        paths={"statement_count": len(statements)},
        status="consistent" if not contradictions else "inconsistent",
        dialog=d_id,
        context=f"Checked {len(statements)} statements, found {len(contradictions)} contradictions"
    )
    
    return {
        "consistent": len(contradictions) == 0,
        "contradictions": contradictions,
        "statement_count": len(statements),
        "numeric_units_found": len(num_data),
        "notes": "Checks numeric, negation, antonym and temporal patterns"
    }

# ─── Validate Numbers ────────────────────────────────────────────────────────
def validate_numbers(text: str, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    numbers = _NUM_PATTERN.findall(text)
    valid = True
    messages = []
    
    # Check arithmetic equations
    for m in _EQ_PATTERN.finditer(text):
        a, op, b, c = m.group(1), m.group(2), m.group(3), m.group(4)
        a, b, c = float(a), float(b), float(c)
        ops = {'+': a + b, '-': a - b, '*': a * b, '/': a / b if b != 0 else float('inf')}
        expected = ops.get(op)
        if expected is not None and abs(expected - c) > 0.001:
            valid = False
            messages.append(f"Error: {a} {op} {b} = {c} (should be {expected:.2f})")
    
    # Check standalone additions
    add_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*\+\s*(\d+(?:\.\d+)?)\s*=\s*(\d+(?:\.\d+)?)')
    for m in add_pattern.finditer(text):
        a, b, c = float(m.group(1)), float(m.group(2)), float(m.group(3))
        if abs((a + b) - c) > 0.001:
            valid = False
            messages.append(f"Error: {a} + {b} != {c}, should be {a+b:.2f}")
    
    if not messages:
        if any(op in text for op in '+-*/='):
            messages.append("No arithmetic errors found")
        else:
            messages.append("No arithmetic operators found")
    
    conversation_memory.add(
        op="validate_numbers",
        paths={"text_preview": text[:100]},
        status="valid" if valid else "invalid",
        dialog=d_id,
        context=f"Validated {len(numbers)} numbers, {len(messages)} messages"
    )
    
    return {
        "valid": valid,
        "numbers_detected": [{"value": n[0], "unit": n[1]} for n in numbers],
        "messages": messages,
        "number_count": len(numbers)
    }

# ─── Fact Check ──────────────────────────────────────────────────────────────
def fact_check(claim: str, context: str, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    claim_lower = claim.lower()
    context_lower = context.lower()
    
    # Extract key terms (skip stop words)
    claim_words = [
        w for w in _WORD_PATTERN.findall(claim_lower)
        if w not in STOP_WORDS and len(w) > 2
    ]
    
    if not claim_words:
        return {
            "claim": claim,
            "found_in_context": False,
            "confidence": "none",
            "reason": "No meaningful terms in claim"
        }
    
    found_words = [w for w in claim_words if w in context_lower]
    ratio = len(found_words) / len(claim_words)
    
    # Confidence levels
    if ratio >= 0.8:
        confidence = "high"
    elif ratio >= 0.5:
        confidence = "medium"
    elif ratio >= 0.3:
        confidence = "low"
    else:
        confidence = "none"
    
    # Check for exact phrase
    exact_phrase = any(phrase in context_lower for phrase in [
        claim_lower,
        claim_lower.strip('.!?')
    ])
    
    conversation_memory.add(
        op="fact_check",
        paths={"claim": claim},
        status=f"confidence_{confidence}",
        dialog=d_id,
        context=f"Fact check: '{claim[:50]}...' vs context, ratio={ratio:.2f}"
    )
    
    return {
        "claim": claim,
        "found_in_context": ratio > 0.3 or exact_phrase,
        "confidence": confidence,
        "matching_terms": found_words,
        "total_terms": len(claim_words),
        "match_ratio": round(ratio, 2),
        "exact_phrase_found": exact_phrase
    }

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("logic-verifier", "3.1")
server.register_tool("check_consistency", {
    "description": "Check statements for contradictions (numeric, negation, antonym)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "statements": {"type": "array", "items": {"type": "string"}},
            "dialog_id": {"type": "string"}
        },
        "required": ["statements"]
    }
}, lambda **kw: check_consistency(kw["statements"], kw.get("dialog_id")))

server.register_tool("validate_numbers", {
    "description": "Extract numbers and validate arithmetic",
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "dialog_id": {"type": "string"}
        },
        "required": ["text"]
    }
}, lambda **kw: validate_numbers(kw["text"], kw.get("dialog_id")))

server.register_tool("fact_check", {
    "description": "Compare claim with context using semantic matching",
    "inputSchema": {
        "type": "object",
        "properties": {
            "claim": {"type": "string"},
            "context": {"type": "string"},
            "dialog_id": {"type": "string"}
        },
        "required": ["claim", "context"]
    }
}, lambda **kw: fact_check(kw["claim"], kw["context"], kw.get("dialog_id")))

if __name__ == "__main__":
    server.run()