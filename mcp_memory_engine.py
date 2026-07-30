#!C:\Tools\.venv\Scripts\python.exe
"""
MCP Memory Engine v5.0 – миграция на mcp_storage + оптимизации поиска.

Изменения относительно v4.7 (API всех 17 инструментов сохранён полностью):
  • Все 8 вызовов sqlite3.connect() заменены на mcp_storage — постоянное
    соединение, WAL, busy_timeout, retry при блокировке. Путь БД прежний
    (MEMORY_DB_PATH из mcp_shared) — данные сохраняются.
  • ИСПРАВЛЕН БАГ: регистронезависимый поиск не работал для кириллицы —
    LOWER() в SQLite понижает регистр только у ASCII, поэтому поиск
    «резервная» не находил «Резервная копия». Теперь регистр сворачивается
    Python-функцией py_lower, зарегистрированной в соединении.
  • ОПТИМИЗАЦИЯ: в list_all_dialogs_with_summary фильтрация по search
    выполняла 2 SQL-запроса на КАЖДЫЙ диалог (N+1). Теперь — 2 запроса
    на всю выборку (DISTINCT dialog по активным и архивным записям).
  • Пагинация и краткое содержание, поддержка dialog_id в mem_thread,
    все инструменты для работы с большим списком диалогов — без изменений.
"""
import os
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Union

import mcp_storage as storage
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx, MEMORY_DB_PATH
)

# Пытаемся импортировать dialog_manager для получения имён диалогов (опционально)
try:
    from dialog_manager import db as dialog_db
    HAS_DIALOG_MANAGER = True
except ImportError:
    HAS_DIALOG_MANAGER = False
    _log("[MemoryEngine] dialog_manager not available, dialog names will be missing")



def _mem_conn():
    """Соединение с БД памяти + регистрация py_lower для кириллицы (v5.0).

    create_function идемпотентен и дёшев, поэтому регистрируем при каждом
    получении соединения — это надёжнее, чем отслеживать соединения
    (sqlite3.Connection не поддерживает слабые ссылки).
    """
    conn = storage.get(MEMORY_DB_PATH)
    conn.create_function(
        "py_lower", 1,
        lambda s: s.lower() if isinstance(s, str) else s
    )
    return conn


class MemoryEngine:
    def __init__(self, db_path: str = MEMORY_DB_PATH):
        self.db_path = db_path
        self._ensure_tables()

    def _ensure_tables(self):
        storage.executescript(self.db_path, """
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                dialog TEXT NOT NULL,
                op TEXT NOT NULL,
                paths_json TEXT,
                context TEXT,
                meta_json TEXT,
                status TEXT,
                related_json TEXT,
                checksum TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dialog ON entries (dialog);
            CREATE INDEX IF NOT EXISTS idx_op ON entries (op);
            CREATE INDEX IF NOT EXISTS idx_ts ON entries (ts);
            CREATE INDEX IF NOT EXISTS idx_dialog_op ON entries (dialog, op);

            CREATE TABLE IF NOT EXISTS mem_snapshots (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                dialog TEXT NOT NULL,
                note TEXT,
                state_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snap_dialog ON mem_snapshots (dialog);
            CREATE INDEX IF NOT EXISTS idx_snap_ts ON mem_snapshots (ts);

            CREATE TABLE IF NOT EXISTS compressed_history (
                dialog TEXT NOT NULL,
                ts TEXT NOT NULL,
                summary TEXT NOT NULL,
                original_count INTEGER,
                compressed_count INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_comp_dialog ON compressed_history (dialog);

            CREATE TABLE IF NOT EXISTS archived_entries (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                dialog TEXT NOT NULL,
                op TEXT NOT NULL,
                paths_json TEXT,
                context TEXT,
                meta_json TEXT,
                status TEXT,
                related_json TEXT,
                checksum TEXT,
                archived_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_arch_dialog ON archived_entries (dialog);
            CREATE INDEX IF NOT EXISTS idx_arch_ts ON archived_entries (ts);
            CREATE INDEX IF NOT EXISTS idx_arch_op ON archived_entries (op);
        """)

    @staticmethod
    def _auto_categorize(path: str) -> Dict[str, Union[str, List[str]]]:
        pl = path.lower()
        name = os.path.basename(pl)
        ext = os.path.splitext(name)[1]
        if any(k in pl for k in ("tools", "utils", "soft", "setup", "install")):
            cat = "tools"
        elif any(k in pl for k in ("tv", "video", "movie", "movies")):
            cat = "media_tv"
        elif any(k in pl for k in ("music", "audio", "sound", "mp3", "lossless")):
            cat = "media_audio"
        elif any(k in pl for k in ("game", "games", "gaming")):
            cat = "games"
        elif any(k in pl for k in ("doc", "office", "docs", "work", "project")):
            cat = "docs"
        elif any(k in pl for k in ("backup", "archive", "old", "trash", "temp")):
            cat = "archive"
        else:
            cat = "other"
        tags = []
        ext_map = {
            ('.exe', '.msi', '.bat', '.ps1', '.cmd', '.vbs'): "#executable",
            ('.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv'): "#video",
            ('.mp3', '.flac', '.wav', '.aac', '.ogg', '.m4a'): "#audio",
            ('.iso', '.img', '.vhd', '.vhdx'): "#disk_image",
            ('.zip', '.rar', '.7z', '.tar', '.gz', '.bz2'): "#archive",
            ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff'): "#image",
            ('.doc', '.docx', '.pdf', '.txt', '.rtf', '.odt', '.ppt', '.pptx'): "#document",
        }
        for exts, tag in ext_map.items():
            if ext in exts:
                tags.append(tag)
                break
        if any(k in name for k in ("defender", "security", "crypt", "antivir", "firewall")):
            tags.append("#security")
        return {"category": cat, "tags": list(set(tags)), "ext": ext if ext else "no_ext"}

    def add(self, op: str, paths: Union[str, Dict], status: str,
            dialog: str = None, meta: Dict = None,
            context: str = None, related: List[str] = None) -> str:
        d_id = dialog or dialog_ctx.get()
        target = ""
        if isinstance(paths, dict):
            target = paths.get("to") or paths.get("path") or ""
        elif isinstance(paths, str):
            target = paths
        auto = self._auto_categorize(target)
        return conversation_memory.add(
            op=op, paths=paths, status=status, dialog=d_id, meta=meta,
            context=context, related=related,
            category=auto["category"], tags=auto["tags"]
        )

    def query(self, dialog: str = None, op: str = None, path: str = None,
              category: str = None, tags: List[str] = None, ext: str = None,
              hours: int = None, limit: int = 20,
              include_related: bool = False, include_context: bool = True) -> List[Dict]:
        d_id = dialog or dialog_ctx.get()
        return conversation_memory.query(
            dialog=d_id, op=op, path=path, category=category, tags=tags,
            ext=ext, hours=hours, limit=limit, include_context=include_context
        )

    def get_thread(self, dialog: str = None, limit: int = 100) -> Dict:
        d_id = dialog or dialog_ctx.get()
        return conversation_memory.get_dialog_thread(dialog=d_id, limit=limit)

    def save_snapshot(self, state: Dict, dialog: str = None, note: str = "") -> str:
        d_id = dialog or dialog_ctx.get()
        snap_id = f"snap_{hashlib.sha256(json.dumps(state).encode()).hexdigest()[:8]}"
        storage.execute(
            self.db_path,
            "INSERT INTO mem_snapshots (id, ts, dialog, note, state_json) VALUES (?, ?, ?, ?, ?)",
            (snap_id, datetime.now().isoformat(), d_id, note, json.dumps(state, default=str))
        )
        return snap_id

    def get_snapshot(self, dialog: str = None, latest: bool = True) -> Optional[Dict]:
        d_id = dialog or dialog_ctx.get()
        rows = storage.query_all(
            self.db_path,
            "SELECT * FROM mem_snapshots WHERE dialog = ? ORDER BY ts DESC LIMIT ?",
            (d_id, 1 if latest else 999),
            row=True
        )
        if not rows:
            return None if latest else []
        if latest:
            r = rows[0]
            return {
                "id": r["id"], "ts": r["ts"], "dialog": r["dialog"],
                "note": r["note"], "state": json.loads(r["state_json"])
            }
        else:
            return [{
                "id": r["id"], "ts": r["ts"], "dialog": r["dialog"],
                "note": r["note"], "state": json.loads(r["state_json"])
            } for r in rows]

    def get_stats(self) -> Dict:
        stats = conversation_memory.get_stats()
        snap_count = storage.query_one(self.db_path, "SELECT COUNT(*) FROM mem_snapshots")[0]
        stats["snapshots"] = snap_count
        dialog_count = storage.query_one(self.db_path, "SELECT COUNT(DISTINCT dialog) FROM entries")[0]
        stats["distinct_dialogs"] = dialog_count
        return stats

    def clear_all(self, dry_run: bool = False) -> Dict:
        if dry_run:
            return self.get_stats()
        with storage.connection(self.db_path) as conn:
            conn.execute("DELETE FROM entries")
            conn.execute("DELETE FROM mem_snapshots")
        return {"removed": "all", "remaining": 0, "message": "Memory & Snapshots cleared"}

    def list_dialogs(self, limit: int = 50) -> Dict:
        rows = storage.query_all(self.db_path, """
            SELECT dialog, COUNT(*) as entries,
                   MIN(ts) as first_seen, MAX(ts) as last_seen
            FROM entries
            GROUP BY dialog
            ORDER BY last_seen DESC
            LIMIT ?
        """, (limit,), row=True)
        dialogs = [{
            "dialog_id": row["dialog"],
            "entries": row["entries"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"]
        } for row in rows]
        return {"status": "success", "dialogs": dialogs, "count": len(dialogs)}

    # --- ПАГИНАЦИЯ И ПОИСК ---
    def get_dialog_name(self, dialog_id: str) -> str:
        """Получить название диалога из dialog_manager, если доступно."""
        if HAS_DIALOG_MANAGER:
            try:
                name = dialog_db.get_name(dialog_id)
                if name:
                    return name
            except Exception:
                pass
        return ""

    def list_all_dialogs_with_summary(self, offset: int = 0, limit: int = 20, search: str = None) -> Dict:
        """
        Возвращает список диалогов (активных + архивных) с кратким содержанием.
        Поддерживает пагинацию (offset, limit) и поиск по search (в context, op, или имени диалога).
        """
        conn = _mem_conn()
        try:
            # 1. Все уникальные dialog_id из entries и archived_entries
            active_dialogs = {r[0] for r in conn.execute("SELECT DISTINCT dialog FROM entries").fetchall()}
            archived_dialogs = {r[0] for r in conn.execute("SELECT DISTINCT dialog FROM archived_entries").fetchall()}
            all_dialog_ids = sorted(active_dialogs.union(archived_dialogs), key=lambda x: x.lower())

            # 2. Фильтрация по поисковому запросу.
            # Оптимизация v5.0: вместо 2 запросов на КАЖДЫЙ диалог (N+1) —
            # 2 запроса на всю выборку. py_lower обеспечивает
            # регистронезависимость для кириллицы (LOWER в SQLite — только ASCII).
            if search:
                search_lower = search.lower()
                pattern = f"%{search_lower}%"
                matched = {r[0] for r in conn.execute(
                    "SELECT DISTINCT dialog FROM entries WHERE py_lower(context) LIKE ?",
                    (pattern,)
                ).fetchall()}
                matched |= {r[0] for r in conn.execute(
                    "SELECT DISTINCT dialog FROM archived_entries WHERE py_lower(context) LIKE ?",
                    (pattern,)
                ).fetchall()}
                all_dialog_ids = [
                    d_id for d_id in all_dialog_ids
                    if d_id in matched or search_lower in self.get_dialog_name(d_id).lower()
                ]

            total = len(all_dialog_ids)
            paginated_ids = all_dialog_ids[offset:offset + limit]
            results = []

            for d_id in paginated_ids:
                name = self.get_dialog_name(d_id)
                active_count = conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE dialog = ?", (d_id,)).fetchone()[0]
                archived_count = conn.execute(
                    "SELECT COUNT(*) FROM archived_entries WHERE dialog = ?", (d_id,)).fetchone()[0]
                total_entries = active_count + archived_count

                # Краткое содержание: последние 2 активные записи,
                # если активных нет — последние 2 из архива
                summary_parts = []
                active_rows = conn.execute(
                    "SELECT op, context, ts FROM entries WHERE dialog = ? ORDER BY ts DESC LIMIT 2",
                    (d_id,)
                ).fetchall()
                for row in active_rows:
                    op, ctx = row[0], (row[1] or "")[:80]
                    summary_parts.append(f"{op}: {ctx}")
                if not active_rows:
                    arch_rows = conn.execute(
                        "SELECT op, context, ts FROM archived_entries WHERE dialog = ? ORDER BY ts DESC LIMIT 2",
                        (d_id,)
                    ).fetchall()
                    for row in arch_rows:
                        op, ctx = row[0], (row[1] or "")[:80]
                        summary_parts.append(f"{op}: {ctx}")

                summary = "; ".join(summary_parts) if summary_parts else "Нет записей"
                if len(summary) > 200:
                    summary = summary[:197] + "..."

                results.append({
                    "dialog_id": d_id,
                    "name": name if name else None,
                    "total_entries": total_entries,
                    "active_entries": active_count,
                    "archived_entries": archived_count,
                    "summary": summary
                })

            return {
                "status": "success",
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + limit < total,
                "dialogs": results
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def search_dialogs(self, query: str, limit: int = 20, offset: int = 0,
                       include_archived: bool = True) -> Dict:
        """
        Поиск по контексту сообщений и операциям в диалогах (активных и архивных).
        Возвращает диалоги, где найдено совпадение, с краткой выдержкой.
        """
        conn = _mem_conn()
        try:
            search_pattern = f"%{query.lower()}%"
            dialog_scores = {}  # dialog_id -> список совпадений

            # Поиск в активных записях (py_lower — кириллица тоже сворачивается)
            rows = conn.execute("""
                SELECT dialog, op, context, ts
                FROM entries
                WHERE py_lower(context) LIKE ? OR py_lower(op) LIKE ?
                ORDER BY ts DESC
            """, (search_pattern, search_pattern)).fetchall()
            for row in rows:
                d_id = row[0]
                dialog_scores.setdefault(d_id, []).append({
                    "source": "active",
                    "op": row[1],
                    "preview": (row[2] or "")[:150],
                    "ts": row[3]
                })

            if include_archived:
                rows = conn.execute("""
                    SELECT dialog, op, context, ts
                    FROM archived_entries
                    WHERE py_lower(context) LIKE ? OR py_lower(op) LIKE ?
                    ORDER BY ts DESC
                """, (search_pattern, search_pattern)).fetchall()
                for row in rows:
                    d_id = row[0]
                    dialog_scores.setdefault(d_id, []).append({
                        "source": "archived",
                        "op": row[1],
                        "preview": (row[2] or "")[:150],
                        "ts": row[3]
                    })

            # Сортируем диалоги по релевантности
            dialogs_list = []
            for d_id, matches in dialog_scores.items():
                name = self.get_dialog_name(d_id)
                matches_sorted = sorted(matches, key=lambda x: x["ts"], reverse=True)
                latest_match = matches_sorted[0]
                dialogs_list.append({
                    "dialog_id": d_id,
                    "name": name if name else None,
                    "match_count": len(matches),
                    "latest_match_op": latest_match["op"],
                    "latest_match_preview": latest_match["preview"],
                    "latest_match_source": latest_match["source"]
                })
            dialogs_list.sort(key=lambda x: x["match_count"], reverse=True)

            total = len(dialogs_list)
            paginated = dialogs_list[offset:offset + limit]

            return {
                "status": "success",
                "query": query,
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + limit < total,
                "dialogs": paginated
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def search_archive(self, dialog: str = None, op: str = None, path: str = None,
                       category: str = None, tags: List[str] = None,
                       hours: int = None, limit: int = 100) -> Dict:
        results = conversation_memory.search_archive(
            dialog=dialog, op=op, path=path,
            category=category, tags=tags,
            hours=hours, limit=limit
        )
        return {"status": "success", "count": len(results), "results": results}

    def restore_from_archive(self, entry_id: str, target_dialog: str = None) -> Dict:
        if not entry_id or not entry_id.strip():
            return {"status": "error", "message": "entry_id is required and cannot be empty"}
        success = conversation_memory.restore_from_archive(entry_id.strip(), target_dialog)
        if success:
            return {"status": "restored", "entry_id": entry_id}
        else:
            return {"status": "error", "message": f"Entry {entry_id} not found in archive", "entry_id": entry_id}

    def restore_dialog_from_archive(self, dialog_id: str, limit: int = 50) -> Dict:
        restored = conversation_memory.restore_dialog_from_archive(dialog_id, limit)
        if restored == 0:
            return {
                "status": "error",
                "message": f"Dialog {dialog_id} not found in archive",
                "dialog_id": dialog_id,
                "restored_count": 0
            }
        return {
            "status": "restored",
            "dialog_id": dialog_id,
            "restored_count": restored
        }

    def purge_archive(self, older_than_days: int = 730) -> Dict:
        return conversation_memory.purge_archive(older_than_days)

    def optimize_database(self) -> Dict:
        # VACUUM выполняется через собственные соединения conversation_memory
        # (mcp_shared) — их жизненный цикл управляется там же.
        with conversation_memory._lock:
            conn = conversation_memory._open_conn()
            try:
                conn.execute("VACUUM")
                conn.commit()
            finally:
                conn.close()
            if hasattr(conversation_memory.chunk_cache, 'cleanup'):
                conversation_memory.chunk_cache.cleanup()
        return {"status": "ok", "message": "Database vacuumed and chunk cache cleaned"}

    def archive_stats(self) -> Dict:
        return conversation_memory.archive_stats()


# ─── ФУНКЦИЯ ЛОГИРОВАНИЯ ────────────────────────────────────────────────
def log_conversation(role: str, content: str, dialog_id: Optional[str] = None) -> str:
    d_id = dialog_id or dialog_ctx.get()
    if not content or not content.strip():
        return "Error: Empty content"
    conversation_memory.add(
        op="conversation",
        paths={"role": role},
        status="logged",
        dialog=d_id,
        context=content.strip(),
        category="chat",
        tags=[role]
    )
    return "OK"


# ─── ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР И СЕРВЕР ────────────────────────────────────────
_engine = MemoryEngine(MEMORY_DB_PATH)
server = BaseMCPServer("memory-engine", "5.0")

server.register_tool("mem_add", {
    "description": "Add entry to persistent memory",
    "inputSchema": {"type": "object", "properties": {
        "op": {"type": "string"}, "paths": {"type": ["string", "object"]},
        "status": {"type": "string"}, "dialog": {"type": "string"},
        "meta": {"type": "object"}, "context": {"type": "string"},
        "related": {"type": "array", "items": {"type": "string"}}
    }, "required": ["op", "paths", "status"]}
}, lambda **kw: _engine.add(**kw))

server.register_tool("mem_query", {
    "description": "Query persistent memory",
    "inputSchema": {"type": "object", "properties": {
        "dialog": {"type": "string"}, "op": {"type": "string"},
        "path": {"type": "string"}, "category": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "ext": {"type": "string"}, "hours": {"type": "integer"},
        "limit": {"type": "integer", "default": 20}
    }}
}, lambda **kw: _engine.query(**kw))

server.register_tool("mem_thread", {
    "description": "Get conversation thread by dialog ID",
    "inputSchema": {
        "type": "object",
        "properties": {
            "dialog_id": {"type": "string", "description": "Dialog ID (preferred)"},
            "dialog": {"type": "string", "description": "Dialog ID (legacy)"},
            "limit": {"type": "integer", "default": 100}
        }
    }
}, lambda **kw: _engine.get_thread(dialog=kw.get("dialog_id") or kw.get("dialog"), limit=kw.get("limit", 100)))

server.register_tool("mem_snapshot", {
    "description": "Save state snapshot",
    "inputSchema": {"type": "object", "properties": {
        "state": {"type": "object"}, "dialog": {"type": "string"}, "note": {"type": "string"}
    }, "required": ["state"]}
}, lambda **kw: _engine.save_snapshot(**kw))

server.register_tool("mem_get_snapshot", {
    "description": "Retrieve latest snapshot",
    "inputSchema": {"type": "object", "properties": {"dialog": {"type": "string"}}}
}, lambda **kw: _engine.get_snapshot(**kw))

server.register_tool("mem_stats", {
    "description": "Engine statistics",
    "inputSchema": {"type": "object"}
}, lambda **kw: _engine.get_stats())

server.register_tool("mem_clear", {
    "description": "Clear all memory",
    "inputSchema": {"type": "object", "properties": {"dry_run": {"type": "boolean", "default": False}}}
}, lambda **kw: _engine.clear_all(kw.get('dry_run', False)))

server.register_tool("mem_list_dialogs", {
    "description": "List all dialog IDs in memory with entry counts and timestamps (basic)",
    "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}}
}, lambda **kw: _engine.list_dialogs(kw.get('limit', 50)))

server.register_tool("mem_list_dialogs_summary", {
    "description": "List dialogs with brief summary (active + archived). Supports pagination (offset/limit) and search by keyword.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "offset": {"type": "integer", "default": 0, "description": "Starting offset (0 = first page)"},
            "limit": {"type": "integer", "default": 20, "description": "Number of dialogs per page (max 40)"},
            "search": {"type": "string", "description": "Optional search keyword (in context, op, or dialog name)"}
        }
    }
}, lambda **kw: _engine.list_all_dialogs_with_summary(
    offset=kw.get("offset", 0),
    limit=min(kw.get("limit", 20), 40),
    search=kw.get("search")
))

server.register_tool("mem_search_dialogs", {
    "description": "Search dialog history by keyword (context or operation). Returns dialogs with match preview.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keyword or phrase"},
            "limit": {"type": "integer", "default": 20},
            "offset": {"type": "integer", "default": 0},
            "include_archived": {"type": "boolean", "default": True}
        },
        "required": ["query"]
    }
}, lambda **kw: _engine.search_dialogs(
    query=kw["query"],
    limit=min(kw.get("limit", 20), 40),
    offset=kw.get("offset", 0),
    include_archived=kw.get("include_archived", True)
))

server.register_tool("mem_search_archive", {
    "description": "Поиск в долгосрочном архиве",
    "inputSchema": {
        "type": "object",
        "properties": {
            "dialog": {"type": "string"},
            "op": {"type": "string"},
            "path": {"type": "string"},
            "category": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "hours": {"type": "integer"},
            "limit": {"type": "integer", "default": 100}
        }
    }
}, lambda **kw: _engine.search_archive(**kw))

server.register_tool("mem_restore_from_archive", {
    "description": "Восстановить одну запись из архива",
    "inputSchema": {
        "type": "object",
        "properties": {
            "entry_id": {"type": "string"},
            "target_dialog": {"type": "string"}
        },
        "required": ["entry_id"]
    }
}, lambda **kw: _engine.restore_from_archive(kw["entry_id"], kw.get("target_dialog")))

server.register_tool("mem_restore_dialog", {
    "description": "Восстановить последние N сообщений диалога из архива в активную память",
    "inputSchema": {
        "type": "object",
        "properties": {
            "dialog_id": {"type": "string"},
            "limit": {"type": "integer", "default": 50}
        },
        "required": ["dialog_id"]
    }
}, lambda **kw: _engine.restore_dialog_from_archive(kw["dialog_id"], kw.get("limit", 50)))

server.register_tool("mem_purge_archive", {
    "description": "Удалить из архива записи старше N дней (по умолчанию 730)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "older_than_days": {"type": "integer", "default": 730}
        }
    }
}, lambda **kw: _engine.purge_archive(kw.get("older_than_days", 730)))

server.register_tool("mem_optimize", {
    "description": "Запустить VACUUM и очистку кеша чанков",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: _engine.optimize_database())

server.register_tool("mem_archive_stats", {
    "description": "Статистика архива",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: _engine.archive_stats())

server.register_tool("log_conversation", {
    "description": "Сохранить сообщение диалога (user/assistant) в память. Вызывай перед каждым своим ответом.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": ["user", "assistant"]},
            "content": {"type": "string"},
            "dialog_id": {"type": "string"}
        },
        "required": ["role", "content"]
    }
}, log_conversation)

if __name__ == "__main__":
    server.run()
