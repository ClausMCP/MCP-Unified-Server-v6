#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Episodic Memory v1.0 – полнофункциональная эпизодическая память.
Хранит события (эпизоды) с эмбеддингами, поддерживает семантический поиск,
автоматическую запись через Cognitive Bus, фоновую очистку.
"""
import os
import json
import sqlite3
import time
import threading
import hashlib
from typing import Dict, List, Optional, Any, Tuple
from contextlib import contextmanager
from datetime import datetime, timedelta

from mcp_shared import _log, BaseMCPServer, dialog_ctx
from mcp_cognitive_bus import subscribe, publish

# ========== Конфигурация ==========
EPISODIC_DB_PATH = os.environ.get("MCP_EPISODIC_DB", "./episodic.db")
EMBEDDING_ENABLED = os.environ.get("MCP_EPISODIC_EMBEDDING", "true").lower() == "true"
EMBEDDING_MODEL = os.environ.get("MCP_EPISODIC_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
RETENTION_DAYS = int(os.environ.get("MCP_EPISODIC_RETENTION_DAYS", "90"))
CLEANUP_INTERVAL = int(os.environ.get("MCP_EPISODIC_CLEANUP_INTERVAL", "3600"))  # раз в час

# Глобальные объекты (ленивая инициализация)
_embedder = None
_embedder_lock = threading.Lock()

def _get_embedder():
    """Ленивая загрузка модели эмбеддингов."""
    global _embedder
    if not EMBEDDING_ENABLED:
        return None
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    _log(f"[Episodic] Loading embedding model '{EMBEDDING_MODEL}'...")
                    _embedder = SentenceTransformer(EMBEDDING_MODEL)
                    _log("[Episodic] Embedder ready")
                except ImportError:
                    _log("[Episodic] sentence_transformers not installed, embedding disabled")
                    return None
    return _embedder


# ========== База данных ==========
class EpisodicDB:
    def __init__(self, db_path: str = EPISODIC_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    dialog TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT,
                    meta_json TEXT,
                    embedding_blob BLOB,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_dialog ON episodes(dialog)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_ts ON episodes(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_type ON episodes(event_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_dialog_ts ON episodes(dialog, ts)")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def add_episode(self, episode_id: str, dialog: str, event_type: str,
                    content: str, meta: Dict, embedding: Optional[bytes],
                    timestamp: float) -> bool:
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO episodes (id, ts, dialog, event_type, content, meta_json, embedding_blob, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (episode_id, timestamp, dialog, event_type, content,
                      json.dumps(meta, default=str), embedding, time.time()))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def search_by_text(self, dialog: Optional[str], event_type: Optional[str],
                       query_text: str, limit: int = 20) -> List[Dict]:
        """Поиск по вхождению подстроки в content или meta."""
        with self._get_conn() as conn:
            sql = "SELECT * FROM episodes WHERE 1=1"
            params = []
            if dialog:
                sql += " AND dialog = ?"
                params.append(dialog)
            if event_type:
                sql += " AND event_type = ?"
                params.append(event_type)
            if query_text:
                sql += " AND (content LIKE ? OR meta_json LIKE ?)"
                like = f"%{query_text}%"
                params.extend([like, like])
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def search_by_embedding(self, query_embedding: List[float], dialog: Optional[str],
                            event_type: Optional[str], limit: int = 10) -> List[Dict]:
        """Семантический поиск через косинусное сходство (вручную)."""
        if not query_embedding:
            return []
        # Извлекаем все эпизоды с эмбеддингами за последний месяц (для производительности)
        cutoff = time.time() - 30 * 86400
        with self._get_conn() as conn:
            sql = "SELECT * FROM episodes WHERE ts > ? AND embedding_blob IS NOT NULL"
            params = [cutoff]
            if dialog:
                sql += " AND dialog = ?"
                params.append(dialog)
            if event_type:
                sql += " AND event_type = ?"
                params.append(event_type)
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []
        # Вычисляем косинусное сходство
        import numpy as np
        q = np.array(query_embedding, dtype=np.float32)
        scored = []
        for row in rows:
            emb = np.frombuffer(row["embedding_blob"], dtype=np.float32)
            if len(emb) != len(q):
                continue
            sim = np.dot(q, emb) / (np.linalg.norm(q) * np.linalg.norm(emb) + 1e-8)
            scored.append((sim, dict(row)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    def get_timeline(self, dialog: str, start_time: Optional[float] = None,
                     end_time: Optional[float] = None, limit: int = 100) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM episodes WHERE dialog = ?"
            params = [dialog]
            if start_time:
                sql += " AND ts >= ?"
                params.append(start_time)
            if end_time:
                sql += " AND ts <= ?"
                params.append(end_time)
            sql += " ORDER BY ts ASC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def delete_old(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._get_conn() as conn:
            cur = conn.execute("DELETE FROM episodes WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount
            conn.commit()
            return deleted

    def get_stats(self) -> Dict:
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            by_type = conn.execute(
                "SELECT event_type, COUNT(*) FROM episodes GROUP BY event_type"
            ).fetchall()
            oldest = conn.execute("SELECT MIN(ts) FROM episodes").fetchone()[0]
            newest = conn.execute("SELECT MAX(ts) FROM episodes").fetchone()[0]
            with_emb = conn.execute("SELECT COUNT(*) FROM episodes WHERE embedding_blob IS NOT NULL").fetchone()[0]
            return {
                "total_episodes": total,
                "by_event_type": dict(by_type),
                "oldest_ts": oldest,
                "newest_ts": newest,
                "with_embeddings": with_emb,
                "retention_days": RETENTION_DAYS
            }

    def delete_by_dialog(self, dialog: str) -> int:
        with self._get_conn() as conn:
            cur = conn.execute("DELETE FROM episodes WHERE dialog = ?", (dialog,))
            deleted = cur.rowcount
            conn.commit()
            return deleted


# ========== Движок эпизодической памяти ==========
class EpisodicMemoryEngine:
    def __init__(self):
        self.db = EpisodicDB()
        self._running = False
        self._cleanup_thread = None
        self._stop_event = threading.Event()
        self._auto_subscribe = True

    def start(self):
        """Запускает фоновую очистку."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True, name="episodic_cleanup")
        self._cleanup_thread.start()
        _log("[EpisodicMemory] Engine started, auto-subscribe enabled")

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5)
        _log("[EpisodicMemory] Engine stopped")

    def _cleanup_worker(self):
        while not self._stop_event.is_set():
            try:
                deleted = self.db.delete_old(RETENTION_DAYS)
                if deleted:
                    _log(f"[EpisodicMemory] Cleaned up {deleted} old episodes")
            except Exception as e:
                _log(f"[EpisodicMemory] Cleanup error: {e}")
            self._stop_event.wait(CLEANUP_INTERVAL)

    def record(self, dialog: str, event_type: str, content: str, meta: Dict = None, ts: float = None) -> str:
        """Записать эпизод. Возвращает ID."""
        if ts is None:
            ts = time.time()
        episode_id = hashlib.md5(f"{dialog}_{event_type}_{ts}_{content[:100]}".encode()).hexdigest()[:16]
        embedding = None
        if EMBEDDING_ENABLED:
            embedder = _get_embedder()
            if embedder and content:
                try:
                    emb = embedder.encode(content).astype('float32').tobytes()
                    embedding = emb
                except Exception as e:
                    _log(f"[EpisodicMemory] Embedding error: {e}")
        self.db.add_episode(episode_id, dialog, event_type, content, meta or {}, embedding, ts)
        # Публикуем событие о новом эпизоде
        try:
            publish("episode_recorded", {
                "episode_id": episode_id,
                "dialog": dialog,
                "event_type": event_type,
                "ts": ts
            }, source="episodic_memory")
        except Exception:
            pass
        return episode_id

    def search(self, dialog: str = None, event_type: str = None,
               query: str = None, limit: int = 20) -> List[Dict]:
        """Текстовый поиск."""
        return self.db.search_by_text(dialog, event_type, query or "", limit)

    def similar(self, query_text: str, dialog: str = None,
                event_type: str = None, limit: int = 10) -> List[Dict]:
        """Семантический поиск по тексту."""
        if not EMBEDDING_ENABLED:
            _log("[EpisodicMemory] Embedding disabled, falling back to text search")
            return self.search(dialog, event_type, query_text, limit)
        embedder = _get_embedder()
        if not embedder:
            return self.search(dialog, event_type, query_text, limit)
        try:
            emb = embedder.encode(query_text).tolist()
        except Exception as e:
            _log(f"[EpisodicMemory] Embedding error: {e}")
            return self.search(dialog, event_type, query_text, limit)
        return self.db.search_by_embedding(emb, dialog, event_type, limit)

    def timeline(self, dialog: str, start_time: float = None,
                 end_time: float = None, limit: int = 100) -> List[Dict]:
        return self.db.get_timeline(dialog, start_time, end_time, limit)

    def stats(self) -> Dict:
        return self.db.get_stats()

    def cleanup_now(self, older_than_days: int = None) -> Dict:
        days = older_than_days or RETENTION_DAYS
        deleted = self.db.delete_old(days)
        return {"deleted": deleted, "retention_days": days}

    def delete_dialog(self, dialog: str) -> Dict:
        deleted = self.db.delete_by_dialog(dialog)
        return {"dialog": dialog, "deleted_episodes": deleted}


# ========== Автоматическая запись через Cognitive Bus ==========
_engine = EpisodicMemoryEngine()
_engine.start()

def _auto_record(event_name: str, data: Dict, source: str):
    """Обработчик событий для автоматической записи эпизодов."""
    # Извлекаем dialog_id из данных, если есть
    dialog_id = data.get("dialog_id") or data.get("dialog") or "global"
    # Формируем content
    content = ""
    if "statement" in data:
        content = data["statement"]
    elif "reason" in data:
        content = data["reason"]
    elif "message" in data:
        content = data["message"]
    else:
        content = f"{event_name}: {json.dumps(data, ensure_ascii=False)[:200]}"
    meta = {"source": source, "original_event": event_name, "data": data}
    _engine.record(dialog_id, event_name, content, meta)

def subscribe_auto():
    """Подписывается на значимые события Cognitive Bus."""
    events_to_record = [
        "hypothesis_created", "hypothesis_verified", "hypothesis_rejected", "hypothesis_promoted",
        "goal_created", "goal_completed", "goal_failed",
        "plan_created", "plan_completed", "plan_failed",
        "fact_added", "rule_added",
        "tool_called", "tool_result",
        "reflection_completed"
    ]
    for ev in events_to_record:
        try:
            subscribe(ev, lambda data, ev_name=ev, src="auto": _auto_record(ev_name, data, src))
            _log(f"[EpisodicMemory] Subscribed to {ev}")
        except Exception as e:
            _log(f"[EpisodicMemory] Failed to subscribe to {ev}: {e}")


# ========== MCP-инструменты ==========
server = BaseMCPServer("episodic-memory", "1.0")

server.register_tool("episodic_record", {
    "description": "Записать эпизод (событие) в эпизодическую память",
    "inputSchema": {
        "type": "object",
        "properties": {
            "dialog": {"type": "string", "description": "ID диалога"},
            "event_type": {"type": "string", "description": "Тип события (например, user_message, tool_call)"},
            "content": {"type": "string", "description": "Текстовое содержание события"},
            "meta": {"type": "object", "description": "Дополнительные метаданные"},
            "timestamp": {"type": "number", "description": "Unix timestamp (опционально)"}
        },
        "required": ["dialog", "event_type", "content"]
    }
}, lambda **kw: {"status": "success", "episode_id": _engine.record(
    kw["dialog"], kw["event_type"], kw["content"], kw.get("meta"), kw.get("timestamp")
)})

server.register_tool("episodic_search", {
    "description": "Поиск эпизодов по текстовому запросу (подстрока в content или meta)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "dialog": {"type": "string"},
            "event_type": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 20}
        }
    }
}, lambda **kw: {"status": "success", "episodes": _engine.search(
    kw.get("dialog"), kw.get("event_type"), kw.get("query"), kw.get("limit", 20)
)})

server.register_tool("episodic_similar", {
    "description": "Семантический поиск похожих эпизодов по смыслу (требует эмбеддинги)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query_text": {"type": "string"},
            "dialog": {"type": "string"},
            "event_type": {"type": "string"},
            "limit": {"type": "integer", "default": 10}
        },
        "required": ["query_text"]
    }
}, lambda **kw: {"status": "success", "episodes": _engine.similar(
    kw["query_text"], kw.get("dialog"), kw.get("event_type"), kw.get("limit", 10)
)})

server.register_tool("episodic_timeline", {
    "description": "Получить хронологию эпизодов для диалога",
    "inputSchema": {
        "type": "object",
        "properties": {
            "dialog": {"type": "string"},
            "start_time": {"type": "number"},
            "end_time": {"type": "number"},
            "limit": {"type": "integer", "default": 100}
        },
        "required": ["dialog"]
    }
}, lambda **kw: {"status": "success", "episodes": _engine.timeline(
    kw["dialog"], kw.get("start_time"), kw.get("end_time"), kw.get("limit", 100)
)})

server.register_tool("episodic_stats", {
    "description": "Статистика эпизодической памяти",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: {"status": "success", "stats": _engine.stats()})

server.register_tool("episodic_cleanup", {
    "description": "Принудительная очистка старых эпизодов",
    "inputSchema": {
        "type": "object",
        "properties": {
            "older_than_days": {"type": "integer", "default": None}
        }
    }
}, lambda **kw: {"status": "success", "result": _engine.cleanup_now(kw.get("older_than_days"))})

server.register_tool("episodic_delete_dialog", {
    "description": "Удалить все эпизоды для указанного диалога",
    "inputSchema": {
        "type": "object",
        "properties": {"dialog": {"type": "string"}},
        "required": ["dialog"]
    }
}, lambda **kw: {"status": "success", "result": _engine.delete_dialog(kw["dialog"])})

# ========== Плагинная метаинформация ==========
__mcp_plugin__ = {
    "name": "episodic-memory",
    "version": "1.0",
    "description": "Эпизодическая память с эмбеддингами, семантическим поиском и автозаписью событий",
    "dependencies": ["sentence_transformers"] if EMBEDDING_ENABLED else [],
    "on_load": lambda: (_log("[EpisodicMemory] v1.0 loaded"), subscribe_auto()),
    "on_unload": lambda: _engine.stop()
}

if __name__ == "__main__":
    # Если запущен как отдельный MCP-сервер
    subscribe_auto()  # подписка при прямом запуске
    server.run()