#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Dialog Manager v2.0 – миграция на mcp_storage + исправления

Изменения относительно v1.1 (функционал и API сохранены полностью):
  • Все 11 вызовов sqlite3.connect() заменены на единый ConnectionManager
    (mcp_storage) — постоянное thread-local соединение, WAL, busy_timeout,
    retry при блокировке.
  • ИСПРАВЛЕН БАГ: INSERT OR REPLACE в set_name() стирал created_at при
    каждом переименовании диалога. Теперь UPSERT сохраняет дату создания.
  • ИСПРАВЛЕН БАГ: в search()/list_all() ленивые проверки _maybe_verify()
    выполнялись ПОСЛЕ формирования результата — возвращались устаревшие
    флаги deleted/archived, а сама проверка была паттерном N+1
    (2 подключения на каждую строку). Теперь статус обновляется до выдачи,
    результат отражает актуальное состояние, лишних подключений нет.
  • Фоновый поток очистки стал останавливаемым (threading.Event) и
    защищён от дублирования при повторном импорте модуля.
  • Все обращения к conversation_memory._open_conn() обёрнуты в try/finally,
    чтобы соединение гарантированно закрывалось при исключении.
"""
import os
import re
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import mcp_storage as storage
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Конфигурация ────────────────────────────────────────────────────────
_LEGACY_DB = os.environ.get(
    "MCP_DIALOG_DB",
    os.path.join(os.path.dirname(__file__), "dialog_names.db")
)
DB_NAME = "dialogs"
DB_PATH = storage.register(DB_NAME, legacy_path=_LEGACY_DB)  # совместимость: имя сохранено

VERIFY_TTL_SEC = int(os.environ.get("MCP_DIALOG_VERIFY_TTL", "300"))   # 5 минут
ARCHIVE_KEEP_DAYS = int(os.environ.get("MCP_DIALOG_ARCHIVE_DAYS", "7"))
CLEANUP_INTERVAL_SEC = int(os.environ.get("MCP_DIALOG_CLEANUP_SEC", "86400"))  # 24 часа


class DialogManagerDB:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self._init_db()

    # ─── Схема ───────────────────────────────────────────────────────────
    def _init_db(self):
        storage.executescript(self.db_name, """
            CREATE TABLE IF NOT EXISTS dialogs (
                dialog_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_used TEXT DEFAULT CURRENT_TIMESTAMP,
                tags TEXT DEFAULT '',
                deleted INTEGER DEFAULT 0,      -- 1 если диалог удалён из памяти
                archived INTEGER DEFAULT 0,     -- 1 если есть сжатая история (архив)
                last_verified TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_name ON dialogs(name);
            CREATE INDEX IF NOT EXISTS idx_last_used ON dialogs(last_used);
            CREATE INDEX IF NOT EXISTS idx_deleted ON dialogs(deleted);
        """)

    # ─── Проверки против внешней БД памяти (conversation_memory) ────────
    def _is_dialog_alive(self, dialog_id: str) -> bool:
        """Проверяет, есть ли хотя бы одна запись в entries для этого dialog_id."""
        try:
            conn = conversation_memory._open_conn()
            try:
                row = conn.execute(
                    "SELECT 1 FROM entries WHERE dialog = ? LIMIT 1", (dialog_id,)
                ).fetchone()
                return row is not None
            finally:
                conn.close()
        except Exception:
            return False

    def _has_compressed_history(self, dialog_id: str) -> bool:
        """Проверяет наличие сжатой истории в таблице compressed_history."""
        try:
            conn = conversation_memory._open_conn()
            try:
                row = conn.execute(
                    "SELECT 1 FROM compressed_history WHERE dialog = ? LIMIT 1",
                    (dialog_id,)
                ).fetchone()
                return row is not None
            finally:
                conn.close()
        except Exception:
            return False

    def _check_status(self, dialog_id: str) -> Dict[str, int]:
        """Возвращает актуальные флаги {deleted, archived} по внешней памяти."""
        alive = self._is_dialog_alive(dialog_id)
        archived = self._has_compressed_history(dialog_id) if not alive else False
        return {"deleted": 0 if alive else 1, "archived": 1 if archived else 0}

    def _verify_and_update(self, dialog_id: str) -> Dict[str, int]:
        """Проверяет статус диалога и обновляет поля deleted/archived."""
        status = self._check_status(dialog_id)
        storage.execute(self.db_name, """
            UPDATE dialogs
            SET deleted = ?, archived = ?, last_verified = ?
            WHERE dialog_id = ?
        """, (status["deleted"], status["archived"],
              datetime.now().isoformat(), dialog_id))
        return status

    def _maybe_verify(self, dialog_id: str) -> Optional[Dict[str, int]]:
        """Ленивая проверка: обновляет статус, если он старше VERIFY_TTL_SEC.
        Возвращает свежие флаги, если проверка выполнялась, иначе None."""
        row = storage.query_one(
            self.db_name,
            "SELECT last_verified FROM dialogs WHERE dialog_id = ?", (dialog_id,)
        )
        if not row:
            return None
        try:
            last_verified = datetime.fromisoformat(row[0])
        except (TypeError, ValueError):
            return self._verify_and_update(dialog_id)
        if (datetime.now() - last_verified).total_seconds() > VERIFY_TTL_SEC:
            return self._verify_and_update(dialog_id)
        return None

    @staticmethod
    def _is_stale(last_verified: Optional[str]) -> bool:
        """True, если метка last_verified отсутствует/битая/старше TTL."""
        if not last_verified:
            return True
        try:
            ts = datetime.fromisoformat(last_verified)
        except (TypeError, ValueError):
            return True
        return (datetime.now() - ts).total_seconds() > VERIFY_TTL_SEC

    # ─── CRUD ────────────────────────────────────────────────────────────
    def set_name(self, dialog_id: str, name: str) -> bool:
        # При создании/обновлении имени сразу проверяем, жив ли диалог
        status = self._check_status(dialog_id)
        now = datetime.now().isoformat()
        # UPSERT: в отличие от INSERT OR REPLACE не стирает created_at
        storage.execute(self.db_name, """
            INSERT INTO dialogs
                (dialog_id, name, last_used, deleted, archived, last_verified)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(dialog_id) DO UPDATE SET
                name = excluded.name,
                last_used = excluded.last_used,
                deleted = excluded.deleted,
                archived = excluded.archived,
                last_verified = excluded.last_verified
        """, (dialog_id, name, now, status["deleted"], status["archived"], now))
        return True

    def get_name(self, dialog_id: str) -> Optional[str]:
        self._maybe_verify(dialog_id)
        row = storage.query_one(
            self.db_name,
            "SELECT name FROM dialogs WHERE dialog_id = ?", (dialog_id,)
        )
        return row[0] if row else None

    # ─── Выборки ─────────────────────────────────────────────────────────
    def _refresh_rows(self, results: List[Dict], include_deleted: bool) -> List[Dict]:
        """Обновляет устаревшие статусы в выборке и приводит её в актуальный вид.

        Исправление v2.0: раньше проверка шла ПОСЛЕ формирования словарей,
        поэтому клиент получал устаревшие deleted/archived. Теперь свежие
        значения подставляются в результат, а строки, оказавшиеся удалёнными
        (при include_deleted=False), исключаются из выдачи.
        """
        fresh: List[Dict] = []
        for r in results:
            if self._is_stale(r.pop("last_verified", None)):
                status = self._verify_and_update(r["dialog_id"])
                r["deleted"] = status["deleted"]
                r["archived"] = status["archived"]
            if not include_deleted and r.get("deleted"):
                continue
            fresh.append(r)
        return fresh

    def search(self, keyword: str, limit: int = 10,
               include_deleted: bool = False) -> List[Dict]:
        """Поиск диалогов. По умолчанию исключает удалённые (deleted=1)."""
        pattern = f"%{keyword}%"
        query = """
            SELECT dialog_id, name, created_at, last_used, tags,
                   deleted, archived, last_verified
            FROM dialogs
            WHERE (name LIKE ? OR tags LIKE ?)
        """
        params: List = [pattern, pattern]
        if not include_deleted:
            query += " AND deleted = 0"
        query += " ORDER BY last_used DESC LIMIT ?"
        params.append(limit)
        rows = storage.query_all(self.db_name, query, params, row=True)
        return self._refresh_rows([dict(row) for row in rows], include_deleted)

    def list_all(self, limit: int = 50,
                 include_deleted: bool = False) -> List[Dict]:
        query = ("SELECT dialog_id, name, created_at, last_used, tags, "
                 "deleted, archived, last_verified FROM dialogs")
        params: List = []
        if not include_deleted:
            query += " WHERE deleted = 0"
        query += " ORDER BY last_used DESC LIMIT ?"
        params.append(limit)
        rows = storage.query_all(self.db_name, query, params, row=True)
        return self._refresh_rows([dict(row) for row in rows], include_deleted)

    # ─── Обслуживание ────────────────────────────────────────────────────
    def update_last_used(self, dialog_id: str):
        storage.execute(
            self.db_name,
            "UPDATE dialogs SET last_used = ? WHERE dialog_id = ?",
            (datetime.now().isoformat(), dialog_id)
        )

    def cleanup_deleted(self, older_than_days: int = ARCHIVE_KEEP_DAYS) -> int:
        """Удаляет диалоги, помеченные deleted=1 и не использовавшиеся более N дней.
        Возвращает число удалённых строк."""
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        return storage.execute(self.db_name, """
            DELETE FROM dialogs
            WHERE deleted = 1 AND last_used < ?
        """, (cutoff,))

    def mark_archived(self, dialog_id: str):
        storage.execute(
            self.db_name,
            "UPDATE dialogs SET archived = 1 WHERE dialog_id = ?", (dialog_id,)
        )


db = DialogManagerDB()

# ─── Фоновая очистка (останавливаемая, без дублей при повторном импорте) ──
_stop_event = threading.Event()
_cleanup_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()


def _cleanup_loop():
    # wait() вместо sleep(): реагирует на остановку мгновенно
    while not _stop_event.wait(CLEANUP_INTERVAL_SEC):
        try:
            removed = db.cleanup_deleted()
            _log(f"[DialogManager] Cleaned up old deleted dialogs (removed={removed})")
        except Exception as e:
            _log(f"[DialogManager] Cleanup error: {e}")
    storage.close_thread()


def start_background_cleanup():
    """Запускает фоновую очистку (идемпотентно)."""
    global _cleanup_thread
    with _thread_lock:
        if _cleanup_thread is not None and _cleanup_thread.is_alive():
            return
        _stop_event.clear()
        _cleanup_thread = threading.Thread(
            target=_cleanup_loop, daemon=True, name="dialog_cleanup"
        )
        _cleanup_thread.start()


def stop_background_cleanup(timeout: float = 5.0):
    """Корректно останавливает фоновую очистку."""
    _stop_event.set()
    with _thread_lock:
        if _cleanup_thread is not None:
            _cleanup_thread.join(timeout=timeout)


start_background_cleanup()


# ─── Инструменты MCP ─────────────────────────────────────────────────────
def dialog_set_name(name: str, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    if not name or len(name.strip()) < 3:
        return {"status": "error", "message": "Имя должно содержать минимум 3 символа"}
    db.set_name(d_id, name)
    conversation_memory.add(
        op="dialog_set_name",
        paths={"name": name},
        status="named",
        dialog=d_id,
        context=f"Диалог получил имя: {name}"
    )
    return {"status": "ok", "dialog_id": d_id, "name": name}


def dialog_get_name(dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    name = db.get_name(d_id)
    return {"dialog_id": d_id, "name": name if name else "не назван"}


def dialog_search(keyword: str, limit: int = 10, include_deleted: bool = False) -> Dict:
    results = db.search(keyword, limit, include_deleted)
    return {"status": "ok", "keyword": keyword, "results": results, "count": len(results)}


def dialog_list(limit: int = 50, include_deleted: bool = False) -> Dict:
    dialogs = db.list_all(limit, include_deleted)
    return {"status": "ok", "dialogs": dialogs, "count": len(dialogs)}


def dialog_switch(name_or_id: str) -> Dict:
    # Сначала ищем по ID (точное совпадение)
    if db.get_name(name_or_id):
        new_id = name_or_id
    else:
        results = db.search(name_or_id, limit=1, include_deleted=False)
        if not results:
            return {"status": "error", "message": f"Диалог '{name_or_id}' не найден"}
        new_id = results[0]["dialog_id"]

    # Проверяем, есть ли активные записи в памяти
    thread = conversation_memory.get_dialog_thread(dialog=new_id)
    if not thread["entries"]:
        # Нет активных записей – пытаемся восстановить из архива последние 50 сообщений
        try:
            restored = conversation_memory.restore_dialog_from_archive(dialog=new_id, limit=50)
            if restored:
                _log(f"Dialog {new_id}: auto-restored {restored} messages from archive")
        except AttributeError:
            # Если метод отсутствует (старая версия), просто игнорируем
            _log(f"Dialog {new_id}: no active entries and restore_dialog_from_archive not available")

    dialog_ctx.set(new_id)
    db.update_last_used(new_id)
    name = db.get_name(new_id)
    return {"status": "switched", "dialog_id": new_id, "name": name}


def dialog_try_auto_name(query: str, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    existing = db.get_name(d_id)
    if existing:
        return {"status": "already_named", "name": existing}
    words = re.findall(r'\b\w{3,}\b', query)
    name = " ".join(words[:5]) if words else "Новый диалог"
    db.set_name(d_id, name)
    return {"status": "auto_named", "dialog_id": d_id, "name": name}


def dialog_refresh_status(dialog_id: str = None) -> Dict:
    """Принудительно проверить статус диалога (жив/архивирован)."""
    d_id = dialog_id or dialog_ctx.get()
    db._verify_and_update(d_id)
    row = storage.query_one(
        DB_NAME,
        "SELECT deleted, archived FROM dialogs WHERE dialog_id = ?", (d_id,),
        row=True
    )
    if row:
        return {"status": "ok", "dialog_id": d_id,
                "deleted": bool(row["deleted"]), "archived": bool(row["archived"])}
    return {"status": "not_found", "dialog_id": d_id}


def dialog_cleanup(older_than_days: int = ARCHIVE_KEEP_DAYS) -> Dict:
    """Принудительно удалить мёртвые диалоги старше указанного числа дней."""
    db.cleanup_deleted(older_than_days)
    return {"status": "cleaned", "older_than_days": older_than_days}


def dialog_restore_from_archive(dialog_id: str) -> Dict:
    """Восстановить контекст диалога из сжатой истории (архива)."""
    try:
        conn = conversation_memory._open_conn()
        try:
            row = conn.execute(
                "SELECT summary FROM compressed_history WHERE dialog = ? ORDER BY ts DESC LIMIT 1",
                (dialog_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            db.mark_archived(dialog_id)
            return {"status": "success", "dialog_id": dialog_id, "summary": row[0]}
        return {"status": "error", "message": "Нет сжатой истории для этого диалога"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─── Регистрация инструментов ───────────────────────────────────────────
def register_tools(server: BaseMCPServer):
    server.register_tool("dialog_set_name", {
        "description": "Присвоить понятное имя текущему диалогу",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "dialog_id": {"type": "string"}
            },
            "required": ["name"]
        }
    }, lambda **kw: dialog_set_name(kw["name"], kw.get("dialog_id")))

    server.register_tool("dialog_get_name", {
        "description": "Узнать имя текущего диалога",
        "inputSchema": {
            "type": "object",
            "properties": {"dialog_id": {"type": "string"}}
        }
    }, lambda **kw: dialog_get_name(kw.get("dialog_id")))

    server.register_tool("dialog_search", {
        "description": "Найти диалоги по ключевому слову",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "include_deleted": {"type": "boolean", "default": False}
            },
            "required": ["keyword"]
        }
    }, lambda **kw: dialog_search(kw["keyword"], kw.get("limit", 10),
                                  kw.get("include_deleted", False)))

    server.register_tool("dialog_list", {
        "description": "Показать список всех диалогов",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50},
                "include_deleted": {"type": "boolean", "default": False}
            }
        }
    }, lambda **kw: dialog_list(kw.get("limit", 50),
                                kw.get("include_deleted", False)))

    server.register_tool("dialog_switch", {
        "description": "Переключить контекст на диалог по имени или ID",
        "inputSchema": {
            "type": "object",
            "properties": {"name_or_id": {"type": "string"}},
            "required": ["name_or_id"]
        }
    }, lambda **kw: dialog_switch(kw["name_or_id"]))

    server.register_tool("dialog_try_auto_name", {
        "description": "Автоматически назвать диалог на основе запроса",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "dialog_id": {"type": "string"}
            },
            "required": ["query"]
        }
    }, lambda **kw: dialog_try_auto_name(kw["query"], kw.get("dialog_id")))

    server.register_tool("dialog_refresh_status", {
        "description": "Принудительно проверить, существует ли диалог в памяти",
        "inputSchema": {
            "type": "object",
            "properties": {"dialog_id": {"type": "string"}}
        }
    }, lambda **kw: dialog_refresh_status(kw.get("dialog_id")))

    server.register_tool("dialog_cleanup", {
        "description": "Удалить из базы мёртвые диалоги старше N дней",
        "inputSchema": {
            "type": "object",
            "properties": {"older_than_days": {"type": "integer", "default": ARCHIVE_KEEP_DAYS}}
        }
    }, lambda **kw: dialog_cleanup(kw.get("older_than_days", ARCHIVE_KEEP_DAYS)))

    server.register_tool("dialog_restore_from_archive", {
        "description": "Получить сжатую историю диалога из архива",
        "inputSchema": {
            "type": "object",
            "properties": {"dialog_id": {"type": "string"}},
            "required": ["dialog_id"]
        }
    }, lambda **kw: dialog_restore_from_archive(kw["dialog_id"]))


__mcp_plugin__ = {
    "name": "dialog-manager",
    "version": "2.0",
    "description": "Управление диалогами с проверкой актуальности и синхронизацией",
    "dependencies": [],
    "on_load": lambda: _log("[DialogManager] Loaded (v2.0, mcp_storage). Auto-cleanup thread started."),
    "on_unload": lambda: stop_background_cleanup()
}

if __name__ == "__main__":
    server = BaseMCPServer("dialog-manager", "2.0")
    register_tools(server)
    server.run()
