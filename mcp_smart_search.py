#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Smart Search v1.3 – устойчивый поиск с исправлением опечаток в источниках
"""
import json
import os
import time
import threading
from collections import OrderedDict
from typing import List, Dict, Optional, Tuple
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx, is_online
)

# ─── Кэш результатов поиска (короткий TTL) ───────────────────────────────
# smart_search — самый частый путь поиска; повторные одинаковые запросы
# (типичны при уточнениях в диалоге) переопрашивали web/kb/memory/mempalace
# заново. Кэшируем ТОЛЬКО успешную выборку (без ошибок) на короткое время.
_SEARCH_CACHE_TTL = 20.0       # секунд
_SEARCH_CACHE_MAX = 128        # макс. число записей (LRU-вытеснение)
_search_cache: "OrderedDict[tuple, Tuple[float, Dict]]" = OrderedDict()
_search_cache_lock = threading.Lock()

def _cache_get(key):
    now = time.monotonic()
    with _search_cache_lock:
        item = _search_cache.get(key)
        if not item:
            return None
        ts, value = item
        if now - ts > _SEARCH_CACHE_TTL:
            _search_cache.pop(key, None)
            return None
        _search_cache.move_to_end(key)   # отметка свежести (LRU)
        return value

def _cache_put(key, value):
    with _search_cache_lock:
        _search_cache[key] = (time.monotonic(), value)
        _search_cache.move_to_end(key)
        while len(_search_cache) > _SEARCH_CACHE_MAX:
            _search_cache.popitem(last=False)

def invalidate_search_cache():
    """Сбрасывает кэш поиска (например, после массового добавления данных)."""
    with _search_cache_lock:
        _search_cache.clear()

# В самом верху файла (после импортов)
try:
    from mcp_background_indexer import _indexer  # запускает фоновый поток
except ImportError:
    pass

__mcp_plugin__ = {
    "name": "smart-search",
    "version": "1.3.0",
    "description": "Unified keyword search over web, KB, memory, and memPalace with typo correction and offline adaptation",
    "dependencies": [],
    "on_load": lambda: _log("[smart-search] v1.3 loaded (typo correction enabled)"),
    "on_unload": lambda: _log("[smart-search] Unloaded.")
}

def _normalize_sources(sources: List[str]) -> List[str]:
    """
    Исправляет типичные опечатки в названиях источников.
    Также удаляет дубликаты.
    """
    mapping = {
        "mempalpace": "mempalace",
        "mempalase": "mempalace",
        "mempalac": "mempalace",
        "mempala": "mempalace",
        "knoledge": "kb",
        "knowlege": "kb",
        "konwledge": "kb",
        "memry": "memory",
        "memmory": "memory",
        "wek": "web",
        "webb": "web",
        "wweb": "web",
    }
    corrected = []
    for s in sources:
        if not isinstance(s, str):
            continue
        s_lower = s.lower()
        corrected.append(mapping.get(s_lower, s_lower))
    # Удаляем дубликаты, сохраняя порядок
    seen = set()
    unique = []
    for item in corrected:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique

def _do_search(query: str, sources: List[str], limit: int,
               mempalace_project: Optional[str], d_id: Optional[str]) -> Tuple[Dict, List[str]]:
    """Фактическая выборка по источникам. Возвращает (results, errors)."""
    results: Dict = {}
    errors: List[str] = []

    # Web search — поддержка движков searxng/brave/duckduckgo
    web_engines = [s for s in sources if s in ("duckduckgo", "searxng", "searx", "brave")]
    if "web" in sources and not web_engines:
        web_engines = ["duckduckgo"]  # обратная совместимость: "web" => DuckDuckGo
    if web_engines:
        try:
            from mcp_web_reader import web_search_enhanced
            web_limit = max(limit, int(os.environ.get("MCP_WEB_RESULTS", "15")))
            web_res = web_search_enhanced(query, max_results=web_limit, sources=web_engines)
            if web_res.get("status") == "success":
                results["web"] = {
                    "status": "success",
                    "engines": web_engines,
                    "count": web_res.get("count", 0),
                    "results": web_res.get("results", [])
                }
            else:
                errors.append(f"Web search: {web_res.get('error', 'Unknown error')}")
        except ImportError:
            errors.append("Web search not available: mcp_web_reader module missing")
        except Exception as e:
            errors.append(f"Web search error: {e}")

    # Knowledge base
    if "kb" in sources:
        try:
            from knowledge_base_server import search_notes
            kb_res = search_notes(query, limit=limit)
            if kb_res.get("status") == "success":
                results["knowledge_base"] = {
                    "status": "success",
                    "count": len(kb_res.get("results", [])),
                    "results": kb_res.get("results", [])
                }
            else:
                errors.append(f"KB search: {kb_res.get('message', 'Unknown error')}")
        except ImportError:
            errors.append("Knowledge base not available: knowledge_base_server missing")
        except Exception as e:
            errors.append(f"KB search error: {e}")

    # Memory (conversation)
    if "memory" in sources:
        try:
            from context_manager_server import recall_fact
            mem_res = recall_fact(query, store_if_missing=False, dialog_id=d_id)
            if mem_res.get("found"):
                results["memory"] = {
                    "status": "success",
                    "confidence": mem_res.get("confidence"),
                    "fact": mem_res.get("fact"),
                    "source": mem_res.get("source"),
                    "related_count": mem_res.get("related_count", 0)
                }
            else:
                results["memory"] = {"status": "not_found", "message": "No matching fact in memory"}
        except ImportError:
            errors.append("Memory recall not available: context_manager_server missing")
        except Exception as e:
            errors.append(f"Memory search error: {e}")

    # memPalace
    if "mempalace" in sources:
        try:
            from mcp_mempalace import mempalace_search
            mp_res = mempalace_search(query, project_path=mempalace_project, limit=limit)
            if mp_res.get("status") == "success":
                results["mempalace"] = {
                    "status": "success",
                    "count": mp_res.get("count", 0),
                    "results": mp_res.get("results", [])
                }
            else:
                errors.append(f"memPalace search: {mp_res.get('error', 'Unknown error')}")
        except ImportError:
            errors.append("memPalace not available: mcp_mempalace module missing")
        except Exception as e:
            errors.append(f"memPalace search error: {e}")

    return results, errors


def smart_search(query: str, sources: Optional[List[str]] = None,
                 limit: int = 5, dialog_id: Optional[str] = None,
                 mempalace_project: Optional[str] = None,
                 prefer_offline: bool = True) -> Dict:
    """
    Search across multiple sources with automatic offline adaptation.
    Sources: "web", "kb", "memory", "mempalace"
    """
    d_id = dialog_id or dialog_ctx.get()

    # 1. Нормализация источников (исправление опечаток)
    if sources is not None:
        sources = _normalize_sources(sources)
    else:
        # Автоматический выбор в зависимости от наличия интернета
        if is_online():
            sources = ["web", "kb", "memory", "mempalace"]
        else:
            sources = ["kb", "memory", "mempalace"]

    # 2. Приоритет офлайн-источников
    if prefer_offline:
        offline_sources = {"kb", "memory", "mempalace"}
        sources = sorted(sources, key=lambda s: (0 if s in offline_sources else 1, sources.index(s)))

    results = {}
    errors = []

    # Кэш: ключ по разрешённым источникам и параметрам. Кэшируем только
    # полностью успешную выборку (errors пуст), на короткий TTL.
    norm_query = " ".join(query.lower().split())
    cache_key = (norm_query, tuple(sources), int(limit),
                 mempalace_project, bool(prefer_offline))
    cached = _cache_get(cache_key)
    if cached is not None:
        results = {k: v for k, v in cached.items()}
        cache_hit = True
    else:
        cache_hit = False
        results, errors = _do_search(query, sources, limit, mempalace_project, d_id)
        if not errors:
            _cache_put(cache_key, {k: v for k, v in results.items()})

    # Логирование в память диалога
    conversation_memory.add(
        op="smart_search",
        paths={"query": query, "sources": sources, "prefer_offline": prefer_offline},
        status="success" if results else "partial",
        dialog=d_id,
        context=f"Smart search for '{query}' over {sources}. Found {len(results)} sources with data."
    )

    return {
        "status": "success" if results else "error",
        "query": query,
        "sources_used": sources,
        "results": results,
        "errors": errors if errors else None,
        "cached": cache_hit,
        "dialog_id": d_id,
        "online_status": is_online()
    }

def register_tools(server: BaseMCPServer):
    server.register_tool("smart_search", {
        "description": "Unified search across web, knowledge base, conversation memory, and memPalace with typo correction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},   # enum убран, валидация внутри функции
                    "default": ["web", "kb", "memory"],
                    "description": "Sources to search (auto-adapts if None). Valid values: web, kb, memory, mempalace. Misspellings will be corrected."
                },
                "limit": {"type": "integer", "default": 5},
                "dialog_id": {"type": "string"},
                "mempalace_project": {"type": "string", "description": "Project path for memPalace (if used)"},
                "prefer_offline": {"type": "boolean", "default": True, "description": "Prioritize offline sources"}
            },
            "required": ["query"]
        }
    }, lambda **kw: smart_search(
        kw["query"],
        kw.get("sources"),
        kw.get("limit", 5),
        kw.get("dialog_id"),
        kw.get("mempalace_project"),
        kw.get("prefer_offline", True)
    ))

if __name__ == "__main__":
    server = BaseMCPServer("smart-search", "1.3")
    register_tools(server)
    server.run()