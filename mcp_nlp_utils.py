#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP NLP Utilities v1.0
Shared NLP resources: stop words, text splitting, keyword extraction,
sentence similarity (intersection based), and text chunking helpers.
Used by logic_verifier_server, context_manager_server, and others.
"""

import re
from collections import Counter
from typing import List, Set, Dict, Optional

# ─── Pre‑compiled Patterns ───────────────────────────────────────────────────
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?…])\s+')
_WORD_SPLIT = re.compile(r'\b\w+\b')
_NUM_PATTERN = re.compile(r'(-?\d+(?:\.\d+)?)\s*([a-zA-Z_\u0400-\u04FF]+)')

# ─── Stop Words (russian + english minimal set) ─────────────────────────────
STOP_WORDS: Set[str] = {
    # English
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'in', 'on', 'at', 'to', 'of',
    'for', 'with', 'and', 'or', 'it', 'that', 'this', 'be', 'as', 'by', 'not',
    'but', 'from', 'has', 'have', 'had', 'will', 'would', 'can', 'could',
    'should', 'may', 'might', 'do', 'does', 'did', 'been', 'being', 'am',
    'so', 'if', 'then', 'than', 'only', 'just', 'also', 'very', 'too', 'much',
    'many', 'more', 'most', 'some', 'any', 'no', 'yes', 'ok', 'well', 'oh',
    'ah', 'um', 'uh', 'like', 'you', 'i', 'we', 'they', 'he', 'she', 'his',
    'her', 'its', 'our', 'their', 'my', 'me', 'us', 'them', 'him',
    # Russian basic (cyrillic)
    'и', 'в', 'не', 'на', 'я', 'что', 'с', 'а', 'по', 'к', 'у', 'о', 'так',
    'же', 'из', 'за', 'под', 'над', 'без', 'до', 'при', 'для', 'от', 'во',
    'ты', 'он', 'она', 'оно', 'мы', 'вы', 'они', 'это', 'эта', 'этот', 'эти',
    'был', 'была', 'было', 'были', 'быть', 'стал', 'стала', 'стало', 'стали',
    'который', 'которая', 'которое', 'которые'
}

# ─── Text Splitting ──────────────────────────────────────────────────────────
def split_sentences(text: str) -> List[str]:
    """
    Split text into sentences using punctuation boundaries.
    Returns list of non‑empty stripped sentences.
    """
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]

def split_words(text: str, lowercase: bool = True) -> List[str]:
    """
    Extract words (alphanumeric sequences) from text.
    Optionally lowercases them.
    """
    if not text:
        return []
    words = _WORD_SPLIT.findall(text.lower() if lowercase else text)
    return words

def filter_stop_words(words: List[str], additional_stop_words: Optional[Set[str]] = None) -> List[str]:
    """
    Remove common stop words from a list of tokens.
    Allows passing extra stop words.
    """
    stop = STOP_WORDS.copy()
    if additional_stop_words:
        stop.update(additional_stop_words)
    return [w for w in words if w not in stop and len(w) > 2]

# ─── Keyword Extraction ─────────────────────────────────────────────────────
def extract_keywords(text: str, top_n: int = 10, min_word_len: int = 3) -> List[str]:
    """
    Extract most frequent meaningful keywords from text.
    Filters stop words and short tokens.
    """
    words = split_words(text, lowercase=True)
    filtered = [w for w in words if w not in STOP_WORDS and len(w) >= min_word_len]
    if not filtered:
        return []
    freq = Counter(filtered)
    return [word for word, _ in freq.most_common(top_n)]

# ─── Similarity (simple overlap) ──────────────────────────────────────────
def sentence_similarity(s1: str, s2: str, threshold: float = 0.7) -> float:
    """
    Compute overlap coefficient between two strings based on word sets.
    Returns value in [0, 1].
    """
    words1 = set(split_words(s1, lowercase=True))
    words2 = set(split_words(s2, lowercase=True))
    if not words1 or not words2:
        return 0.0
    intersect = words1.intersection(words2)
    # Use Jaccard-like overlap: intersection / min(len1, len2) – gives higher score for short matches
    return len(intersect) / min(len(words1), len(words2))

def text_similarity(text1: str, text2: str) -> float:
    """Alias for sentence_similarity."""
    return sentence_similarity(text1, text2)

# ─── Numbers Extraction ─────────────────────────────────────────────────────
def extract_numbers_with_units(text: str) -> List[Dict[str, str]]:
    """
    Extract numeric values and their following units.
    Returns list of dicts: {'value': str, 'unit': str, 'match': str}
    """
    matches = []
    for m in _NUM_PATTERN.finditer(text):
        matches.append({
            "value": m.group(1),
            "unit": m.group(2).lower(),
            "match": m.group(0)
        })
    return matches

def extract_numbers(text: str) -> List[float]:
    """Extract all numbers (including decimal) as floats."""
    return [float(num) for num in re.findall(r'-?\d+(?:\.\d+)?', text)]

# ─── Chunking (for long texts) ─────────────────────────────────────────────
def chunk_by_sentences(text: str, max_chars: int = 2000, overlap_sentences: int = 1) -> List[str]:
    """
    Split text into chunks using sentence boundaries, with optional overlap.
    """
    sentences = split_sentences(text)
    if not sentences:
        return []
    chunks = []
    current = []
    current_len = 0
    for i, sent in enumerate(sentences):
        sent_len = len(sent)
        if current_len + sent_len > max_chars and current:
            chunks.append(' '.join(current))
            # keep overlapping sentences
            overlap = max(0, min(overlap_sentences, len(current)))
            current = current[-overlap:] if overlap else []
            current_len = sum(len(s) for s in current)
        current.append(sent)
        current_len += sent_len
    if current:
        chunks.append(' '.join(current))
    return chunks

# ─── Helper for memory consistency ─────────────────────────────────────────
def is_contradictory(statement1: str, statement2: str, similarity_threshold: float = 0.7) -> bool:
    """
    Rough heuristic: if one contains 'not' and the other does not,
    and they are otherwise similar, treat as contradiction.
    """
    s1_low = statement1.lower()
    s2_low = statement2.lower()
    if (' not ' in s1_low) != (' not ' in s2_low):
        pos1 = s1_low.replace(' not ', ' ')
        pos2 = s2_low.replace(' not ', ' ')
        if sentence_similarity(pos1, pos2) > similarity_threshold:
            return True
    return False

# ─── Exposed public API ────────────────────────────────────────────────────
__all__ = [
    "STOP_WORDS",
    "split_sentences",
    "split_words",
    "filter_stop_words",
    "extract_keywords",
    "sentence_similarity",
    "text_similarity",
    "extract_numbers_with_units",
    "extract_numbers",
    "chunk_by_sentences",
    "is_contradictory",
]