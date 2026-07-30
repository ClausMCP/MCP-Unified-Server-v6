#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Extension Manager v2.0 — обёртка совместимости над Plugin Registry.

Вся логика перенесена в mcp_plugin_registry (единый реестр вместо трёх
параллельных загрузчиков). Этот модуль сохраняет прежний публичный API:
  • discover_and_load_plugins(unified) — та же сигнатура и формат ответа;
  • _check_dependencies(deps)          — теперь с кэшем find_spec;
  • _load_module_from_path(path)       — делегирует реестру;
  • PLUGIN_DIRS, LEGACY_MODULES        — прежние константы.

Существующий код, импортирующий mcp_extension_manager, работает без правок.
Для нового кода используйте mcp_plugin_registry напрямую.
"""
import os
from pathlib import Path
from typing import Dict, List, Optional

from mcp_shared import BaseMCPServer, _log
import mcp_plugin_registry as _reg

PLUGIN_DIRS = ["mcp_plugins", os.path.join(os.path.dirname(__file__), "mcp_plugins")]

# Прежний список «legacy»-модулей сохранён: discover_and_load_plugins
# по-прежнему грузит именно их (плюс каталоги плагинов), а не всё подряд —
# поведение v1.0 не меняется.
LEGACY_MODULES = [
    "mcp_fs_operations", "mcp_fs_search", "mcp_fs_batch", "mcp_fs_trash",
    "mcp_fs_sync", "mcp_fs_watcher", "mcp_fs_archives", "mcp_fs_cloud",
    "mcp_fs_organizer", "mcp_fs_versioning", "mcp_fs_indexer",
    "mcp_fs_discovery", "mcp_fs_scripts", "mcp_fs_media",
    "mcp_orchestrator", "mcp_admin_server", "mcp_event_bus",
    "knowledge_base_server", "logic_verifier_server", "context_manager_server",
    "code_debugger_server", "mcp_memory_engine"
]


def _check_dependencies(deps: List[str]) -> List[str]:
    """Return list of missing packages (теперь с кэшем find_spec)."""
    return _reg.check_dependencies(deps)


def _load_module_from_path(module_path: Path) -> Optional[object]:
    """Совместимость: импорт модуля из файла (без регистрации инструментов)."""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(module_path.stem, str(module_path))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_path.stem] = mod
        spec.loader.exec_module(mod)
        return mod
    return None


def discover_and_load_plugins(unified: BaseMCPServer) -> Dict:
    """Прежний интерфейс: грузит LEGACY_MODULES + каталоги плагинов через реестр.

    Возвращает {"loaded", "skipped", "failed", "total_tools"} — как v1.0,
    плюс расширенные поля реестра (loaded_names и т.д.)."""
    return _reg.registry.load_all(unified, modules=LEGACY_MODULES,
                                  include_plugin_dirs=True)
