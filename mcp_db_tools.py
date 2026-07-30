#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP DB Tools v1.0 — работа с базами данных и их оптимизация (офлайн).

  • sql_query — выполнить SQL по SQLite-БД (SELECT/PRAGMA свободно; запись —
    только при allow_write=true). Работает с любыми .db/.sqlite файлами и с
    собственными БД проекта.
  • list_tables / db_info — структура БД, таблицы и число строк, размеры.
  • optimize_all_databases — VACUUM + ANALYZE по ВСЕМ БД проекта (сжатие/очистка),
    с отчётом «было/стало». Расширяет mem_optimize (который сжимал только память).

Только стандартный sqlite3 — без интернета и сторонних пакетов.
"""
import os
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Any

from mcp_shared import BaseMCPServer, _log, conversation_memory, normalize_path, _ensure_allowed

_WRITE_KEYWORDS = ("insert", "update", "delete", "replace", "create", "drop",
                   "alter", "truncate")


def _data_dir() -> str:
    p = getattr(conversation_memory, "db_path", None) or "."
    return os.path.dirname(os.path.abspath(p)) or "."


def _resolve_db(db_path: str) -> Path:
    """Разрешает путь к БД: внутренние БД проекта — без allowlist, иначе проверка."""
    p = Path(normalize_path(db_path)).resolve()
    data_dir = Path(_data_dir()).resolve()
    try:
        inside = str(p).startswith(str(data_dir))
    except Exception:
        inside = False
    if not inside:
        _ensure_allowed(p, "sql_query")
    return p


def sql_query(db_path: str, query: str, params: Optional[list] = None,
              limit: int = 1000, allow_write: bool = False) -> Dict[str, Any]:
    """Выполнить SQL. SELECT/PRAGMA — свободно; изменяющие запросы — только allow_write=true."""
    p = _resolve_db(db_path)
    if not p.exists():
        return {"status": "error", "error": f"БД не найдена: {p}"}
    q = (query or "").strip()
    if not q:
        return {"status": "error", "error": "пустой запрос"}
    first = q.lstrip("(").split(None, 1)[0].lower() if q else ""
    is_write = first in _WRITE_KEYWORDS
    if is_write and not allow_write:
        return {"status": "blocked", "error": "Изменяющий запрос требует allow_write=true",
                "detected": first}
    try:
        conn = sqlite3.connect(str(p), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(q, tuple(params) if params else ())
            if is_write:
                conn.commit()
                return {"status": "success", "write": True, "rowcount": cur.rowcount}
            rows = cur.fetchmany(max(1, min(int(limit), 100000)))
            cols = [d[0] for d in cur.description] if cur.description else []
            return {
                "status": "success",
                "write": False,
                "columns": cols,
                "row_count": len(rows),
                "rows": [dict(r) for r in rows],
                "truncated": len(rows) >= limit,
            }
        finally:
            conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def list_tables(db_path: str) -> Dict[str, Any]:
    """Список таблиц БД с числом строк."""
    p = _resolve_db(db_path)
    if not p.exists():
        return {"status": "error", "error": f"БД не найдена: {p}"}
    try:
        conn = sqlite3.connect(str(p), timeout=10)
        try:
            tabs = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()]
            out = []
            for t in tabs:
                try:
                    n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                except Exception:
                    n = None
                out.append({"table": t, "rows": n})
            return {"status": "success", "db": str(p), "tables": out}
        finally:
            conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def db_info(db_path: str) -> Dict[str, Any]:
    """Сводка по БД: размер, число таблиц, режим журнала."""
    p = _resolve_db(db_path)
    if not p.exists():
        return {"status": "error", "error": f"БД не найдена: {p}"}
    try:
        conn = sqlite3.connect(str(p), timeout=10)
        try:
            tcount = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            return {
                "status": "success", "db": str(p),
                "size_bytes": p.stat().st_size,
                "tables": tcount, "journal_mode": journal,
                "page_count": page_count, "page_size": page_size,
            }
        finally:
            conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def optimize_all_databases() -> Dict[str, Any]:
    """VACUUM + ANALYZE по всем БД проекта (сжатие и очистка). Отчёт было/стало."""
    data_dir = _data_dir()
    results = []
    total_before = total_after = 0
    for f in sorted(Path(data_dir).glob("*.db")):
        if f.name.endswith("-wal") or f.name.endswith("-shm"):
            continue
        before = f.stat().st_size if f.exists() else 0
        item = {"db": f.name, "before_bytes": before}
        try:
            conn = sqlite3.connect(str(f), timeout=15)
            try:
                conn.execute("VACUUM")
                conn.execute("ANALYZE")
                conn.commit()
            finally:
                conn.close()
            after = f.stat().st_size
            item.update({"after_bytes": after, "saved_bytes": before - after, "status": "ok"})
            total_before += before
            total_after += after
        except Exception as e:
            item.update({"status": "error", "error": f"{type(e).__name__}: {e}"})
        results.append(item)
    _log(f"[DBTools] optimize_all: {len(results)} БД, освобождено {total_before - total_after} байт")
    return {
        "status": "success",
        "databases": results,
        "total_before_bytes": total_before,
        "total_after_bytes": total_after,
        "total_saved_bytes": total_before - total_after,
    }


def register_tools(server: BaseMCPServer):
    server.register_tool("sql_query", {
        "description": "Run SQL on a SQLite database (.db/.sqlite). SELECT/PRAGMA freely; write queries "
                       "(INSERT/UPDATE/DELETE/CREATE/...) require allow_write=true. Offline.",
        "inputSchema": {"type": "object", "properties": {
            "db_path": {"type": "string"},
            "query": {"type": "string"},
            "params": {"type": "array", "items": {"type": "string"}, "description": "Optional bound parameters"},
            "limit": {"type": "integer", "description": "Max rows for SELECT (default 1000)"},
            "allow_write": {"type": "boolean", "description": "Must be true for data-modifying queries"}
        }, "required": ["db_path", "query"]}
    }, lambda **kw: sql_query(kw["db_path"], kw["query"], kw.get("params"), kw.get("limit", 1000), kw.get("allow_write", False)))

    server.register_tool("list_tables", {
        "description": "List tables in a SQLite DB with row counts.",
        "inputSchema": {"type": "object", "properties": {"db_path": {"type": "string"}}, "required": ["db_path"]}
    }, lambda **kw: list_tables(kw["db_path"]))

    server.register_tool("db_info", {
        "description": "SQLite DB summary: size, table count, journal mode, page stats.",
        "inputSchema": {"type": "object", "properties": {"db_path": {"type": "string"}}, "required": ["db_path"]}
    }, lambda **kw: db_info(kw["db_path"]))

    server.register_tool("optimize_all_databases", {
        "description": "VACUUM + ANALYZE every project database (compaction/cleanup) with before/after "
                       "sizes. Broader than mem_optimize (which only compacts memory).",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: optimize_all_databases())


__mcp_plugin__ = {
    "name": "db-tools",
    "version": "1.0.0",
    "description": "SQL queries and full-database optimization (sql_query, list_tables, db_info, optimize_all_databases)",
    "dependencies": [],
    "on_load": lambda: _log("[db-tools] v1.0 loaded — tools: sql_query, list_tables, db_info, optimize_all_databases"),
}

if __name__ == "__main__":
    print(json.dumps(optimize_all_databases(), indent=2, ensure_ascii=False, default=str))
