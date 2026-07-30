# plugins/loader.py
"""
Асинхронный загрузчик плагинов для MCP Cognitive Server.

Обнаруживает подпакеты с plugin.py, импортирует классы-наследники MCPPlugin,
сортирует их топологически по зависимостям (depends_on) и регистрирует.

ИСПРАВЛЕНО: граф зависимостей строится по отображаемому имени плагина
(plugin.name), а не по имени папки. Раньше depends_on возвращал имена вида
"World Model", а граф был по папкам ("world_model"), из-за чего топосортировка
ничего не делала и зависимости игнорировались.
"""

import importlib
import pkgutil
from collections import deque
from pathlib import Path
from typing import Dict, List, Tuple

from .base_plugin import MCPPlugin

IGNORE = {"base_plugin", "loader", "__pycache__"}


def _discover_plugin_classes(plugins_path: Path) -> List[Tuple[str, type]]:
    """Находит (имя_папки, класс_плагина) во всех подпапках plugins/."""
    found: List[Tuple[str, type]] = []
    for module_info in pkgutil.iter_modules([str(plugins_path)]):
        folder = module_info.name
        if folder in IGNORE or not module_info.ispkg:
            continue
        try:
            mod = importlib.import_module(f"plugins.{folder}.plugin")
        except ImportError as e:
            print(f"[Loader] Пропуск {folder}: не найден plugin.py ({e})")
            continue
        except Exception as e:
            print(f"[Loader] Ошибка импорта {folder}: {e}")
            continue
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if isinstance(attr, type) and issubclass(attr, MCPPlugin) and attr is not MCPPlugin:
                found.append((folder, attr))
                break
    return found


def _topo_sort(instances: Dict[str, MCPPlugin]) -> List[str]:
    """Сортировка Кана по отображаемым именам (одно пространство имён с depends_on)."""
    graph = {name: list(inst.depends_on) for name, inst in instances.items()}

    # Предупреждаем об отсутствующих зависимостях и отбрасываем их
    for name, deps in graph.items():
        missing = [d for d in deps if d not in instances]
        for d in missing:
            print(f"[Loader] Предупреждение: '{name}' зависит от отсутствующего плагина '{d}' — игнорируется")
        graph[name] = [d for d in deps if d in instances]

    in_degree = {name: len(deps) for name, deps in graph.items()}
    queue = deque(sorted(n for n, d in in_degree.items() if d == 0))
    order: List[str] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for other, deps in graph.items():
            if n in deps:
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    queue.append(other)

    remaining = [n for n in graph if n not in order]
    if remaining:
        print(f"[Loader] Обнаружена циклическая зависимость, порядок не гарантирован: {remaining}")
        order.extend(remaining)
    return order


async def load_plugins(server) -> List[MCPPlugin]:
    plugins_path = Path(__file__).parent
    plugin_classes = _discover_plugin_classes(plugins_path)
    if not plugin_classes:
        print("[Loader] Плагины не найдены.")
        return []

    # Создаём экземпляры ОДИН раз и индексируем по отображаемому имени (name).
    instances: Dict[str, MCPPlugin] = {}
    for folder, cls in plugin_classes:
        try:
            inst = cls()
        except Exception as e:
            print(f"[Loader] Не удалось создать экземпляр {folder}: {e}")
            continue
        if inst.name in instances:
            print(f"[Loader] Предупреждение: дубль имени плагина '{inst.name}' (папка {folder}) — пропуск")
            continue
        instances[inst.name] = inst

    loaded: List[MCPPlugin] = []
    for name in _topo_sort(instances):
        inst = instances[name]
        try:
            print(f"[Plugin] Загрузка: {inst.name}")
            await inst.register(server)
            loaded.append(inst)
        except Exception as e:
            print(f"[Plugin] Ошибка регистрации '{name}': {e}")

    print(f"[Plugin] Загружено плагинов: {len(loaded)}")
    return loaded
