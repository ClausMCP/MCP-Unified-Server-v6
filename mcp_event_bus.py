#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Event Bus v2.0 (Context-Isolated & Thread-Safe)
Asynchronous message bus for inter-server communication.
Supports publish/subscribe with wildcard topics, persistent subscriptions,
path filtering, and conversation memory integration.

Изменения относительно v1.2 (API всех 5 инструментов сохранён полностью):
  • Все 6 вызовов sqlite3.connect() заменены на mcp_storage — постоянное
    соединение, WAL, busy_timeout, retry. Существующий event_bus.db
    (или путь из EVENT_BUS_DB) подхватывается без миграции.
  • ИСПРАВЛЕН БАГ: параметр ttl_seconds принимался в publish() и сохранялся
    в БД, но при очистке ПОЛНОСТЬЮ игнорировался — все события жили
    фиксированные 24 часа. Теперь очистка честно удаляет события по их TTL
    (published_at + ttl_seconds < now), а max_age_seconds остаётся верхним
    пределом жизни любого события.
  • Фоновый поток очистки стал останавливаемым (threading.Event) и
    защищён от дублирования; соединения потока закрываются при выходе.
"""
import os
import sys
import json
import time
import threading
from pathlib import Path
from typing import Dict, List, Set, Optional, Any
from collections import defaultdict
from datetime import datetime

import mcp_storage as storage
from mcp_shared import _log, BaseMCPServer, conversation_memory, dialog_ctx

# ─── Configuration ──────────────────────────────────────────────────────────
_LEGACY_DB = os.environ.get("EVENT_BUS_DB", os.path.join(os.path.dirname(__file__), "event_bus.db"))
DB_NAME = "event_bus"
DB_PATH = storage.register(DB_NAME, legacy_path=_LEGACY_DB)  # имя сохранено для совместимости

PERSISTENT = os.environ.get("EVENT_BUS_PERSISTENT", "true").lower() == "true"
MAX_EVENTS_PER_CLIENT = int(os.environ.get("EVENT_BUS_MAX_QUEUE", "1000"))
CLEANUP_INTERVAL_SEC = int(os.environ.get("EVENT_BUS_CLEANUP_INTERVAL", "3600"))


# ─── Database ────────────────────────────────────────────────────────────────
class EventBusDB:
    """Persistent storage for subscriptions (optional) and event history."""
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        if PERSISTENT:
            self._init_db()

    def _init_db(self):
        storage.executescript(self.db_name, """
            CREATE TABLE IF NOT EXISTS subscriptions (
                client_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                filter_path TEXT,
                created REAL,
                PRIMARY KEY (client_id, topic)
            );
            CREATE INDEX IF NOT EXISTS idx_subscriptions_topic ON subscriptions(topic);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                published_at REAL,
                ttl_seconds INTEGER DEFAULT 3600
            );
            CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic);
            CREATE INDEX IF NOT EXISTS idx_events_published_at ON events(published_at);
        """)

    def save_subscription(self, client_id: str, topic: str, filter_path: str = None):
        if not PERSISTENT: return
        storage.execute(
            self.db_name,
            "INSERT OR REPLACE INTO subscriptions (client_id, topic, filter_path, created) VALUES (?, ?, ?, ?)",
            (client_id, topic, filter_path, time.time())
        )

    def load_subscriptions(self) -> Dict[str, Dict[str, Set[str]]]:
        if not PERSISTENT: return {}
        result = defaultdict(dict)
        rows = storage.query_all(
            self.db_name, "SELECT client_id, topic, filter_path FROM subscriptions")
        for client_id, topic, filter_path in rows:
            result[client_id][topic] = filter_path
        return dict(result)

    def remove_subscription(self, client_id: str, topic: str = None):
        if not PERSISTENT: return
        if topic is None:
            storage.execute(self.db_name,
                            "DELETE FROM subscriptions WHERE client_id = ?", (client_id,))
        else:
            storage.execute(self.db_name,
                            "DELETE FROM subscriptions WHERE client_id = ? AND topic = ?",
                            (client_id, topic))

    def log_event(self, topic: str, payload: str, ttl: int = 3600):
        if not PERSISTENT: return
        storage.execute(
            self.db_name,
            "INSERT INTO events (topic, payload, published_at, ttl_seconds) VALUES (?, ?, ?, ?)",
            (topic, payload, time.time(), ttl)
        )

    def cleanup_old_events(self, max_age_seconds: int = 86400):
        """Удаляет события, у которых истёк собственный TTL, а также любые
        события старше max_age_seconds (верхний предел).

        Исправление v2.0: раньше ttl_seconds сохранялся, но игнорировался.
        """
        if not PERSISTENT: return
        now = time.time()
        cutoff = now - max_age_seconds
        deleted = storage.execute(
            self.db_name,
            """DELETE FROM events
               WHERE published_at + COALESCE(ttl_seconds, 3600) < ?
                  OR published_at < ?""",
            (now, cutoff)
        )
        if deleted > 0:
            _log(f"Event cleanup: removed {deleted} old events")


# ─── Event Bus Core ─────────────────────────────────────────────────────────
class EventBus:
    r"""
    In-memory pub/sub with optional persistence.
    Handles wildcard topics: "file.*" matches "file.created", "file.deleted".
    Path filtering: clients can subscribe to "file.created.D:\data\*" to receive only events under that path.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._subscriptions: Dict[str, Dict[str, Optional[str]]] = {}
        self._queues: Dict[str, List[Dict]] = defaultdict(list)
        self._db = EventBusDB() if PERSISTENT else None
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._restore_subscriptions()
        self._start_cleanup_thread()

    def _restore_subscriptions(self):
        if self._db:
            self._subscriptions = self._db.load_subscriptions()

    def _save_subscription(self, client_id: str, topic: str, filter_path: str = None):
        if self._db:
            self._db.save_subscription(client_id, topic, filter_path)

    def _remove_subscription(self, client_id: str, topic: str = None):
        if self._db:
            self._db.remove_subscription(client_id, topic)

    def subscribe(self, client_id: str, topic: str, filter_path: str = None) -> Dict:
        with self._lock:
            if client_id not in self._subscriptions:
                self._subscriptions[client_id] = {}
            self._subscriptions[client_id][topic] = filter_path
            self._save_subscription(client_id, topic, filter_path)
        return {"status": "subscribed", "client": client_id, "topic": topic, "filter": filter_path}

    def unsubscribe(self, client_id: str, topic: str = None) -> Dict:
        with self._lock:
            if topic is None:
                self._subscriptions.pop(client_id, None)
                self._remove_subscription(client_id)
                return {"status": "unsubscribed_all", "client": client_id}
            else:
                if client_id in self._subscriptions:
                    self._subscriptions[client_id].pop(topic, None)
                self._remove_subscription(client_id, topic)
            return {"status": "unsubscribed", "client": client_id, "topic": topic}

    def _matches_topic(self, sub_topic: str, event_topic: str) -> bool:
        if sub_topic == event_topic:
            return True
        if sub_topic.endswith('.*'):
            prefix = sub_topic[:-2]
            return event_topic.startswith(prefix + '.')
        return False

    def _matches_path(self, filter_path: Optional[str], event_payload: Dict) -> bool:
        if not filter_path:
            return True
        event_path = str(event_payload.get("path") or event_payload.get("source") or event_payload.get("target") or "")
        if not event_path:
            return False
        filter_norm = str(Path(filter_path)).lower().rstrip('\\/')
        event_norm = str(Path(event_path)).lower()
        return event_norm.startswith(filter_norm)

    def publish(self, topic: str, payload: Dict, ttl_seconds: int = 3600) -> Dict:
        d_id = dialog_ctx.get()
        matched = 0
        event_record = {
            "topic": topic,
            "payload": payload,
            "published_at": time.time(),
            "id": f"{topic}_{int(time.time()*1000)}"
        }

        with self._lock:
            # Persist event
            if self._db:
                self._db.log_event(topic, json.dumps(payload, default=str), ttl_seconds)

            # Log to conversation memory
            try:
                conversation_memory.add(
                    op="event_publish", paths={"topic": topic}, status="published",
                    dialog=d_id, context=f"Event {topic} published, payload keys: {list(payload.keys())}"
                )
            except Exception:
                pass

            # Distribute to queues
            for client_id, subs in list(self._subscriptions.items()):
                for sub_topic, filter_path in subs.items():
                    if self._matches_topic(sub_topic, topic):
                        if self._matches_path(filter_path, payload):
                            self._queues[client_id].append(event_record)
                            if len(self._queues[client_id]) > MAX_EVENTS_PER_CLIENT:
                                self._queues[client_id] = self._queues[client_id][-MAX_EVENTS_PER_CLIENT:]
                            matched += 1

        return {"status": "published", "topic": topic, "subscribers_matched": matched}

    def fetch_events(self, client_id: str, limit: int = 100) -> Dict:
        with self._lock:
            queue = self._queues.get(client_id, [])
            if not queue:
                return {"status": "ok", "events": [], "count": 0}
            events = queue[:limit]
            self._queues[client_id] = queue[limit:]
            if not self._queues[client_id]:
                del self._queues[client_id]
        return {"status": "ok", "events": events, "count": len(events), "remaining": len(self._queues.get(client_id, []))}

    def _start_cleanup_thread(self):
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return
        self._cleanup_stop.clear()
        def cleanup_loop():
            # wait() вместо sleep(): мгновенная реакция на остановку (v2.0)
            while not self._cleanup_stop.wait(CLEANUP_INTERVAL_SEC):
                if self._db:
                    try:
                        self._db.cleanup_old_events()
                    except Exception as e:
                        _log(f"Event bus cleanup error: {e}")
            storage.close_thread()
        self._cleanup_thread = threading.Thread(
            target=cleanup_loop, daemon=True, name="event_bus_cleanup")
        self._cleanup_thread.start()

    def stop(self, timeout: float = 5.0):
        """Корректная остановка фоновой очистки (v2.0)."""
        self._cleanup_stop.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=timeout)


# ─── Global Instance & Handlers ─────────────────────────────────────────────
_event_bus = EventBus()

def subscribe(client_id: str, topic: str, filter_path: str = None) -> Dict:
    return _event_bus.subscribe(client_id, topic, filter_path)

def unsubscribe(client_id: str, topic: str = None) -> Dict:
    return _event_bus.unsubscribe(client_id, topic)

def publish(topic: str, payload: Dict, ttl_seconds: int = 3600) -> Dict:
    return _event_bus.publish(topic, payload, ttl_seconds)

def fetch_events(client_id: str, limit: int = 100) -> Dict:
    return _event_bus.fetch_events(client_id, limit)

def get_stats() -> Dict:
    with _event_bus._lock:
        return {
            "clients": len(_event_bus._subscriptions),
            "total_subscriptions": sum(len(subs) for subs in _event_bus._subscriptions.values()),
            "queued_events_total": sum(len(q) for q in _event_bus._queues.values()),
            "persistent": PERSISTENT,
            "max_queue_per_client": MAX_EVENTS_PER_CLIENT
        }

# ─── Server Setup ──────────────────────────────────────────────────────────
server = BaseMCPServer("event-bus", "2.0")
server.register_tool("subscribe", {
    "description": "Subscribe a client (server) to event topics. Wildcards allowed: 'file.*'",
    "inputSchema": {
        "type": "object",
        "properties": {
            "client_id": {"type": "string", "description": "Unique identifier of the subscriber (e.g., 'fs_indexer')"},
            "topic": {"type": "string", "description": "Event topic, e.g., 'file.created', 'file.*'"},
            "filter_path": {"type": "string", "description": "Optional path prefix to filter events"}
        },
        "required": ["client_id", "topic"]
    }
}, lambda **kw: subscribe(kw["client_id"], kw["topic"], kw.get("filter_path")))

server.register_tool("unsubscribe", {
    "description": "Unsubscribe from a topic (or all topics)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "client_id": {"type": "string"},
            "topic": {"type": "string"}
        },
        "required": ["client_id"]
    }
}, lambda **kw: unsubscribe(kw["client_id"], kw.get("topic")))

server.register_tool("publish", {
    "description": "Publish an event to the bus",
    "inputSchema": {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "payload": {"type": "object"},
            "ttl_seconds": {"type": "integer", "default": 3600}
        },
        "required": ["topic", "payload"]
    }
}, lambda **kw: publish(kw["topic"], kw["payload"], kw.get("ttl_seconds", 3600)))

server.register_tool("fetch_events", {
    "description": "Retrieve pending events for a client (polling)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "client_id": {"type": "string"},
            "limit": {"type": "integer", "default": 100}
        },
        "required": ["client_id"]
    }
}, lambda **kw: fetch_events(kw["client_id"], kw.get("limit", 100)))

server.register_tool("event_bus_stats", {
    "description": "Get statistics of the event bus",
    "inputSchema": {"type": "object", "properties": {}}
}, get_stats)

if __name__ == "__main__":
    server.run()
