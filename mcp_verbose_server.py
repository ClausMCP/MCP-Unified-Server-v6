#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Verbose Server v2.0 — управление verbosity, rate limiting, batch aggregation.
"""
from mcp_shared import BaseMCPServer, _log, dialog_ctx
from mcp_verbose import (
    set_verbose, is_verbose, list_verbose_dialogs, list_disabled_dialogs,
    clear_verbose_all, verbose_stats, VERBOSE_DEFAULT,
    is_batch_mode, force_batch_mode,
    get_rate_limiter, get_batch_detector, get_batch_aggregator
)

server = BaseMCPServer("verbose-control", "2.0")

# ─── Handlers ─────────────────────────────────────────────────────────────
def tool_set_verbose(enable: bool = True, dialog_id: str = None) -> dict:
    d_id = dialog_id or dialog_ctx.get()
    set_verbose(d_id, enable)
    return {"status": "ok", "dialog": d_id, "verbose": enable}

def tool_get_verbose(dialog_id: str = None) -> dict:
    d_id = dialog_id or dialog_ctx.get()
    return {
        "dialog": d_id, "verbose": is_verbose(d_id),
        "global_default": VERBOSE_DEFAULT, "batch_mode_active": is_batch_mode()
    }

def tool_list_verbose() -> dict:
    return {
        "global_default": VERBOSE_DEFAULT, "batch_mode_active": is_batch_mode(),
        "explicitly_enabled": list_verbose_dialogs(),
        "explicitly_disabled": list_disabled_dialogs()
    }

def tool_clear_all_verbose() -> dict:
    clear_verbose_all()
    return {"status": "cleared", "global_default": VERBOSE_DEFAULT}

def tool_verbose_stats() -> dict:
    return verbose_stats()

def tool_get_rate_limit_stats() -> dict:
    return get_rate_limiter().stats()

def tool_reset_rate_limit_stats() -> dict:
    get_rate_limiter().reset()
    return {"status": "reset"}

def tool_get_batch_mode() -> dict:
    return get_batch_detector().stats()

def tool_force_batch_mode(seconds: float = None) -> dict:
    force_batch_mode(seconds)
    return {"status": "forced", "batch_mode": True,
            "duration_sec": seconds or get_batch_detector().cooldown_sec}

def tool_reset_batch_detector() -> dict:
    get_batch_detector().reset()
    return {"status": "reset"}

def tool_get_aggregator_stats() -> dict:
    return get_batch_aggregator().stats()

def tool_force_aggregate_emit() -> dict:
    """Принудительно отправить сводное уведомление прямо сейчас."""
    get_batch_aggregator().force_emit()
    return {"status": "emitted"}

def tool_reset_aggregator() -> dict:
    get_batch_aggregator().reset()
    return {"status": "reset"}

# ─── Tool Registration ────────────────────────────────────────────────────
server.register_tool("set_verbose", {
    "description": "Включить/выключить уведомления о прогрессе для диалога.",
    "inputSchema": {"type": "object", "properties": {
        "enable": {"type": "boolean", "default": True},
        "dialog_id": {"type": "string"}
    }}
}, lambda **kw: tool_set_verbose(kw.get("enable", True), kw.get("dialog_id")))

server.register_tool("get_verbose", {
    "description": "Статус verbose для диалога + состояние batch mode.",
    "inputSchema": {"type": "object", "properties": {"dialog_id": {"type": "string"}}}
}, lambda **kw: tool_get_verbose(kw.get("dialog_id")))

server.register_tool("list_verbose_dialogs", {
    "description": "Список диалогов с явными настройками verbose.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_list_verbose())

server.register_tool("clear_all_verbose", {
    "description": "Сбросить все явные настройки verbose.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_clear_all_verbose())

server.register_tool("verbose_stats", {
    "description": "Полная статистика: verbose, rate limiter, batch detector, batch aggregator.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_verbose_stats())

server.register_tool("get_rate_limit_stats", {
    "description": "Статистика rate limiter.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_get_rate_limit_stats())

server.register_tool("reset_rate_limit_stats", {
    "description": "Сброс счётчиков rate limiter.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_reset_rate_limit_stats())

server.register_tool("get_batch_mode", {
    "description": "Статус batch detector.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_get_batch_mode())

server.register_tool("force_batch_mode", {
    "description": "Принудительно включить batch mode на N секунд.",
    "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}
}, lambda **kw: tool_force_batch_mode(kw.get("seconds")))

server.register_tool("reset_batch_detector", {
    "description": "Сбросить batch detector.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_reset_batch_detector())

server.register_tool("get_aggregator_stats", {
    "description": (
        "Статистика batch aggregator: количество активных задач, "
        "сколько обновлений получено, сколько сводных отправлено, "
        "коэффициент сжатия (updates/emits)."
    ),
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_get_aggregator_stats())

server.register_tool("force_aggregate_emit", {
    "description": "Принудительно отправить сводное batch-уведомление прямо сейчас.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_force_aggregate_emit())

server.register_tool("reset_aggregator", {
    "description": "Сбросить состояние batch aggregator.",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_reset_aggregator())

if __name__ == "__main__":
    server.run()