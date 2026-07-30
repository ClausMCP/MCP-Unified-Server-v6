#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Dialog Indexer v1.1 – автоматическая индексация диалогов LM Studio в RAG.
Отслеживает папку ~/.lmstudio/conversations и при изменении файлов вызывает rag_index_folder.
Использует хеширование для инкрементальной индексации.

Изменения относительно v1.0 (API всех 5 инструментов сохранён полностью):
  • Все 5 вызовов sqlite3.connect() заменены на mcp_storage — постоянное
    соединение, WAL, busy_timeout, retry. Существующий dialog_indexer.db
    подхватывается без миграции.
  • ИСПРАВЛЕНО: dialog_indexer_status возвращал None вместо false в поле
    watcher_running, если watcher ни разу не запускался (нарушение JSON-контракта).
  • Watcher-цикл переведён на Event.wait() — остановка мгновенная,
    соединения потока закрываются при выходе.
"""

import os
import json
import time
import hashlib
import threading
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime

import mcp_storage as storage
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx,
    normalize_path, _ensure_allowed
)
from mcp_rag_engine import rag_index_folder, rag_search

# ─── Конфигурация ──────────────────────────────────────────────────────────
LM_STUDIO_CHATS_PATH = Path.home() / ".lmstudio" / "conversations"
RAG_COLLECTION_NAME = "lmstudio_dialogs"
_LEGACY_DB = Path(__file__).parent / "dialog_indexer.db"
DB_NAME = "dialog_indexer"
INDEX_DB_PATH = Path(storage.register(DB_NAME, legacy_path=str(_LEGACY_DB)))  # имя сохранено
CHECK_INTERVAL_SEC = 30  # частота проверки изменений (сек)


# ─── База данных хешей ────────────────────────────────────────────────────
class IndexTracker:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self._init_db()

    def _init_db(self):
        storage.executescript(self.db_name, """
            CREATE TABLE IF NOT EXISTS indexed_files (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                last_indexed REAL NOT NULL,
                collection_name TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_collection ON indexed_files(collection_name);
        """)

    def get_hash(self, file_path: str, collection: str = RAG_COLLECTION_NAME) -> Optional[str]:
        row = storage.query_one(
            self.db_name,
            "SELECT file_hash FROM indexed_files WHERE file_path = ? AND collection_name = ?",
            (file_path, collection)
        )
        return row[0] if row else None

    def update_hash(self, file_path: str, file_hash: str, collection: str = RAG_COLLECTION_NAME):
        storage.execute(
            self.db_name,
            """INSERT OR REPLACE INTO indexed_files (file_path, file_hash, last_indexed, collection_name)
               VALUES (?, ?, ?, ?)""",
            (file_path, file_hash, time.time(), collection)
        )

    def remove_file(self, file_path: str, collection: str = RAG_COLLECTION_NAME):
        storage.execute(
            self.db_name,
            "DELETE FROM indexed_files WHERE file_path = ? AND collection_name = ?",
            (file_path, collection)
        )

    def list_indexed(self, collection: str = RAG_COLLECTION_NAME) -> List[str]:
        rows = storage.query_all(
            self.db_name,
            "SELECT file_path FROM indexed_files WHERE collection_name = ?", (collection,)
        )
        return [r[0] for r in rows]


tracker = IndexTracker()


# ─── Извлечение текста из .conversation.json (как в mcp_export_lmstudio) ──
def extract_text_from_chat(file_path: Path) -> Optional[str]:
    """Извлекает весь читаемый текст из файла диалога LM Studio."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        _log(f"[DialogIndexer] Failed to read {file_path}: {e}")
        return None

    messages = []
    for msg in data.get('messages', []):
        versions = msg.get('versions', [])
        selected = None
        for v in versions:
            if v.get('currentlySelected'):
                selected = v
                break
        if not selected and versions:
            selected = versions[0]
        if not selected:
            continue

        role = msg.get('role', 'unknown')
        text = ""
        content_blocks = selected.get('content', [])
        if content_blocks:
            text = content_blocks[0].get('text', '')
        elif selected.get('type') == 'multiStep':
            steps = selected.get('steps', [])
            if steps:
                last_step = steps[-1]
                content_blocks = last_step.get('content', [])
                if content_blocks:
                    text = content_blocks[0].get('text', '')
        if text:
            messages.append(f"[{role}]: {text}")

    if not messages:
        return None
    return "\n\n".join(messages)


def compute_file_hash(file_path: Path) -> str:
    """Вычисляет MD5 содержимого файла."""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


# ─── Индексация одного файла (через rag_index_folder с пересозданием) ─────
# rag_index_folder не умеет индексировать один файл, только папку, поэтому
# поддерживаем временную папку с текстовыми представлениями диалогов и
# храним хеши для инкрементальности: переиндексируются только изменившиеся.

def index_dialogs_folder(force: bool = False) -> Dict:
    """
    Сканирует папку LM_STUDIO_CHATS_PATH, для новых или изменённых файлов
    извлекает текст и вызывает rag_index_folder. Использует хеши.
    """
    if not LM_STUDIO_CHATS_PATH.exists():
        return {"status": "error", "message": f"Folder not found: {LM_STUDIO_CHATS_PATH}"}

    # Создаём временную папку для текстовых представлений диалогов
    temp_text_dir = Path(__file__).parent / ".dialog_texts"
    temp_text_dir.mkdir(exist_ok=True)

    indexed_files = set(tracker.list_indexed(RAG_COLLECTION_NAME))
    new_or_changed = 0
    deleted = 0

    # 1. Обрабатываем текущие файлы
    current_files = set()
    for json_file in LM_STUDIO_CHATS_PATH.rglob("*.conversation.json"):
        current_files.add(str(json_file))
        current_hash = compute_file_hash(json_file)
        stored_hash = tracker.get_hash(str(json_file), RAG_COLLECTION_NAME)

        if force or stored_hash != current_hash:
            # Извлекаем текст и сохраняем во временный .txt файл
            text = extract_text_from_chat(json_file)
            if text:
                # Сохраняем текст в файл с тем же именем, но .txt
                txt_file = temp_text_dir / (json_file.stem + ".txt")
                txt_file.write_text(text, encoding='utf-8')
                new_or_changed += 1
            tracker.update_hash(str(json_file), current_hash, RAG_COLLECTION_NAME)

    # 2. Удаляем из индекса файлы, которых больше нет
    for old_file in indexed_files:
        if old_file not in current_files:
            # Удаляем соответствующий .txt файл
            old_txt = temp_text_dir / (Path(old_file).stem + ".txt")
            if old_txt.exists():
                old_txt.unlink()
            tracker.remove_file(old_file, RAG_COLLECTION_NAME)
            deleted += 1

    # 3. Запускаем RAG индексацию всей временной папки (инкрементально)
    if new_or_changed > 0 or deleted > 0 or force:
        _log(f"[DialogIndexer] Indexing {new_or_changed} new/changed, {deleted} deleted dialogs")
        result = rag_index_folder(
            folder_path=str(temp_text_dir),
            collection_name=RAG_COLLECTION_NAME,
            force_reindex=False,
            incremental=True,
            cleanup_deleted=True
        )
        return {
            "status": "success",
            "new_or_changed": new_or_changed,
            "deleted": deleted,
            "rag_result": result
        }
    else:
        return {"status": "skipped", "message": "No changes detected"}


# ─── Watcher для автоматического отслеживания изменений ──────────────────
_watcher_thread = None
_watcher_stop = threading.Event()
_watcher_lock = threading.Lock()


def _watcher_loop():
    """Фоновый поток, периодически проверяющий изменения в папке диалогов."""
    # wait() вместо sleep(): мгновенная реакция на остановку (v1.1)
    while not _watcher_stop.wait(CHECK_INTERVAL_SEC):
        try:
            result = index_dialogs_folder(force=False)
            if result.get("new_or_changed", 0) > 0 or result.get("deleted", 0) > 0:
                _log(f"[DialogIndexer] Auto-indexed: {result['new_or_changed']} new/changed, {result['deleted']} deleted")
        except Exception as e:
            _log(f"[DialogIndexer] Watcher error: {e}")
    storage.close_thread()


def start_dialog_watcher():
    global _watcher_thread
    with _watcher_lock:
        if _watcher_thread and _watcher_thread.is_alive():
            return
        _watcher_stop.clear()
        _watcher_thread = threading.Thread(target=_watcher_loop, daemon=True, name="dialog_indexer_watcher")
        _watcher_thread.start()
    _log("[DialogIndexer] Watcher started")


def stop_dialog_watcher():
    _watcher_stop.set()
    with _watcher_lock:
        if _watcher_thread:
            _watcher_thread.join(timeout=2)
    _log("[DialogIndexer] Watcher stopped")


# ─── MCP инструменты ──────────────────────────────────────────────────────
def index_dialogs_now(force: bool = False) -> Dict:
    """Принудительная индексация всех диалогов (или только изменённых)."""
    return index_dialogs_folder(force=force)


def search_dialogs(query: str, limit: int = 10) -> Dict:
    """Поиск по проиндексированным диалогам LM Studio."""
    return rag_search(query, collection_name=RAG_COLLECTION_NAME, top_k=limit)


def dialog_indexer_status() -> Dict:
    """Статус индексатора: количество проиндексированных файлов, наличие watcher."""
    indexed = tracker.list_indexed(RAG_COLLECTION_NAME)
    return {
        # bool(): раньше поле было None до первого запуска watcher (v1.1)
        "watcher_running": bool(_watcher_thread and _watcher_thread.is_alive()),
        "indexed_files_count": len(indexed),
        "collection_name": RAG_COLLECTION_NAME,
        "chats_folder": str(LM_STUDIO_CHATS_PATH)
    }


def register_tools(server: BaseMCPServer):
    server.register_tool("index_dialogs_now", {
        "description": "Принудительно проиндексировать все диалоги LM Studio в RAG (инкрементально)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {"type": "boolean", "default": False, "description": "Переиндексировать всё, даже без изменений"}
            }
        }
    }, lambda **kw: index_dialogs_now(kw.get("force", False)))

    server.register_tool("search_dialogs", {
        "description": "Поиск по проиндексированным диалогам LM Studio (семантический)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        }
    }, lambda **kw: search_dialogs(kw["query"], kw.get("limit", 10)))

    server.register_tool("dialog_indexer_status", {
        "description": "Показать статус автоматического индексатора диалогов",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: dialog_indexer_status())

    server.register_tool("start_dialog_watcher", {
        "description": "Запустить фоновый мониторинг папки диалогов LM Studio (авто-индексация)",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: start_dialog_watcher() or {"status": "started"})

    server.register_tool("stop_dialog_watcher", {
        "description": "Остановить фоновый мониторинг",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: stop_dialog_watcher() or {"status": "stopped"})


# ─── Автоматический запуск watcher при загрузке модуля ───────────────────
# Если переменная окружения MCP_AUTO_INDEX_DIALOGS=1, запускаем watcher сразу
if os.environ.get("MCP_AUTO_INDEX_DIALOGS", "0") == "1":
    start_dialog_watcher()

__mcp_plugin__ = {
    "name": "dialog-indexer",
    "version": "1.1",
    "description": "Автоматическая индексация диалогов LM Studio в RAG с инкрементальным обновлением",
    "dependencies": [],
    "on_load": lambda: _log("[DialogIndexer] Loaded (v1.1, mcp_storage). Use index_dialogs_now, search_dialogs, start_dialog_watcher"),
    "on_unload": lambda: stop_dialog_watcher()
}

if __name__ == "__main__":
    # Для тестирования
    _log("Testing dialog indexer...")
    result = index_dialogs_now()
    _log(json.dumps(result, ensure_ascii=False, default=str))
