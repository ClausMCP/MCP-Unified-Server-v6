#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Unified Filesystem Server v4.0 (Plugin Registry + Task Manager + Help + Export + Shell + Office Editor + Memory Graph)
Поддержка: асинхронные задачи, !command, справка, экспорт диалога, безопасный шелл,
экспорт чатов LM Studio, полноценное редактирование Excel/Word/PPT, граф памяти, авто-обнаружение модулей.

Изменения относительно v3.4 (набор инструментов и поведение сохранены):
  • Внутренний загрузчик (~150 строк: discover_modules_in_root, _check_dependencies,
    _copy_tools, _load_module, _register_module, _discover_plugins, _loaded_modules)
    заменён на mcp_plugin_registry — единый реестр с кэшем find_spec, полным
    учётом статусов и корректным unload в обратном порядке загрузки.
    Диагностика доступна через новые инструменты plugin_registry_status / plugin_reload.
  • ИСПРАВЛЕН БАГ: инструмент rate_limiter_reset вызывал rate_limiter.reset(),
    которого в v1.3.2 не существовало, — падал с AttributeError при каждом
    использовании. Метод добавлен в mcp_rate_limiter v2.0.
  • rate_limiter_stats переведён с прямого sqlite3.connect на mcp_storage
    (последнее прямое подключение в этом файле).
  • MODULE_DEPS сохранён как информационная сводка об опциональных
    зависимостях (лог перед загрузкой), проверка — через кэш реестра.
"""
import sys
import os
import atexit
import threading
import time
import json
from pathlib import Path
from datetime import datetime

import mcp_storage as storage
from mcp_shared import BaseMCPServer, _log, dialog_ctx
from mcp_verbose import set_verbose as set_dialog_verbose, is_verbose as get_dialog_verbose
import mcp_plugin_registry as plugin_registry
from mcp_plugin_registry import registry, check_dependencies

# --- Явно регистрируемые модули (не через авто-обнаружение) ---
from mcp_task_manager import register_tasks
from mcp_help import register_help_tool
from mcp_export_dialog import register_export_tool
from mcp_shell import register_shell_tool
from mcp_export_lmstudio import register_export_lmstudio_tool

# Активация координатора (опционально, но улучшает интеграцию)
try:
    import cognitive_coordinator
    _log("Cognitive Coordinator loaded")
except ImportError:
    _log("Cognitive Coordinator not available")

# --- Конфигурация обнаружения ---
PLUGIN_DIR = Path(__file__).parent / "mcp_plugins"

# Модули, регистрируемые в main() явно, исключаются из авто-обнаружения,
# чтобы не регистрироваться дважды (mcp_task_manager экспонирует
# register_tasks, который реестр умеет вызывать сам).
registry.excluded |= {"mcp_task_manager"}

# Модули вне маски mcp_*.py, которые должны загружаться как плагины.
# ИСПРАВЛЕНИЕ v4.0: в v3.4 discover_modules_in_root() искал только mcp_*.py,
# поэтому эти модули (перечисленные автором в MODULE_DEPS и в LEGACY_MODULES
# extension_manager) НИКОГДА не загружались — их инструменты (dialog_switch,
# dialog_search и др.) отсутствовали на unified-сервере.
EXTRA_MODULES = [
    "dialog_manager", "knowledge_base_server", "code_debugger_server",
    "logic_verifier_server", "context_manager_server",
]

# Информационная сводка об опциональных зависимостях (как в v3.4):
# отсутствие пакета из этого списка НЕ блокирует загрузку модуля,
# но фиксируется в логе для диагностики.
MODULE_DEPS = {
    "mcp_fs_search": ["watchdog"],
    "mcp_fs_watcher": ["watchdog"],
    "mcp_fs_media": ["PIL", "mutagen"],
    "mcp_fs_indexer": [],
    "mcp_office_reader": ["docx", "openpyxl", "pptx"],
    "mcp_office_editor": ["openpyxl", "python-docx", "python-pptx"],
    "mcp_web_reader": ["requests", "bs4", "feedparser"],
    "mcp_db_client": ["duckdb", "pyodbc"],
    "mcp_calendar": ["icalendar"],
    "mcp_email_client": ["keyring"],
    "code_debugger_server": [],
    "knowledge_base_server": [],
    "mcp_mempalace": [],
    "mcp_smart_search": [],
    "dialog_manager": [],
    "mcp_rag_engine": ["chromadb", "sentence_transformers", "pypdf", "docx", "ebooklib", "bs4"],
    "mcp_memory_engine": [],
    "mcp_memory_tools": [],
    "mcp_memory_graph": [],
    "mcp_dialog_indexer": [],
}


def _log_optional_deps():
    """Информирует об отсутствующих опциональных зависимостях (не блокирует)."""
    for mod_name, deps in MODULE_DEPS.items():
        if not deps:
            continue
        missing = check_dependencies(deps)
        if missing:
            _log(f"[INFO] {mod_name}: optional deps missing → {', '.join(missing)}")


def _graceful_shutdown():
    _log("Shutting down MCP Server. Running unload hooks...")
    try:
        from mcp_shared import conversation_memory
        if hasattr(conversation_memory, '_stop_auto_threads'):
            conversation_memory._stop_auto_threads()
            _log("ConversationMemory auto-threads stop signal sent.")
    except Exception as e:
        _log(f"[WARN] Failed to stop ConversationMemory threads: {e}")
    # Реестр выгружает плагины в обратном порядке загрузки (on_unload)
    registry.unload_all()
    _log("All cleanup completed. Exiting.")


def print_tools_summary(unified: BaseMCPServer):
    _log("=" * 60)
    _log(f"TOOLS REGISTRY: {len(unified._handlers)} tools available")
    _log("=" * 60)
    from collections import defaultdict
    by_module = defaultdict(list)
    for name in sorted(unified._handlers.keys()):
        handler = unified._handlers[name]
        module = getattr(handler, '__module__', 'unknown')
        by_module[module].append(name)
    for module in sorted(by_module.keys()):
        tools = by_module[module]
        _log(f"  [{module}] ({len(tools)} tools): {', '.join(tools)}")


os.environ.setdefault("MCP_SEARCH_TIMEOUT", "3600")
os.environ.setdefault("MCP_ANALYSIS_TIMEOUT", "3600")
os.environ.setdefault("MCP_GRAPH_AUTO_EXTRACT", "true")


def main():
    os.environ.setdefault("MCP_AUTO_INDEX_DIALOGS", "1")
    _log("Initializing MCP Unified Filesystem Server v4.0 (Plugin Registry)...")
    unified = BaseMCPServer("filesystem-unified", "4.0")

    # Register verbose control tools
    unified.register_tool("set_verbose", {
        "description": "Включить/выключить подробные уведомления о прогрессе (прогресс-бар в чате)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "enable": {"type": "boolean", "default": True},
                "dialog_id": {"type": "string"}
            }
        }
    }, lambda **kw: set_dialog_verbose(kw.get("dialog_id") or dialog_ctx.get(), kw.get("enable", True)) or {"status": "ok"})

    unified.register_tool("get_verbose", {
        "description": "Проверить, включены ли подробные уведомления для диалога",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dialog_id": {"type": "string"}
            }
        }
    }, lambda **kw: {"verbose": get_dialog_verbose(kw.get("dialog_id") or dialog_ctx.get())})

    register_help_tool(unified)
    register_export_tool(unified)
    register_shell_tool(unified)
    register_tasks(unified)
    register_export_lmstudio_tool(unified)
    plugin_registry.register_tools(unified)  # plugin_registry_status / plugin_reload

    from mcp_rate_limiter import rate_limiter, DB_NAME as RATE_DB_NAME

    def rate_limiter_stats():
        # v4.0: через mcp_storage (был последний прямой sqlite3.connect в файле)
        rows = storage.query_all(
            RATE_DB_NAME, "SELECT service, failures, last_failure FROM rate_history")
        return {"services": [{"service": r[0], "failures": r[1], "last_failure": r[2]} for r in rows]}

    unified.register_tool("rate_limiter_stats", {
        "description": "Статистика rate limiter и circuit breaker",
        "inputSchema": {"type": "object", "properties": {}}
    }, rate_limiter_stats)

    unified.register_tool("rate_limiter_reset", {
        "description": "Сбросить rate limiter для сервиса (или всех, если service не указан)",
        "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}}}
    }, lambda **kw: rate_limiter.reset(kw.get("service")))

    # ── Загрузка модулей и плагинов через единый реестр ──────────────────
    _log_optional_deps()
    modules = registry.discover_root_modules(extra=EXTRA_MODULES)
    summary = registry.load_all(unified, modules=modules, include_plugin_dirs=True)

    total_tools = len(unified._handlers)
    _log(f"Server ready: {summary['loaded']} modules/plugins loaded. {total_tools} tools available.")
    if summary["skipped"] or summary["failed"]:
        _log(f"Skipped: {summary['skipped']} ({', '.join(summary['skipped_names'])})")
        if summary["failed"]:
            _log(f"Failed: {summary['failed']} ({', '.join(summary['failed_names'])})")

    print_tools_summary(unified)
    atexit.register(_graceful_shutdown)

    # ========== Автоматическая индексация memPalace ==========
    def auto_index_mempalace():
        """Фоновый запуск индексации диалогов и кода (один раз при старте)."""
        time.sleep(3)  # даём серверу полностью стартовать
        _log("[MemPalace] Starting background auto-indexing...")
        try:
            from mcp_mempalace import mempalace_mine
        except ImportError:
            _log("[MemPalace] mempalace module not available, skipping auto-index")
            return

        server_dir = Path(__file__).parent.resolve()

        # --- 1. Индексация диалогов LM Studio (один раз) ---
        lmstudio_chats = Path.home() / ".lmstudio" / "conversations"
        if lmstudio_chats.exists() and lmstudio_chats.is_dir():
            index_flag = server_dir / ".mempalace_chats_indexed"
            if not index_flag.exists():
                _log(f"[MemPalace] Indexing LM Studio chats from {lmstudio_chats}")
                res = mempalace_mine(str(lmstudio_chats), mode="convos")
                if res.get("status") == "success":
                    # Создаём флаг, чтобы больше не индексировать при следующих запусках
                    index_flag.touch()
                    _log("[MemPalace] LM Studio chats indexed (flag created)")
                else:
                    _log(f"[MemPalace] Failed to index chats: {res.get('error')}")
            else:
                _log("[MemPalace] LM Studio chats already indexed, skipping")
        else:
            _log(f"[MemPalace] LM Studio chats folder not found: {lmstudio_chats}")

        # --- 2. Индексация кода (проект mempalace_project) с проверкой по дате ---
        code_project = server_dir / "mempalace_project"
        if not code_project.exists():
            code_project.mkdir(exist_ok=True)
            _log(f"[MemPalace] Created project folder: {code_project}")

        # Проверяем, когда в последний раз индексировался код
        code_index_flag = code_project / ".mempalace_code_indexed"
        need_index = False
        if not code_index_flag.exists():
            need_index = True
        else:
            try:
                with open(code_index_flag, 'r') as f:
                    data = json.load(f)
                last_index = datetime.fromisoformat(data.get("last_index", "2000-01-01"))
                if (datetime.now() - last_index).days >= 1:  # раз в сутки
                    need_index = True
                else:
                    _log(f"[MemPalace] Code already indexed today ({last_index.date()})")
            except Exception:
                need_index = True

        if need_index:
            _log(f"[MemPalace] Indexing code project: {code_project}")
            res = mempalace_mine(str(code_project), mode="files")
            if res.get("status") == "success":
                with open(code_index_flag, 'w') as f:
                    json.dump({"last_index": datetime.now().isoformat()}, f)
                _log("[MemPalace] Code project indexed")
            else:
                _log(f"[MemPalace] Failed to index code: {res.get('error')}")
        else:
            _log("[MemPalace] Code project indexing skipped (fresh)")

    # Запускаем фоновый поток
    threading.Thread(target=auto_index_mempalace, daemon=True, name="mempalace_auto_index").start()
    _log("Listening on STDIO (JSON-RPC 2.0)...")
    unified.run()


if __name__ == "__main__":
    main()
