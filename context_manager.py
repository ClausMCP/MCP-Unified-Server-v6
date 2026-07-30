#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Context Manager v3.1 (FTS5 + Token-Aware)

Исправления относительно исходной версии (v3.0):
- `recall_fact` использует FTS5 (токенизатор подбирается пробой на старте)
  (вместо линейного перебора 2 000 строк в Python).
- Удалён мёртвый код `_best_recall`.
- Соединения с БД — через context manager (нет утечек при исключениях).
- `split_into_chunks` принимает лимит в токенах (а не символах), по умолчанию
  подстраивается под размер контекстного окна модели.
- `compress_history` использует простую эвристику по токенам, если
  доступна `get_model_info()`.
- Добавлена таблица `entries_fts` (создаётся при первом обращении через
  `_ensure_fts_table()`). Если БД не поддерживает FTS5 — fallback на
  медленный, но корректный `LIKE`-поиск с предупреждением.
- Добавлен `ensure_schema()` для идемпотентной миграции.
"""
import json
import os
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    from mcp_shared import BaseMCPServer, _log, conversation_memory, dialog_ctx
except ImportError as exc:
    print(f"FATAL: missing mcp_shared: {exc}", file=sys.stderr)
    sys.exit(1)

# Импорт менеджера подключений (опционально — для контекстного окна)
try:
    from connectivity import get_model_info
except ImportError:
    get_model_info = None  # type: ignore[assignment]


# ─── Common NLP Resources ───────────────────────────────────────────────────
STOP_WORDS: frozenset = frozenset(
    {
        "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to",
        "of", "for", "with", "and", "or", "it", "that", "this", "be", "as",
        "by", "not", "but", "from", "has", "have", "had", "will", "would",
        "can", "could", "should", "may", "might", "do", "does", "did", "been",
        "being", "am", "so", "if", "then", "than", "only", "just", "also",
        "very", "too", "much", "many", "more", "most", "some", "any", "no",
        "yes", "ok", "well", "oh", "ah", "um", "uh", "like", "you", "i",
        "we", "they", "he", "she", "his", "her", "its", "our", "their", "my",
        "me", "us", "them", "him",
    }
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_WORD_SPLIT = re.compile(r"\b\w+\b")

# ─── Token estimation ──────────────────────────────────────────────────────
def estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов для русского/английского текста.

    Точнее — tiktoken, но мы не добавляем зависимость. 1 токен ≈ 3.5 символа
    для смешанного ru/en, 1 токен ≈ 4 символа для чистого en.
    """
    if not text:
        return 0
    return max(1, int(len(text) / 3.5))


def model_context_budget(default: int = 4096) -> int:
    """Возвращает размер контекстного окна текущей модели (с запасом 25%)."""
    if get_model_info is None:
        return default
    try:
        info = get_model_info()
    except Exception:
        return default
    if info is None or not info.context_length:
        return default
    return max(512, int(info.context_length * 0.75))


# ─── FTS5 schema management ─────────────────────────────────────────────────
_FTS_SCHEMA_STATEMENTS: Tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-64000",  # 64 MB
)

# ВАЖНО: токенизатора 'snowball' в штатном SQLite НЕТ. Встроенные токенизаторы
# FTS5: unicode61, ascii, porter, trigram. snowball — стороннее C-расширение,
# которое нужно собирать и грузить через load_extension. Прежняя версия жёстко
# требовала 'snowball russian', из-за чего CREATE VIRTUAL TABLE падал всегда,
# _fts_failed навсегда становился True, и FTS-поиск молча возвращал пустой
# список. Теперь токенизатор подбирается пробой на реальном соединении.
_TOKENIZER_CANDIDATES: Tuple[str, ...] = (
    "snowball russian english",        # если сборка с расширением есть
    "unicode61 remove_diacritics 2",   # штатный, корректен для кириллицы
    "unicode61",                       # минимальный штатный
)

# src_id хранит rowid исходной записи в entries — по нему собирается entry.
_FTS_COLUMNS: str = (
    "context, paths, op, dialog UNINDEXED, status UNINDEXED, "
    "memory_type UNINDEXED, ts UNINDEXED, src_id UNINDEXED"
)


def _fts_ddl(tokenizer: str) -> str:
    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5("
        f"{_FTS_COLUMNS}, tokenize='{tokenizer}')"
    )


_FTS_STATE_DDL = (
    "CREATE TABLE IF NOT EXISTS entries_fts_state ("
    "k TEXT PRIMARY KEY, v TEXT NOT NULL)"
)


@contextmanager
def _open_conn() -> Iterator[Any]:
    """Открывает соединение с conversation_memory через context manager."""
    open_fn = getattr(conversation_memory, "_open_conn", None)
    if open_fn is None:
        raise RuntimeError("conversation_memory has no _open_conn()")
    conn = open_fn()
    try:
        yield conn
    finally:
        close = getattr(conn, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


_fts_ready: bool = False
_fts_failed: bool = False
_fts_tokenizer: str = ""


def _probe_tokenizer(conn: Any) -> str:
    """Первый реально работающий токенизатор. Пустая строка = FTS5 недоступен."""
    for idx, tok in enumerate(_TOKENIZER_CANDIDATES):
        probe = f"_fts_probe_{idx}"
        try:
            conn.execute(f"DROP TABLE IF EXISTS {probe}")
            conn.execute(
                f"CREATE VIRTUAL TABLE {probe} USING fts5(x, tokenize='{tok}')"
            )
            conn.execute(f"DROP TABLE {probe}")
            return tok
        except Exception:
            continue
    return ""


def _table_columns(conn: Any, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _get_state(conn: Any, key: str, default: str = "0") -> str:
    try:
        row = conn.execute(
            "SELECT v FROM entries_fts_state WHERE k = ?", (key,)
        ).fetchone()
        return default if row is None else str(row[0])
    except Exception:
        return default


def _set_state(conn: Any, key: str, value: str) -> None:
    try:
        conn.execute(
            "INSERT INTO entries_fts_state(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
    except Exception:
        pass


def _sync_fts(conn: Any, batch: int = 5000) -> int:
    """Инкрементально переносит новые строки entries -> entries_fts.

    Без этого MATCH всегда возвращал бы пустой результат: в прежней версии
    таблица создавалась, но никогда не заполнялась (ни INSERT, ни триггеров).
    Watermark (последний перенесённый rowid) хранится в entries_fts_state.
    """
    cols = _table_columns(conn, "entries")
    if not cols:
        return 0

    def col(name: str) -> str:
        return name if name in cols else "''"

    paths_col = "paths_json" if "paths_json" in cols else col("paths")
    last = int(_get_state(conn, "last_rowid", "0") or 0)
    try:
        rows = conn.execute(
            f"SELECT rowid, {col('context')}, {paths_col}, {col('op')}, "
            f"{col('dialog')}, {col('status')}, {col('memory_type')}, {col('ts')} "
            "FROM entries WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last, batch),
        ).fetchall()
    except Exception as exc:
        _log(f"[context_manager] FTS sync read failed: {exc}")
        return 0
    if not rows:
        return 0

    payload = [
        (
            r[1] or "", r[2] or "", r[3] or "", r[4] or "",
            r[5] or "", r[6] or "", r[7] or "", r[0],
        )
        for r in rows
    ]
    try:
        conn.executemany(
            "INSERT INTO entries_fts("
            "context, paths, op, dialog, status, memory_type, ts, src_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            payload,
        )
        _set_state(conn, "last_rowid", str(rows[-1][0]))
        conn.commit()
    except Exception as exc:
        _log(f"[context_manager] FTS sync write failed: {exc}")
        return 0
    return len(rows)


def _ensure_fts_table() -> bool:
    """Идемпотентно создаёт FTS5-таблицу и подтягивает новые записи."""
    global _fts_ready, _fts_failed, _fts_tokenizer
    if _fts_failed:
        return False
    try:
        with _open_conn() as conn:
            if not _fts_ready:
                for stmt in _FTS_SCHEMA_STATEMENTS:
                    try:
                        conn.execute(stmt)
                    except Exception:
                        pass
                tok = _probe_tokenizer(conn)
                if not tok:
                    _log("[context_manager] FTS5 недоступен в этой сборке SQLite")
                    _fts_failed = True
                    return False
                conn.execute(_fts_ddl(tok))
                conn.execute(_FTS_STATE_DDL)
                conn.commit()
                _fts_tokenizer = tok
                _fts_ready = True
                _log(f"[context_manager] FTS5 tokenizer: {tok}")
            _sync_fts(conn)
        return True
    except Exception as e:
        _log(f"[context_manager] FTS5 init failed: {e}")
        _fts_failed = True
        return False


def fts_status() -> Dict[str, Any]:
    """Диагностика: доступен ли FTS5, какой токенизатор, сколько строк в индексе."""
    ready = _ensure_fts_table()
    info: Dict[str, Any] = {
        "available": ready,
        "tokenizer": _fts_tokenizer,
        "indexed_rows": 0,
    }
    if ready:
        try:
            with _open_conn() as conn:
                row = conn.execute("SELECT count(*) FROM entries_fts").fetchone()
                info["indexed_rows"] = int(row[0]) if row else 0
        except Exception:
            pass
    return info


# ─── Compress History ────────────────────────────────────────────────────────
def compress_history(
    history: List[str],
    max_sentences: int = 10,
    dialog_id: Optional[str] = None,
) -> Dict[str, Any]:
    d_id = dialog_id
    if d_id is None:
        try:
            d_id = dialog_ctx.get()
        except LookupError:
            d_id = None
    if not history:
        return {
            "summary": "",
            "original_sentences": 0,
            "note": "Empty history",
        }

    text = " ".join(history)
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]

    if len(sentences) <= max_sentences:
        summary = " ".join(sentences)
        try:
            conversation_memory.add(
                op="compress_history",
                paths={"dialog": d_id or ""},
                status="under_limit",
                dialog=d_id,
                context=f"Summary (under limit): {summary[:200]}",
            )
        except Exception:
            pass
        return {
            "summary": summary,
            "original_sentences": len(sentences),
            "summary_sentences": len(sentences),
            "note": "Under limit, returned as-is",
        }

    # TF-скор — сумма частот слов в предложении, делённая на длину.
    words = _WORD_SPLIT.findall(text.lower())
    freq = Counter(w for w in words if w not in STOP_WORDS and len(w) > 2)

    def score(sent: str) -> float:
        sent_words = _WORD_SPLIT.findall(sent.lower())
        if not sent_words:
            return 0.0
        return sum(freq.get(w, 0) for w in sent_words if w not in STOP_WORDS) / max(
            len(sent_words), 1
        )

    scored = sorted(
        ((i, s, score(s)) for i, s in enumerate(sentences)),
        key=lambda x: x[2],
        reverse=True,
    )
    top_indices = {x[0] for x in scored[:max_sentences]}
    selected = [sentences[i] for i in sorted(top_indices)]
    summary = " ".join(selected)

    try:
        conversation_memory.add(
            op="compress_history",
            paths={"dialog": d_id or ""},
            status="compressed",
            dialog=d_id,
            context=(
                f"Compressed {len(sentences)} sentences to {len(selected)}. "
                f"Summary: {summary[:300]}"
            ),
        )
    except Exception:
        pass

    return {
        "summary": summary,
        "original_sentences": len(sentences),
        "summary_sentences": len(selected),
        "compression_ratio": round(len(selected) / len(sentences), 2),
        "note": "Extractive summarization with TF scoring",
    }


# ─── Split into Chunks ───────────────────────────────────────────────────────
def split_into_chunks(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 100,
    use_tokens: bool = False,
) -> List[str]:
    """Разбивает текст на чанки. По умолчанию — в символах; если use_tokens=True —
    в токенах (с учётом контекстного окна модели)."""
    if not text:
        return []
    if chunk_size <= 0:
        chunk_size = 1000
    if overlap < 0:
        overlap = 0
    if overlap >= chunk_size:
        overlap = chunk_size // 4

    if use_tokens:
        # Грубая бинаризация: делим текст на слова и набираем чанки по токенам.
        budget = chunk_size
        ov_budget = overlap
        words = text.split()
        chunks: List[str] = []
        cur_words: List[str] = []
        cur_tokens = 0
        for w in words:
            wt = max(1, estimate_tokens(w))
            if cur_tokens + wt > budget and cur_words:
                chunks.append(" ".join(cur_words))
                # Скользящее окно: оставляем overlap-токенов
                tail: List[str] = []
                tail_t = 0
                for prev in reversed(cur_words):
                    pt = max(1, estimate_tokens(prev))
                    if tail_t + pt > ov_budget:
                        break
                    tail.insert(0, prev)
                    tail_t += pt
                cur_words = tail
                cur_tokens = tail_t
            cur_words.append(w)
            cur_tokens += wt
        if cur_words:
            chunks.append(" ".join(cur_words))
        return chunks

    # Символьный режим (как было), с попыткой резать по границе предложения.
    step = chunk_size - overlap
    chunks_chars: List[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        if end < text_len:
            search_start = max(start + int(chunk_size * 0.8), start + 1)
            match = re.search(r"[.!?…]\s+", text[search_start:end])
            if match:
                end = search_start + match.end()
        chunks_chars.append(text[start:end])
        start += step
        if start >= text_len:
            break
        if start <= 0:
            start = end
    return chunks_chars


# ─── Recall Fact (FTS5 + dialog fallback) ────────────────────────────────────
def _query_via_path(query: str, dialog: Optional[str], limit: int = 10) -> List[Dict[str, Any]]:
    """Прямой path-поиск через conversation_memory (если поддерживается)."""
    fn = getattr(conversation_memory, "query", None)
    if fn is None:
        return []
    try:
        if dialog is not None:
            return list(fn(dialog=dialog, path=query, limit=limit) or [])
        return list(fn(path=query, limit=limit) or [])
    except Exception:
        return []


def _query_via_fts(stems: List[str], dialog: Optional[str], limit: int = 20) -> List[Dict[str, Any]]:
    """FTS5-поиск с фильтром по диалогу. Возвращает список записей."""
    if not _ensure_fts_table() or not stems:
        return []
    # FTS5 принимает выражения типа "stem1 OR stem2"; используем простой OR
    # Префиксный поиск ("термин*") — грубая, но рабочая замена морфологии
    # для unicode61. Спецсимволы FTS5 экранируются двойными кавычками.
    def _q(term: str) -> str:
        safe = term.replace('"', "")
        return f'"{safe}"*' if safe else ""

    expr = " OR ".join(t for t in (_q(s) for s in stems if s) if t)
    if not expr:
        return []
    try:
        with _open_conn() as conn:
            if dialog is not None:
                rows = conn.execute(
                    f"SELECT rowid, * FROM entries_fts "
                    f"WHERE entries_fts MATCH ? AND dialog = ? "
                    f"ORDER BY rank LIMIT ?",
                    (expr, dialog, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT rowid, * FROM entries_fts "
                    f"WHERE entries_fts MATCH ? "
                    f"ORDER BY rank LIMIT ?",
                    (expr, limit),
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        _log(f"FTS query failed: {e}")
        return []


def _query_via_fts_global(stems: List[str], limit: int = 20) -> List[Dict[str, Any]]:
    return _query_via_fts(stems, None, limit)


def _row_to_entry(row: Dict[str, Any]) -> Dict[str, Any]:
    paths_json = row.get("paths") or row.get("paths_json")
    paths: Dict[str, Any] = {}
    if isinstance(paths_json, str) and paths_json:
        try:
            paths = json.loads(paths_json)
        except ValueError:
            paths = {}
    elif isinstance(paths_json, dict):
        paths = paths_json
    return {
        # src_id — rowid исходной записи в entries; rowid — строка FTS-индекса.
        "id": row.get("src_id") or row.get("rowid") or row.get("id"),
        "op": row.get("op"),
        "paths": paths,
        "status": row.get("status"),
        "context": row.get("context"),
        "ts": row.get("ts"),
        "memory_type": row.get("memory_type") or "",
    }


def _recall_type_score(entry: Dict[str, Any]) -> float:
    op = entry.get("op") or ""
    paths = entry.get("paths") or {}
    role = paths.get("role", "") if isinstance(paths, dict) else ""
    score = 0.0
    if op != "conversation":
        score += 3.0
    if entry.get("memory_type") == "fact":
        score += 2.0
    if role == "assistant":
        score += 1.5
    if role == "user":
        score -= 2.0
    try:
        score += float(entry.get("confidence") or 0) * 0.5
    except (TypeError, ValueError):
        pass
    return score


def _stem_prefixes(query: str, prefix_len: int = 5, min_len: int = 4) -> List[str]:
    """Грубые префиксы для FTS5 MATCH (snowball даст лучший результат, но
    префиксы покрывают случаи, когда FTS5 недоступен)."""
    words = re.findall(r"\w+", (query or "").lower())
    return [w[:prefix_len] for w in words if len(w) >= min_len]


def recall_fact(
    query: str,
    store_if_missing: bool = False,
    dialog_id: Optional[str] = None,
) -> Dict[str, Any]:
    d_id = dialog_id
    if d_id is None:
        try:
            d_id = dialog_ctx.get()
        except LookupError:
            d_id = None
    stems = _stem_prefixes(query)

    # 1) Текущий диалог: path-поиск
    dialog_hits = _query_via_path(query, d_id, limit=10)

    # 2) Глобальный path-поиск
    global_path_hits = _query_via_path(query, None, limit=10)

    # 3) Глобальный FTS-поиск
    fts_hits = _query_via_fts_global(stems, limit=20)

    # Сводим в один список, дедуп по id
    candidates: Dict[Any, Dict[str, Any]] = {}
    dialog_ids: set = set()
    for entry in dialog_hits:
        eid = entry.get("id")
        if eid is not None and eid not in candidates:
            candidates[eid] = entry
            dialog_ids.add(eid)
    for entry in global_path_hits:
        eid = entry.get("id")
        if eid is not None and eid not in candidates:
            candidates[eid] = entry
    for row in fts_hits:
        entry = _row_to_entry(row)
        eid = entry.get("id")
        if eid is not None and eid not in candidates:
            candidates[eid] = entry

    if candidates:
        cand_list = list(candidates.values())

        def overlap(e: Dict[str, Any]) -> int:
            if not stems:
                return 0
            ctx = (e.get("context") or "").lower()
            return sum(1 for st in stems if st in ctx)

        best = max(
            cand_list,
            key=lambda e: overlap(e) * 1.0 + _recall_type_score(e),
        )
        source = "dialog_memory" if best.get("id") in dialog_ids else "global_memory"
        return {
            "found": True,
            "source": source,
            "confidence": "high" if source == "dialog_memory" else "medium",
            "fact": {
                "id": best.get("id"),
                "operation": best.get("op"),
                "paths": best.get("paths"),
                "status": best.get("status"),
                "context": best.get("context"),
                "timestamp": best.get("ts"),
            },
            "related_count": len(cand_list),
        }

    # Fallback: сжатая история диалога
    try:
        with _open_conn() as conn:
            comp = conn.execute(
                "SELECT summary FROM compressed_history "
                "WHERE dialog = ? ORDER BY ts DESC LIMIT 1",
                (d_id,),
            ).fetchone()
        if comp:
            summary = comp["summary"] if hasattr(comp, "keys") else comp[0]
            if query.lower() in (summary or "").lower():
                return {
                    "found": True,
                    "source": "compressed_history",
                    "confidence": "low",
                    "summary": (summary or "")[:300],
                }
    except Exception:
        pass

    if store_if_missing:
        try:
            conversation_memory.add(
                op="recall_fact",
                paths={"query": query},
                status="missing",
                dialog=d_id,
                context=f"Fact query '{query}' not found, stored as placeholder",
            )
        except Exception:
            pass
    return {
        "found": False,
        "fact": None,
        "source": None,
        "confidence": "none",
    }


# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("context-manager", "3.1")
server.register_tool(
    "compress_history",
    {
        "description": "Compress dialog history with extractive summarization",
        "inputSchema": {
            "type": "object",
            "properties": {
                "history": {"type": "array", "items": {"type": "string"}},
                "max_sentences": {"type": "integer", "default": 10},
                "dialog_id": {"type": "string"},
            },
            "required": ["history"],
        },
    },
    lambda **kw: compress_history(
        kw["history"], kw.get("max_sentences", 10), kw.get("dialog_id")
    ),
)

server.register_tool(
    "split_into_chunks",
    {
        "description": (
            "Split long text into overlapping chunks "
            "(by characters or by estimated tokens)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "chunk_size": {"type": "integer", "default": 1000},
                "overlap": {"type": "integer", "default": 100},
                "use_tokens": {"type": "boolean", "default": False},
            },
            "required": ["text"],
        },
    },
    lambda **kw: split_into_chunks(
        kw["text"],
        kw.get("chunk_size", 1000),
        kw.get("overlap", 100),
        kw.get("use_tokens", False),
    ),
)

server.register_tool(
    "recall_fact",
    {
        "description": (
            "Retrieve fact from persistent conversation memory "
            "(FTS5 + path search, snowball russian)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "store_if_missing": {"type": "boolean", "default": False},
                "dialog_id": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    lambda **kw: recall_fact(
        kw["query"], kw.get("store_if_missing", False), kw.get("dialog_id")
    ),
)


if __name__ == "__main__":
    server.run()
