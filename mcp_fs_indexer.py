#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Indexer v3.2 (Context-Isolated & Thread-Safe)
Background content indexing with SQLite FTS5, incremental updates,
and fast full-text search across file contents.
Uses contextvars for secure dialog isolation.

Изменения относительно v3.1 (API всех 3 инструментов сохранён полностью):
  • Все 5 вызовов sqlite3.connect() заменены на mcp_storage — постоянное
    соединение, WAL, busy_timeout, retry при блокировке.
  • ИСПРАВЛЕНО: путь БД был относительным ("mcp_index.db") — файл создавался
    в текущем рабочем каталоге процесса. Теперь путь стабилен (существующий
    legacy-файл рядом с модулем или в CWD подхватывается автоматически).
  • ИСПРАВЛЕН БАГ: триггер fts_update использовал UPDATE по внешне-контентной
    FTS5-таблице — для content='files' таблиц это некорректно (индекс
    рассинхронизировался бы). Триггер пересоздан по канонической схеме
    delete+insert. На практике UPDATE files не выполнялся (index_file делает
    DELETE+INSERT), поэтому существующие индексы не повреждены.
"""
import os
import sys
import json
import time
import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

import mcp_storage as storage
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# Legacy-кандидаты: рядом с модулем и в текущем каталоге (старое поведение)
_module_dir_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_index.db")
_cwd_db = os.path.abspath("mcp_index.db")
_LEGACY = _module_dir_db if os.path.isfile(_module_dir_db) else _cwd_db

DB_NAME = "fs_index"
DB_PATH = storage.register(DB_NAME, legacy_path=_LEGACY)


# ─── Database ───────────────────────────────────────────────────────────────
class ContentIndex:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self.db_path = storage.resolve(db_name)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            storage.executescript(self.db_name, """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    content TEXT,
                    size INTEGER,
                    mtime REAL,
                    indexed_at REAL,
                    hash TEXT
                );
                CREATE TABLE IF NOT EXISTS indexed_roots (
                    root TEXT PRIMARY KEY,
                    last_scan REAL
                );
            """)
            try:
                storage.executescript(self.db_name, """
                    CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                        path, content,
                        content='files', content_rowid='id'
                    );
                    CREATE TRIGGER IF NOT EXISTS fts_insert AFTER INSERT ON files BEGIN
                        INSERT INTO fts(rowid, path, content) VALUES (new.id, new.path, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS fts_delete AFTER DELETE ON files BEGIN
                        INSERT INTO fts(fts, rowid, path, content)
                        VALUES ('delete', old.id, old.path, old.content);
                    END;
                """)
                # Исправление v3.2: триггер UPDATE для внешне-контентной FTS5
                # обязан использовать команды 'delete' + insert, а не UPDATE.
                # Пересоздаём безопасно (на существующих БД старый триггер
                # ни разу не срабатывал — index_file делает DELETE+INSERT).
                storage.executescript(self.db_name, """
                    DROP TRIGGER IF EXISTS fts_update;
                    CREATE TRIGGER fts_update AFTER UPDATE ON files BEGIN
                        INSERT INTO fts(fts, rowid, path, content)
                        VALUES ('delete', old.id, old.path, old.content);
                        INSERT INTO fts(rowid, path, content)
                        VALUES (new.id, new.path, new.content);
                    END;
                """)
            except sqlite3.OperationalError as e:
                _log(f"FTS5 initialization warning: {e}")

    def index_file(self, path: str, content: str, size: int, mtime: float) -> bool:
        h = hashlib.md5(content.encode()).hexdigest()[:16]
        with self._lock:
            with storage.connection(self.db_name) as conn:
                cur = conn.execute("SELECT id, hash FROM files WHERE path = ?", (path,))
                row = cur.fetchone()
                if row:
                    if row[1] == h:
                        return False
                    conn.execute("DELETE FROM files WHERE id = ?", (row[0],))
                conn.execute("""
                    INSERT INTO files (path, content, size, mtime, indexed_at, hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (path, content, size, mtime, time.time(), h))
                return True

    def search(self, query: str, limit: int = 50) -> List[Dict]:
        with self._lock:
            try:
                rows = storage.query_all(self.db_name, """
                    SELECT f.path, f.size, f.mtime, rank
                    FROM fts
                    JOIN files f ON fts.rowid = f.id
                    WHERE fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (query, limit))
                return [
                    {"path": r[0], "size": r[1], "mtime": r[2], "rank": r[3]}
                    for r in rows
                ]
            except sqlite3.OperationalError:
                return []

    def get_stats(self) -> Dict:
        with self._lock:
            files = storage.query_one(self.db_name, "SELECT COUNT(*) FROM files")[0]
            roots = storage.query_one(self.db_name, "SELECT COUNT(*) FROM indexed_roots")[0]
            size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
            return {"files_indexed": files, "roots": roots, "db_size_bytes": size}


_idx = ContentIndex()


# ─── Indexing ───────────────────────────────────────────────────────────────
def build_index(path: str, extensions: List[str] = None, max_size_kb: int = 1024,
                dry_run: bool = False) -> Dict:
    p = Path(normalize_path(path))
    _ensure_allowed(p, "build_index")
    if not p.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    exts = set(e.lower() for e in (extensions or [".txt", ".py", ".md", ".json", ".log", ".csv"]))
    max_bytes = max_size_kb * 1024
    scanned = 0
    indexed = 0
    skipped = 0
    errors = 0
    start_time = time.time()

    for root, dirs, files in os.walk(str(p)):
        for f in files:
            scanned += 1
            fp = Path(root) / f
            if fp.suffix.lower() not in exts:
                skipped += 1
                continue
            try:
                st = fp.stat()
                if st.st_size > max_bytes or st.st_size == 0:
                    skipped += 1
                    continue
                with open(fp, 'r', encoding='utf-8', errors='replace') as fh:
                    content = fh.read(max_bytes)
                if not dry_run:
                    added = _idx.index_file(str(fp), content, st.st_size, st.st_mtime)
                    if added:
                        indexed += 1
            except Exception:
                errors += 1

        if time.time() - start_time > 300:
            _log("Index build timeout reached (5m)")
            break

    if not dry_run:
        storage.execute(_idx.db_name, """
            INSERT OR REPLACE INTO indexed_roots (root, last_scan)
            VALUES (?, ?)
        """, (str(p), time.time()))

        conversation_memory.add(
            op="build_index", paths={"path": str(p)},
            status="ok", dialog=dialog_ctx.get(),
            context=f"Indexed {indexed}/{scanned} files from {str(p)}"
        )

    return {
        "status": "dry_run" if dry_run else "completed",
        "path": str(p),
        "scanned": scanned,
        "indexed": indexed,
        "skipped": skipped,
        "errors": errors,
        "extensions": list(exts),
        "elapsed_sec": round(time.time() - start_time, 2)
    }


def search_indexed(query: str, limit: int = 50) -> Dict:
    results = _idx.search(query, limit)
    return {
        "query": query,
        "results": results,
        "count": len(results),
        "db_stats": _idx.get_stats()
    }


def index_stats() -> Dict:
    return _idx.get_stats()


# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-indexer", "3.2")
server.register_tool("build_index", {
    "description": "Index file contents for full-text search (incremental)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "extensions": {"type": "array", "items": {"type": "string"}},
            "max_size_kb": {"type": "integer", "default": 1024},
            "dry_run": {"type": "boolean", "default": False}
        },
        "required": ["path"]
    }
}, lambda **kw: build_index(
    kw["path"], kw.get("extensions"), kw.get("max_size_kb", 1024), kw.get("dry_run", False)
))

server.register_tool("search_indexed", {
    "description": "Full-text search in indexed content (FTS5)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 50}
        },
        "required": ["query"]
    }
}, lambda **kw: search_indexed(kw["query"], kw.get("limit", 50)))

server.register_tool("index_stats", {
    "description": "Indexer database statistics",
    "inputSchema": {"type": "object", "properties": {}}
}, index_stats)

if __name__ == "__main__":
    server.run()
