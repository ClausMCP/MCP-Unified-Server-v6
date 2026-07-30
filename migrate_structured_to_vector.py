#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migrate Structured → Vector v1.0 (идемпотентный).

Переносит записи conversation_memory (SQLite: entries, archived_entries)
в ChromaDB. Гарантии:
  * Детерминированные ID: sha256(table:id:ts:checksum) — повторный запуск
    делает upsert, дублей не возникает.
  * Watermark по rowid в таблице vector_migration_state (в той же SQLite БД):
    инкрементальный прогон берёт только новые записи; прерывание безопасно —
    watermark двигается после каждого успешного батча.
  * --reset: сброс watermark и полный перепрогон (безопасен благодаря upsert).
  * --verify: сверка количества и выборочная проверка content_hash без
    повторной векторизации.

Эмбеддинги: sentence-transformers (env MCP_EMBED_MODEL,
по умолчанию all-MiniLM-L6-v2). Если библиотека не установлена —
используется встроенная embedding function ChromaDB.

Запуск:
    python migrate_structured_to_vector.py
    python migrate_structured_to_vector.py --include-archive --batch-size 256
    python migrate_structured_to_vector.py --verify
    python migrate_structured_to_vector.py --reset
"""
import os
import sys
import json
import time
import sqlite3
import hashlib
import argparse
from datetime import datetime
from typing import Dict, List, Any, Optional, Iterator, Tuple

# ─── Конфигурация ────────────────────────────────────────────────────────────
def _default_data_dir() -> str:
    base = os.environ.get("MCP_DATA_DIR")
    if not base:
        base = r"C:\Tools" if os.name == "nt" else os.path.join(os.path.expanduser("~"), ".mcp")
    os.makedirs(base, exist_ok=True)
    return base

def _default_memory_db() -> str:
    try:
        from mcp_shared import MEMORY_DB_PATH as _p
        return _p
    except Exception:
        return os.path.join(_default_data_dir(), "mcp_memory.db")

MEMORY_DB_PATH = os.environ.get("MCP_MEMORY_PATH", _default_memory_db())
CHROMA_DIR = os.environ.get("MCP_CHROMA_DIR", os.path.join(_default_data_dir(), "mcp_chroma"))
COLLECTION_NAME = os.environ.get("MCP_CHROMA_COLLECTION", "structured_memory")
EMBED_MODEL = os.environ.get("MCP_EMBED_MODEL",
                             "sentence-transformers/all-MiniLM-L6-v2")
DEFAULT_BATCH = int(os.environ.get("MCP_MIGRATE_BATCH", "128"))

STATE_TABLE = "vector_migration_state"
SOURCES = {"entries": "entries", "archive": "archived_entries"}


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}][Migrate] {msg}",
          file=sys.stderr, flush=True)


# ─── SQLite: чтение и watermark ──────────────────────────────────────────────
def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _ensure_state_table(conn: sqlite3.Connection):
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
            source TEXT PRIMARY KEY,
            last_rowid INTEGER NOT NULL DEFAULT 0,
            migrated_total INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.commit()


def _get_watermark(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        f"SELECT last_rowid FROM {STATE_TABLE} WHERE source = ?", (source,)
    ).fetchone()
    return int(row["last_rowid"]) if row else 0


def _set_watermark(conn: sqlite3.Connection, source: str,
                   last_rowid: int, migrated_delta: int):
    conn.execute(f"""
        INSERT INTO {STATE_TABLE} (source, last_rowid, migrated_total, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_rowid = excluded.last_rowid,
            migrated_total = migrated_total + ?,
            updated_at = excluded.updated_at
    """, (source, last_rowid, migrated_delta,
          datetime.now().isoformat(timespec="seconds"), migrated_delta))
    conn.commit()


def _reset_watermarks(conn: sqlite3.Connection):
    conn.execute(f"DELETE FROM {STATE_TABLE}")
    conn.commit()
    _log("Watermarks reset — следующий запуск перепрогонит всё (upsert, без дублей)")


def _iter_batches(conn: sqlite3.Connection, table: str,
                  after_rowid: int, batch_size: int
                  ) -> Iterator[List[sqlite3.Row]]:
    """Читать записи rowid > after_rowid батчами, по порядку rowid."""
    last = after_rowid
    while True:
        rows = conn.execute(
            f"""SELECT rowid AS _rowid, id, ts, dialog, op, paths_json,
                       context, meta_json, status, checksum
                FROM {table}
                WHERE rowid > ?
                ORDER BY rowid ASC
                LIMIT ?""",
            (last, batch_size)
        ).fetchall()
        if not rows:
            return
        yield rows
        last = rows[-1]["_rowid"]


# ─── Преобразование записи ───────────────────────────────────────────────────
def vector_id(table: str, row: sqlite3.Row) -> str:
    """
    Детерминированный ID. entry id — усечённый timestamp и МОЖЕТ
    коллидировать, поэтому подмешиваем ts и checksum.
    """
    raw = f"{table}:{row['id']}:{row['ts']}:{row['checksum'] or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def row_to_document(row: sqlite3.Row) -> Tuple[str, Dict[str, Any]]:
    """Собрать текст для эмбеддинга и метаданные."""
    parts: List[str] = []
    if row["op"]:
        parts.append(f"op: {row['op']}")
    if row["context"]:
        parts.append(str(row["context"]))
    paths_str = ""
    if row["paths_json"]:
        try:
            paths = json.loads(row["paths_json"])
            paths_str = " ".join(str(v) for v in paths.values() if v)
            if paths_str:
                parts.append(f"paths: {paths_str}")
        except (json.JSONDecodeError, AttributeError):
            pass
    text = "\n".join(parts).strip() or f"op: {row['op'] or 'unknown'}"

    meta: Dict[str, Any] = {
        "source_table": "",  # заполняется вызывающим
        "source_id": row["id"] or "",
        "ts": row["ts"] or "",
        "dialog": row["dialog"] or "",
        "op": row["op"] or "",
        "status": row["status"] or "",
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }
    # category/tags из meta_json — плоско (Chroma не принимает вложенность)
    if row["meta_json"]:
        try:
            m = json.loads(row["meta_json"])
            if m.get("category"):
                meta["category"] = str(m["category"])
            tags = m.get("tags") or []
            if tags:
                meta["tags"] = ",".join(str(t) for t in tags)[:500]
        except (json.JSONDecodeError, AttributeError):
            pass
    return text, meta


# ─── ChromaDB ────────────────────────────────────────────────────────────────
def get_collection():
    try:
        import chromadb
    except ImportError:
        _log("ОШИБКА: chromadb не установлен. pip install chromadb")
        sys.exit(1)

    client = chromadb.PersistentClient(path=CHROMA_DIR)

    embedding_function = None
    try:
        from chromadb.utils import embedding_functions
        embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=EMBED_MODEL)
        _log(f"Эмбеддинги: sentence-transformers ({EMBED_MODEL})")
    except Exception as e:
        _log(f"sentence-transformers недоступен ({e}); "
             f"использую встроенный default EF ChromaDB")

    kwargs = {"name": COLLECTION_NAME, "metadata": {"hnsw:space": "cosine"}}
    if embedding_function is not None:
        kwargs["embedding_function"] = embedding_function
    return client.get_or_create_collection(**kwargs)


# ─── Миграция ────────────────────────────────────────────────────────────────
def migrate(db_path: str, include_archive: bool, batch_size: int) -> Dict[str, Any]:
    if not os.path.exists(db_path):
        _log(f"ОШИБКА: БД не найдена: {db_path}")
        sys.exit(1)

    collection = get_collection()
    conn = _conn(db_path)
    _ensure_state_table(conn)

    sources = ["entries"] + (["archive"] if include_archive else [])
    totals = {"migrated": 0, "skipped_empty": 0, "elapsed_sec": 0.0}
    started = time.monotonic()

    try:
        for source in sources:
            table = SOURCES[source]
            wm = _get_watermark(conn, source)
            _log(f"Источник '{table}': watermark rowid={wm}")
            batch_n = 0

            for rows in _iter_batches(conn, table, wm, batch_size):
                ids, docs, metas = [], [], []
                for row in rows:
                    text, meta = row_to_document(row)
                    if not text.strip():
                        totals["skipped_empty"] += 1
                        continue
                    meta["source_table"] = table
                    ids.append(vector_id(table, row))
                    docs.append(text[:8000])  # защита от гигантских записей
                    metas.append(meta)

                if ids:
                    # upsert = идемпотентность: повтор перезапишет, не задублирует
                    collection.upsert(ids=ids, documents=docs, metadatas=metas)

                last_rowid = rows[-1]["_rowid"]
                _set_watermark(conn, source, last_rowid, len(ids))
                totals["migrated"] += len(ids)
                batch_n += 1
                if batch_n % 10 == 0:
                    _log(f"  ... {totals['migrated']} записей "
                         f"(rowid={last_rowid})")

            _log(f"Источник '{table}' завершён.")
    finally:
        conn.close()

    totals["elapsed_sec"] = round(time.monotonic() - started, 1)
    _log(f"ГОТОВО: перенесено {totals['migrated']}, "
         f"пропущено пустых {totals['skipped_empty']}, "
         f"за {totals['elapsed_sec']}s")
    return totals


# ─── Верификация ─────────────────────────────────────────────────────────────
def verify(db_path: str, include_archive: bool,
           sample_size: int = 50) -> Dict[str, Any]:
    """Сверка количества + выборочная проверка соответствия записей."""
    collection = get_collection()
    conn = _conn(db_path)
    _ensure_state_table(conn)

    report: Dict[str, Any] = {"sources": {}, "vector_total": collection.count()}
    sources = ["entries"] + (["archive"] if include_archive else [])

    try:
        for source in sources:
            table = SOURCES[source]
            wm = _get_watermark(conn, source)
            sql_total = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            sql_migrated = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE rowid <= ?",
                (wm,)).fetchone()["c"]

            # выборочная проверка: берём случайные записи до watermark
            mismatches: List[str] = []
            sample = conn.execute(
                f"""SELECT rowid AS _rowid, id, ts, dialog, op, paths_json,
                           context, meta_json, status, checksum
                    FROM {table} WHERE rowid <= ?
                    ORDER BY RANDOM() LIMIT ?""",
                (wm, sample_size)).fetchall()
            if sample:
                ids = [vector_id(table, r) for r in sample]
                got = collection.get(ids=ids, include=["metadatas"])
                found = set(got.get("ids") or [])
                meta_by_id = dict(zip(got.get("ids") or [],
                                      got.get("metadatas") or []))
                for r, vid in zip(sample, ids):
                    if vid not in found:
                        mismatches.append(f"missing: {table} rowid={r['_rowid']}")
                        continue
                    text, meta = row_to_document(r)
                    stored = meta_by_id.get(vid) or {}
                    if stored.get("content_hash") != meta["content_hash"]:
                        mismatches.append(
                            f"hash mismatch: {table} rowid={r['_rowid']}")

            report["sources"][table] = {
                "sql_total": sql_total,
                "covered_by_watermark": sql_migrated,
                "pending": sql_total - sql_migrated,
                "sampled": len(sample),
                "mismatches": mismatches,
            }
    finally:
        conn.close()

    ok = all(not s["mismatches"] for s in report["sources"].values())
    report["status"] = "ok" if ok else "mismatches_found"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Идемпотентная миграция Structured → Vector (ChromaDB)")
    parser.add_argument("--db", default=MEMORY_DB_PATH,
                        help=f"Путь к SQLite БД памяти (default: {MEMORY_DB_PATH})")
    parser.add_argument("--include-archive", action="store_true",
                        help="Также мигрировать archived_entries")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--reset", action="store_true",
                        help="Сбросить watermark (полный перепрогон, без дублей)")
    parser.add_argument("--verify", action="store_true",
                        help="Только сверка, без миграции")
    args = parser.parse_args()

    if args.reset:
        conn = _conn(args.db)
        _ensure_state_table(conn)
        _reset_watermarks(conn)
        conn.close()
        return

    if args.verify:
        verify(args.db, args.include_archive)
        return

    migrate(args.db, args.include_archive, args.batch_size)


if __name__ == "__main__":
    main()
