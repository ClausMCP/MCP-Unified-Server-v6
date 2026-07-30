#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Health Check v1.0 — единый статус системы.

Инструмент `health_check` возвращает состояние ключевых подсистем: память,
граф знаний, планировщик, рефлексия, плагины, размеры БД, онлайн-статус и общее
число зарегистрированных инструментов. Только чтение, без побочных эффектов:
инспектирует уже загруженные модули (sys.modules), не импортируя тяжёлые/
side-effect модули заново.
"""
import os
import sys
import json
import sqlite3
from typing import Dict, Any

from mcp_shared import BaseMCPServer, _log, conversation_memory, is_online

# Ссылка на unified-сервер (для подсчёта инструментов) — ставится в register_tools.
_unified_server = None


def _db_size_mb(path: str):
    try:
        return round(os.path.getsize(path) / (1024 * 1024), 3) if path and os.path.exists(path) else None
    except Exception:
        return None


def _count(conn, table: str):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return None


def _probe_memory() -> Dict[str, Any]:
    info = {"status": "ok"}
    try:
        path = getattr(conversation_memory, "db_path", None)
        info["db_path"] = path
        info["db_size_mb"] = _db_size_mb(path)
        conn = conversation_memory._open_conn()
        try:
            info["entries"] = _count(conn, "entries")
            info["archived"] = _count(conn, "archived_entries")
        finally:
            conn.close()
    except Exception as e:
        info["status"] = "error"
        info["error"] = str(e)
    return info


def _probe_graph() -> Dict[str, Any]:
    mod = sys.modules.get("mcp_memory_graph")
    if not mod or not hasattr(mod, "_graph_db"):
        return {"status": "not_loaded"}
    info = {"status": "ok"}
    try:
        g = mod._graph_db
        info["db_path"] = g.db_path
        info["db_size_mb"] = _db_size_mb(g.db_path)
        with g._get_conn() as conn:
            info["entities"] = _count(conn, "entities")
            info["beliefs"] = _count(conn, "beliefs")
            info["facts"] = _count(conn, "facts")
            info["support_links"] = _count(conn, "statement_support")
    except Exception as e:
        info["status"] = "error"
        info["error"] = str(e)
    return info


def _probe_scheduler() -> Dict[str, Any]:
    mod = sys.modules.get("mcp_scheduler")
    if not mod:
        return {"status": "not_loaded"}
    info = {"status": "ok"}
    try:
        sched = getattr(mod, "_scheduler", None)
        db = getattr(sched, "db", None) if sched else None
        path = getattr(db, "db_path", None) or os.environ.get("MCP_SCHEDULER_DB")
        info["db_path"] = path
        info["db_size_mb"] = _db_size_mb(path)
        if path and os.path.exists(path):
            conn = sqlite3.connect(path, timeout=5)
            try:
                info["jobs"] = _count(conn, "jobs")
                try:
                    info["enabled_jobs"] = conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE enabled = 1").fetchone()[0]
                except Exception:
                    pass
            finally:
                conn.close()
    except Exception as e:
        info["status"] = "error"
        info["error"] = str(e)
    return info


def _probe_reflection() -> Dict[str, Any]:
    mod = sys.modules.get("reflection_server")
    if not mod or not hasattr(mod, "_reflection"):
        return {"status": "not_loaded"}
    try:
        last = getattr(mod._reflection, "_last_run", None)
        return {"status": "ok", "last_run": last}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _probe_plugins() -> Dict[str, Any]:
    mod = sys.modules.get("plugins.loader") or sys.modules.get("plugins")
    # Плагины грузятся в cognitive-сервере; здесь сообщаем только факт наличия пакета.
    try:
        import importlib.util
        present = importlib.util.find_spec("plugins") is not None
        return {"status": "available" if present else "absent"}
    except Exception:
        return {"status": "unknown"}


def health_check(verbose: bool = True) -> Dict[str, Any]:
    """Возвращает агрегированный статус системы (read-only)."""
    subsystems = {
        "memory": _probe_memory(),
        "graph": _probe_graph(),
        "scheduler": _probe_scheduler(),
        "reflection": _probe_reflection(),
        "plugins": _probe_plugins(),
    }
    # Общая оценка: error если хоть одна подсистема в error
    overall = "healthy"
    for name, s in subsystems.items():
        if s.get("status") == "error":
            overall = "degraded"
            break

    tools_count = None
    try:
        if _unified_server is not None and hasattr(_unified_server, "tools"):
            tools_count = len(_unified_server.tools)
    except Exception:
        pass

    result = {
        "status": overall,
        "online": bool(is_online()),
        "tools_registered": tools_count,
        "subsystems": subsystems if verbose else {k: v.get("status") for k, v in subsystems.items()},
    }
    return result


def register_tools(server: BaseMCPServer):
    """Регистрирует инструмент health_check в unified-сервере."""
    global _unified_server
    _unified_server = server
    server.register_tool("health_check", {
        "description": "System health: status of memory, knowledge graph, scheduler, reflection, "
                       "plugins; DB sizes, entry/job counts, online status, total tools. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "verbose": {"type": "boolean", "description": "Full details (default true) or only statuses"}
            }
        }
    }, lambda **kw: health_check(kw.get("verbose", True)))


__mcp_plugin__ = {
    "name": "healthcheck",
    "version": "1.0.0",
    "description": "System health check (read-only status of all subsystems)",
    "dependencies": [],
    "on_load": lambda: _log("[healthcheck] v1.0 loaded — tool: health_check"),
}

if __name__ == "__main__":
    print(json.dumps(health_check(), indent=2, ensure_ascii=False, default=str))
