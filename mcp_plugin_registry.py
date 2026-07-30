#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Plugin Registry v1.0 — единый реестр и загрузчик плагинов.
═══════════════════════════════════════════════════════════════

Заменяет ТРИ параллельные реализации загрузки, существовавшие в проекте:
  1) внутренний загрузчик mcp_fs_server (discover_modules_in_root,
     _load_module, _register_module, _discover_plugins);
  2) mcp_extension_manager.discover_and_load_plugins;
  3) точечные importlib.import_module в mcp_orchestrator и др.

Каждая из них по-своему проверяла зависимости (find_spec без кэша — при
30+ плагинах это сотни повторных обращений к файловой системе), по-своему
обрабатывала fallback на legacy-объект `server` и по-своему вела учёт.

Что даёт реестр:
  • Единый жизненный цикл плагина: discover → check deps → import →
    on_load → register_tools (или legacy server) → учёт инструментов;
    unload с вызовом on_unload.
  • Кэш find_spec: каждая зависимость проверяется один раз за процесс.
  • Полный учёт: какой плагин загружен/пропущен/упал, какие инструменты
    он зарегистрировал, текст ошибки — доступно через status().
  • Дедупликация инструментов: повторная регистрация имени пропускается
    с предупреждением (политика обоих старых загрузчиков сохранена).
  • Идемпотентность: повторный load_all не грузит уже загруженное.
  • reload(name) для горячей перезагрузки плагина при разработке.

Контракт плагина (обратная совместимость 100%):
  __mcp_plugin__ = {"name", "version", "description",
                    "dependencies": [...], "on_load", "on_unload"}  # всё опционально
  def register_tools(server): ...          # предпочтительный интерфейс
  # ЛИБО legacy: модульный объект `server` c .tools и ._handlers

Зависимости: только stdlib + mcp_shared (_log, BaseMCPServer).
"""
import os
import sys
import importlib
import importlib.util
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any, Iterable

from mcp_shared import _log, BaseMCPServer

# ─── Конфигурация по умолчанию ─────────────────────────────────────────────
_ROOT = Path(__file__).parent

DEFAULT_PLUGIN_DIRS = [
    "mcp_plugins",
    os.path.join(os.path.dirname(__file__), "mcp_plugins"),
]

# Модули, которые никогда не загружаются как плагины
# (инфраструктура, точки входа, утилиты запуска) — объединение списков
# исключений из mcp_fs_server и здравого смысла.
DEFAULT_EXCLUDED = {
    "mcp_fs_server", "mcp_orchestrator", "mcp_shared", "mcp_storage",
    "mcp_plugin_registry", "mcp_extension_manager",
    "mcp_setup", "fix_lmstudio_config", "mcp_verbose", "mcp_rate_limiter",
    "mcp_help", "mcp_export_dialog", "mcp_shell", "mcp_export_lmstudio",
}


# ─── Кэш проверки зависимостей ─────────────────────────────────────────────
_dep_cache: Dict[str, bool] = {}
_dep_cache_lock = threading.Lock()


def check_dependencies(deps: Iterable[str]) -> List[str]:
    """Возвращает список отсутствующих пакетов.

    В отличие от старых загрузчиков результат find_spec кэшируется:
    одна проверка на пакет за процесс вместо проверки на каждый плагин.
    """
    missing = []
    for dep in deps or ():
        pkg = dep.split("==")[0].split(">=")[0].split("<")[0].strip()
        if not pkg:
            continue
        with _dep_cache_lock:
            present = _dep_cache.get(pkg)
        if present is None:
            try:
                present = importlib.util.find_spec(pkg) is not None
            except (ImportError, ValueError, ModuleNotFoundError):
                present = False
            with _dep_cache_lock:
                _dep_cache[pkg] = present
        if not present:
            missing.append(dep)
    return missing


# ─── Запись о плагине ──────────────────────────────────────────────────────
class PluginRecord:
    __slots__ = ("name", "module_name", "version", "description",
                 "status", "tools", "error", "meta", "module", "source")

    def __init__(self, module_name: str, source: str = "root"):
        self.module_name = module_name
        self.name = module_name
        self.version = "1.0.0"
        self.description = ""
        self.status = "discovered"     # discovered|loaded|skipped|failed|unloaded
        self.tools: List[str] = []
        self.error: Optional[str] = None
        self.meta: Dict = {}
        self.module = None
        self.source = source           # root | plugin_dir | explicit

    def as_dict(self) -> Dict:
        return {
            "name": self.name, "module": self.module_name,
            "version": self.version, "description": self.description,
            "status": self.status, "tools": list(self.tools),
            "tool_count": len(self.tools), "source": self.source,
            "error": self.error,
        }


# ─── Реестр ────────────────────────────────────────────────────────────────
class PluginRegistry:
    def __init__(self, plugin_dirs: Optional[List[str]] = None,
                 excluded: Optional[Iterable[str]] = None):
        self.plugin_dirs = list(plugin_dirs) if plugin_dirs is not None else list(DEFAULT_PLUGIN_DIRS)
        self.excluded = set(excluded) if excluded is not None else set(DEFAULT_EXCLUDED)
        self._records: Dict[str, PluginRecord] = {}
        self._lock = threading.RLock()

    # ── Обнаружение ──────────────────────────────────────────────────────
    def discover_root_modules(self, pattern: str = "mcp_*.py",
                              extra: Iterable[str] = ()) -> List[str]:
        """Находит модули-кандидаты в корне проекта (замена discover_modules_in_root)."""
        found = []
        for py_file in sorted(_ROOT.glob(pattern)):
            stem = py_file.stem
            if stem in self.excluded or py_file.name.startswith("_"):
                continue
            found.append(stem)
        for name in extra:
            if name not in found and name not in self.excluded:
                found.append(name)
        return found

    # ── Регистрация инструментов модуля в объединённом сервере ──────────
    def _register_from_module(self, rec: PluginRecord, mod: object,
                              unified: BaseMCPServer) -> bool:
        meta = getattr(mod, "__mcp_plugin__", {}) or {}
        rec.meta = meta
        rec.module = mod
        rec.name = meta.get("name", rec.module_name)
        rec.version = str(meta.get("version", "1.0.0"))
        rec.description = meta.get("description", "")

        missing = check_dependencies(meta.get("dependencies", []))
        if missing:
            rec.status = "skipped"
            rec.error = f"missing dependencies: {', '.join(missing)}"
            _log(f"[Registry SKIP] {rec.name} v{rec.version}: {rec.error}")
            return False

        on_load = meta.get("on_load")
        if callable(on_load):
            try:
                on_load()
            except Exception as e:
                _log(f"[Registry WARN] {rec.name}: on_load failed → {e}")

        handlers_before = set(getattr(unified, "_handlers", {}) or {})

        reg_func = getattr(mod, "register_tools", None)
        if not callable(reg_func):
            reg_func = getattr(mod, "register_tasks", None)  # task_manager-стиль
        if callable(reg_func):
            reg_func(unified)
            rec.tools = sorted(set(getattr(unified, "_handlers", {}) or {}) - handlers_before)
            rec.status = "loaded"
            _log(f"[Registry OK] {rec.name} v{rec.version} ({len(rec.tools)} tools)")
            return True

        # Fallback: legacy-объект `server` внутри модуля
        srv = getattr(mod, "server", None)
        if srv is not None and hasattr(srv, "tools"):
            copied = 0
            for tool in srv.tools:
                t_name = tool.get("name") if isinstance(tool, dict) else None
                if not t_name:
                    continue
                if t_name in getattr(unified, "_handlers", {}):
                    _log(f"[Registry WARN] Tool '{t_name}' already registered. Skipping.")
                    continue
                handler = getattr(srv, "_handlers", {}).get(t_name)
                if handler:
                    unified.register_tool(t_name, tool, handler)
                    rec.tools.append(t_name)
                    copied += 1
            rec.status = "loaded"
            _log(f"[Registry OK] {rec.name} v{rec.version} legacy ({copied} tools)")
            return True

        rec.status = "failed"
        rec.error = "no register_tools/register_tasks or server object"
        _log(f"[Registry FAIL] {rec.name}: {rec.error}")
        return False

    # ── Загрузка ─────────────────────────────────────────────────────────
    def load_module(self, mod_name: str, unified: BaseMCPServer,
                    source: str = "explicit") -> PluginRecord:
        """Импортирует модуль по имени и регистрирует его инструменты (идемпотентно)."""
        with self._lock:
            rec = self._records.get(mod_name)
            if rec and rec.status == "loaded":
                return rec
            rec = PluginRecord(mod_name, source=source)
            self._records[mod_name] = rec
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            rec.status = "skipped"
            rec.error = f"import failed (missing dependency): {e}"
            _log(f"[Registry SKIP] {mod_name}: {rec.error}")
            return rec
        except Exception as e:
            rec.status = "failed"
            rec.error = f"import error: {type(e).__name__}: {e}"
            _log(f"[Registry FAIL] {mod_name}: {rec.error}")
            return rec
        try:
            self._register_from_module(rec, mod, unified)
        except Exception as e:
            rec.status = "failed"
            rec.error = f"registration error: {type(e).__name__}: {e}"
            _log(f"[Registry FAIL] {mod_name}: {rec.error}")
        return rec

    def load_from_path(self, module_path: Path, unified: BaseMCPServer) -> PluginRecord:
        """Загружает плагин из файла (каталог mcp_plugins/)."""
        mod_name = module_path.stem
        with self._lock:
            rec = self._records.get(mod_name)
            if rec and rec.status == "loaded":
                return rec
            rec = PluginRecord(mod_name, source="plugin_dir")
            self._records[mod_name] = rec
        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
            if not spec or not spec.loader:
                rec.status = "failed"
                rec.error = "spec_from_file_location returned None"
                return rec
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            self._register_from_module(rec, mod, unified)
        except Exception as e:
            rec.status = "failed"
            rec.error = f"{type(e).__name__}: {e}"
            _log(f"[Registry FAIL] plugin {mod_name}: {rec.error}")
        return rec

    def load_all(self, unified: BaseMCPServer,
                 modules: Optional[Iterable[str]] = None,
                 include_plugin_dirs: bool = True) -> Dict:
        """Загружает указанные модули (или авто-обнаруженные) + каталоги плагинов.

        Возвращает сводку в формате, совместимом со старым
        mcp_extension_manager.discover_and_load_plugins:
        {"loaded", "skipped", "failed", "total_tools"} + расширенные поля.
        """
        names = list(modules) if modules is not None else self.discover_root_modules()
        for mod_name in names:
            self.load_module(mod_name, unified, source="root")

        if include_plugin_dirs:
            for pdir in self.plugin_dirs:
                plugin_dir = Path(pdir)
                if not plugin_dir.is_dir():
                    continue
                for p_file in sorted(plugin_dir.glob("*.py")):
                    if p_file.name.startswith("_"):
                        continue
                    self.load_from_path(p_file, unified)

        return self.summary()

    # ── Выгрузка / перезагрузка ──────────────────────────────────────────
    def unload(self, name: str) -> bool:
        """Вызывает on_unload плагина и помечает его выгруженным.
        (Инструменты из unified-сервера не удаляются — BaseMCPServer
        не поддерживает дерегистрацию; выгрузка нужна для остановки
        фоновых потоков плагина через его on_unload.)"""
        with self._lock:
            rec = self._records.get(name) or next(
                (r for r in self._records.values() if r.name == name), None)
        if not rec or rec.status != "loaded":
            return False
        on_unload = (rec.meta or {}).get("on_unload")
        if callable(on_unload):
            try:
                on_unload()
            except Exception as e:
                _log(f"[Registry WARN] {rec.name}: on_unload failed → {e}")
        rec.status = "unloaded"
        _log(f"[Registry] {rec.name} unloaded")
        return True

    def reload(self, name: str, unified: BaseMCPServer) -> PluginRecord:
        """Горячая перезагрузка плагина (для разработки)."""
        self.unload(name)
        with self._lock:
            self._records.pop(name, None)
        if name in sys.modules:
            try:
                importlib.reload(sys.modules[name])
            except Exception:
                sys.modules.pop(name, None)
        return self.load_module(name, unified, source="explicit")

    def unload_all(self):
        """Вызывает on_unload у всех загруженных плагинов в ОБРАТНОМ порядке
        загрузки (семантика graceful shutdown старого mcp_fs_server:
        зависимые плагины выгружаются раньше своих зависимостей)."""
        with self._lock:
            names = [r.module_name for r in self._records.values() if r.status == "loaded"]
        for n in reversed(names):
            self.unload(n)

    # ── Отчётность ───────────────────────────────────────────────────────
    def summary(self) -> Dict:
        with self._lock:
            recs = list(self._records.values())
        loaded = [r for r in recs if r.status == "loaded"]
        skipped = [r for r in recs if r.status == "skipped"]
        failed = [r for r in recs if r.status == "failed"]
        return {
            "loaded": len(loaded),
            "skipped": len(skipped),
            "failed": len(failed),
            "total_tools": sum(len(r.tools) for r in loaded),
            "loaded_names": sorted(r.name for r in loaded),
            "skipped_names": sorted(r.name for r in skipped),
            "failed_names": sorted(r.name for r in failed),
        }

    def status(self) -> Dict:
        """Полная детализация по каждому плагину (для инструмента диагностики)."""
        with self._lock:
            return {
                "plugins": [r.as_dict() for r in
                            sorted(self._records.values(), key=lambda r: r.name)],
                "summary": self.summary(),
                "dep_cache_size": len(_dep_cache),
            }

    def get(self, name: str) -> Optional[PluginRecord]:
        with self._lock:
            return self._records.get(name) or next(
                (r for r in self._records.values() if r.name == name), None)


# ─── Глобальный реестр по умолчанию ────────────────────────────────────────
registry = PluginRegistry()


def discover_and_load_plugins(unified: BaseMCPServer) -> Dict:
    """Drop-in замена mcp_extension_manager.discover_and_load_plugins:
    та же сигнатура, тот же формат ответа (плюс расширенные поля)."""
    return registry.load_all(unified)


def register_tools(server: BaseMCPServer):
    """Инструмент диагностики реестра (опционально)."""
    server.register_tool("plugin_registry_status", {
        "description": "Статус реестра плагинов: что загружено/пропущено/упало, инструменты каждого плагина",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: registry.status())

    server.register_tool("plugin_reload", {
        "description": "Горячая перезагрузка плагина по имени модуля (для разработки)",
        "inputSchema": {
            "type": "object",
            "properties": {"module_name": {"type": "string"}},
            "required": ["module_name"]
        }
    }, lambda **kw: registry.reload(kw["module_name"], server).as_dict())


__mcp_plugin__ = {
    "name": "plugin-registry",
    "version": "1.0",
    "description": "Единый реестр плагинов: обнаружение, зависимости с кэшем, жизненный цикл, диагностика",
    "dependencies": [],
    "on_load": lambda: _log("[Registry] Plugin Registry v1.0 active"),
    "on_unload": lambda: registry.unload_all(),
}
