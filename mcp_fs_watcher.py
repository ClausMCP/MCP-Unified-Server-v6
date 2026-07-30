#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Watcher v3.7 (Context-Isolated & Event-Bus Safe)
Real-time monitoring with filtering, debouncing, and direct Event Bus integration.
Falls back to HTTP only if explicitly configured, otherwise uses in-memory pub/sub.
Uses contextvars for secure dialog isolation and cleans up debounce timers on stop.
"""
import os
import sys
import json
import time
import threading
from pathlib import Path
from typing import List, Dict, Optional, Callable
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Watchdog Import with Graceful Fallback ──────────────────────────────────
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent, FileDeletedEvent, FileMovedEvent
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    class FileSystemEventHandler: pass
    class FileCreatedEvent: pass
    class FileModifiedEvent: pass
    class FileDeletedEvent: pass
    class FileMovedEvent: pass

# ─── Event Bus Safe Publishing ───────────────────────────────────────────────
def _publish_event_safe(topic: str, payload: dict):
    """Publish to Event Bus: tries direct monolithic call first, falls back to HTTP."""
    # 1. Direct in-memory publish (monolith mode)
    try:
        from mcp_event_bus import _event_bus
        _event_bus.publish(topic, payload)
        return
    except ImportError:
        pass
    except Exception as e:
        _log(f"Event bus direct publish failed: {e}")

    # 2. HTTP fallback (distributed mode)
    url = os.environ.get("EVENT_BUS_URL", "")
    if not url:
        return
    try:
        import requests
        requests.post(url, json={"topic": topic, "payload": payload}, timeout=1.0)
    except ImportError:
        _log("requests library not installed, event HTTP publishing disabled.")
    except Exception as e:
        _log(f"Event bus HTTP publish failed: {e}")

class MCPEventHandler(FileSystemEventHandler):
    def __init__(self, callback: Callable, include_ext: List[str] = None,
                 exclude_ext: List[str] = None, min_size_kb: int = 0,
                 debounce_sec: float = 1.0, publish_events: bool = True):
        super().__init__()
        self.callback = callback
        self.include_ext = [e.lower() for e in include_ext] if include_ext else None
        self.exclude_ext = [e.lower() for e in exclude_ext] if exclude_ext else []
        self.min_size_bytes = min_size_kb * 1024
        self.debounce_sec = debounce_sec
        self.publish_events = publish_events
        self._debounce_timers = {}
        self._lock = threading.Lock()
        self._stopped = False

    def _should_process(self, event) -> bool:
        if event.is_directory:
            return False
        path = event.src_path
        ext = Path(path).suffix.lower()
        if self.include_ext and ext not in self.include_ext:
            return False
        if ext in self.exclude_ext:
            return False
        try:
            if os.path.exists(path) and os.path.getsize(path) < self.min_size_bytes:
                return False
        except Exception:
            pass
        return True

    def _debounced_callback(self, event, path):
        with self._lock:
            if self._stopped:
                return
            if path in self._debounce_timers:
                del self._debounce_timers[path]
        self.callback(event)

    def _schedule(self, event):
        path = event.src_path
        with self._lock:
            if self._stopped:
                return
            if path in self._debounce_timers:
                self._debounce_timers[path].cancel()
            timer = threading.Timer(self.debounce_sec, self._debounced_callback, args=[event, path])
            timer.daemon = True
            timer.start()
            self._debounce_timers[path] = timer

    def on_created(self, event):
        if self._should_process(event):
            self._schedule(event)
            if self.publish_events:
                _publish_event_safe("file.created", {"path": event.src_path, "is_directory": event.is_directory})

    def on_modified(self, event):
        if self._should_process(event):
            self._schedule(event)
            if self.publish_events:
                _publish_event_safe("file.modified", {"path": event.src_path, "is_directory": event.is_directory})

    def on_deleted(self, event):
        if self._should_process(event):
            self.callback(event)  # без дебаунса
            if self.publish_events:
                _publish_event_safe("file.deleted", {"path": event.src_path, "is_directory": event.is_directory})

    def on_moved(self, event):
        if self._should_process(event):
            self.callback(event)
            if self.publish_events:
                _publish_event_safe("file.moved", {
                    "src_path": event.src_path,
                    "dest_path": event.dest_path,
                    "is_directory": event.is_directory
                })

    def stop(self):
        with self._lock:
            self._stopped = True
            for timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()
# ─── Watcher Core ────────────────────────────────────────────────────────────
_active_watchers = {}
_watcher_lock = threading.Lock()
_counter = 0

def watch_start(path: str, recursive: bool = True, events: List[str] = None,
                interval: float = 2.0, include_extensions: List[str] = None,
                exclude_extensions: List[str] = None, min_size_kb: int = 0,
                debounce_sec: float = 1.0, publish_events: bool = True) -> Dict:
    global _counter
    if not HAS_WATCHDOG:
        return {"status": "error", "message": "Watchdog library required. pip install watchdog"}

    p = Path(normalize_path(path))
    try:
        _ensure_allowed(p, "watch_start")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}

    if not p.exists() or not p.is_dir():
        return {"status": "error", "message": "Invalid directory"}

    with _watcher_lock:
        for wid, info in _active_watchers.items():
            if Path(info["path"]).resolve() == p.resolve():
                return {"status": "error", "message": f"Path already watched: {path} (watch_id={wid})"}

        _counter += 1
        wid = f"watch_{_counter}"

        handler = MCPEventHandler(
            callback=lambda e: _handle_event(wid, e),
            include_ext=include_extensions,
            exclude_ext=exclude_extensions,
            min_size_kb=min_size_kb,
            debounce_sec=debounce_sec,
            publish_events=publish_events
        )

        observer = Observer()
        observer.schedule(handler, str(p), recursive=recursive)
        observer.start()

        _active_watchers[wid] = {
            "observer": observer,
            "handler": handler,
            "path": str(p),
            "events": events or ["created", "modified", "deleted", "moved"],
            "buffer": [],
            "started": time.time(),
            "publish_events": publish_events
        }

    d_id = dialog_ctx.get()
    conversation_memory.add(
        op="watch_start",
        paths={"path": str(p)},
        status="started",
        dialog=d_id,
        context=f"Watching {p} (inc: {include_extensions}, exc: {exclude_extensions}, publish: {publish_events})"
    )

    return {
        "status": "success",
        "watch_id": wid,
        "path": str(p),
        "message": "Watcher started",
        "publish_events": publish_events
    }

def _handle_event(wid: str, event):
    with _watcher_lock:
        if wid not in _active_watchers:
            return
        info = _active_watchers[wid]
        event_type = "unknown"
        if isinstance(event, FileCreatedEvent):
            event_type = "created"
        elif isinstance(event, FileModifiedEvent):
            event_type = "modified"
        elif isinstance(event, FileDeletedEvent):
            event_type = "deleted"
        elif isinstance(event, FileMovedEvent):
            event_type = "moved"

        if event_type in info["events"]:
            record = {
                "event": event_type,
                "path": event.src_path,
                "timestamp": time.time(),
                "is_directory": event.is_directory
            }
            if isinstance(event, FileMovedEvent):
                record["dest_path"] = event.dest_path
            info["buffer"].append(record)
            if len(info["buffer"]) > 1000:
                info["buffer"] = info["buffer"][-500:]

def watch_get_changes(watch_id: str, limit: int = 100) -> Dict:
    with _watcher_lock:
        if watch_id not in _active_watchers:
            return {"status": "error", "message": "Unknown watch_id"}
        info = _active_watchers[watch_id]
        changes = info["buffer"][:limit]
        info["buffer"] = info["buffer"][limit:]
        return {"status": "success", "watch_id": watch_id, "changes": changes, "count": len(changes)}

def watch_stop(watch_id: str) -> Dict:
    with _watcher_lock:
        if watch_id not in _active_watchers:
            return {"status": "error", "message": "Unknown watch_id"}
        info = _active_watchers.pop(watch_id)
        # Остановка обработчика (отмена всех таймеров)
        info["handler"].stop()
        try:
            info["observer"].stop()
            info["observer"].join(timeout=5)
        except Exception as e:
            _log(f"Watcher stop error for {watch_id}: {e}")
        return {"status": "success", "watch_id": watch_id, "message": "Watcher stopped"}

def watch_list() -> Dict:
    with _watcher_lock:
        watchers = []
        for wid, info in _active_watchers.items():
            watchers.append({
                "watch_id": wid,
                "path": info["path"],
                "events": info["events"],
                "buffer_size": len(info["buffer"]),
                "uptime_sec": round(time.time() - info["started"], 1),
                "publish_events": info.get("publish_events", True)
            })
        return {"status": "success", "watchers": watchers, "count": len(watchers)}

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-watcher", "3.7")
server.register_tool("watch_start", {
    "description": "Start watching a directory for file changes with filtering and Event Bus publishing",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean", "default": True},
            "events": {"type": "array", "items": {"type": "string"}, "default": ["created", "modified", "deleted", "moved"]},
            "include_extensions": {"type": "array", "items": {"type": "string"}},
            "exclude_extensions": {"type": "array", "items": {"type": "string"}},
            "min_size_kb": {"type": "integer", "default": 0},
            "debounce_sec": {"type": "number", "default": 1.0},
            "publish_events": {"type": "boolean", "default": True}
        },
        "required": ["path"]
    }
}, lambda **kw: watch_start(
    kw["path"], kw.get("recursive", True), kw.get("events"),
    kw.get("interval", 2.0), kw.get("include_extensions"),
    kw.get("exclude_extensions"), kw.get("min_size_kb", 0),
    kw.get("debounce_sec", 1.0), kw.get("publish_events", True)
))

server.register_tool("watch_get_changes", {
    "description": "Get accumulated changes from a watcher",
    "inputSchema": {
        "type": "object",
        "properties": {
            "watch_id": {"type": "string"},
            "limit": {"type": "integer", "default": 100}
        },
        "required": ["watch_id"]
    }
}, lambda **kw: watch_get_changes(kw["watch_id"], kw.get("limit", 100)))

server.register_tool("watch_stop", {
    "description": "Stop a watcher and free resources",
    "inputSchema": {
        "type": "object",
        "properties": {"watch_id": {"type": "string"}},
        "required": ["watch_id"]
    }
}, lambda **kw: watch_stop(kw["watch_id"]))

server.register_tool("watch_list", {
    "description": "List active watchers",
    "inputSchema": {"type": "object", "properties": {}}
}, watch_list)

if __name__ == "__main__":
    server.run()