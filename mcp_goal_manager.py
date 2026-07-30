#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Goal Manager v1.1 – с контекстом целей, событиями и интеграцией Cognitive Bus.
"""
import os
import json
import time
import sqlite3
import threading
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Any, Set
from dataclasses import dataclass, asdict
from enum import Enum
from contextlib import contextmanager

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx,
    normalize_path, _ensure_allowed
)

try:
    from mcp_task_manager import executor as task_executor
    TASK_MANAGER_AVAILABLE = True
except ImportError:
    TASK_MANAGER_AVAILABLE = False

try:
    from mcp_memory_graph import _graph_db
    GRAPH_AVAILABLE = True
except ImportError:
    GRAPH_AVAILABLE = False

try:
    from mcp_cognitive_bus import publish
    COGNITIVE_BUS_AVAILABLE = True
except ImportError:
    COGNITIVE_BUS_AVAILABLE = False
    def publish(*args, **kwargs): pass

GOALS_DB_PATH = os.environ.get("MCP_GOALS_DB", os.path.join(os.path.dirname(__file__), "goals.db"))
GOAL_CHECK_INTERVAL_MIN = int(os.environ.get("MCP_GOAL_CHECK_INTERVAL_MIN", "5"))
GOAL_AUTO_PLAN = os.environ.get("MCP_GOAL_AUTO_PLAN", "true").lower() == "true"

class GoalStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

class GoalType(str, Enum):
    ATOMIC = "atomic"
    COMPOSITE = "composite"
    MILESTONE = "milestone"

class GoalsDB:
    def __init__(self, db_path: str = GOALS_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    title TEXT NOT NULL,
                    description TEXT,
                    goal_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER DEFAULT 0,
                    progress REAL DEFAULT 0.0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    deadline REAL,
                    metadata TEXT,
                    context_json TEXT,
                    FOREIGN KEY(parent_id) REFERENCES goals(goal_id) ON DELETE CASCADE
                )
            """)
            # Добавляем колонку context_json, если её нет (миграция)
            try:
                conn.execute("ALTER TABLE goals ADD COLUMN context_json TEXT")
            except sqlite3.OperationalError:
                pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS goal_dependencies (
                    goal_id TEXT NOT NULL,
                    depends_on_goal_id TEXT NOT NULL,
                    PRIMARY KEY (goal_id, depends_on_goal_id),
                    FOREIGN KEY(goal_id) REFERENCES goals(goal_id) ON DELETE CASCADE,
                    FOREIGN KEY(depends_on_goal_id) REFERENCES goals(goal_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS goal_conditions (
                    condition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL,
                    condition_type TEXT NOT NULL,
                    condition_target TEXT,
                    satisfied INTEGER DEFAULT 0,
                    last_checked REAL,
                    FOREIGN KEY(goal_id) REFERENCES goals(goal_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS goal_tasks (
                    goal_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    PRIMARY KEY (goal_id, task_id),
                    FOREIGN KEY(goal_id) REFERENCES goals(goal_id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_priority ON goals(priority)")
            conn.commit()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def add_goal(self, goal_id: str, title: str, description: str,
                 goal_type: str, parent_id: str = None, priority: int = 0,
                 deadline: float = None, metadata: Dict = None,
                 context: Dict = None) -> bool:
        now = time.time()
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO goals
                    (goal_id, parent_id, title, description, goal_type, status, priority,
                     progress, created_at, updated_at, deadline, metadata, context_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (goal_id, parent_id, title, description, goal_type,
                      GoalStatus.PENDING.value, priority, 0.0, now, now, deadline,
                      json.dumps(metadata) if metadata else None,
                      json.dumps(context) if context else None))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def update_goal(self, goal_id: str, **kwargs):
        allowed = ['title', 'description', 'status', 'priority', 'progress',
                   'updated_at', 'completed_at', 'deadline', 'metadata', 'context_json']
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        updates['updated_at'] = time.time()
        # Если статус меняется на COMPLETED, публикуем событие
        if updates.get('status') == GoalStatus.COMPLETED.value:
            try:
                publish("goal_completed", {"goal_id": goal_id, **updates}, source="goal_manager")
            except Exception:
                pass
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        with self._get_conn() as conn:
            conn.execute(f"UPDATE goals SET {set_clause} WHERE goal_id = ?",
                         list(updates.values()) + [goal_id])
            conn.commit()
            # Публикация события goal_updated
            try:
                publish("goal_updated", {"goal_id": goal_id, "changes": updates}, source="goal_manager")
            except Exception:
                pass

    def get_goal(self, goal_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
            return dict(row) if row else None

    def get_subgoals(self, parent_id: str) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM goals WHERE parent_id = ? ORDER BY priority DESC", (parent_id,)).fetchall()
            return [dict(r) for r in rows]

    def add_dependency(self, goal_id: str, depends_on_goal_id: str):
        with self._get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO goal_dependencies VALUES (?, ?)",
                         (goal_id, depends_on_goal_id))
            conn.commit()

    def get_dependencies(self, goal_id: str) -> List[str]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT depends_on_goal_id FROM goal_dependencies WHERE goal_id = ?", (goal_id,)).fetchall()
            return [r[0] for r in rows]

    def add_condition(self, goal_id: str, condition_type: str, condition_target: str):
        with self._get_conn() as conn:
            cur = conn.execute("""
                INSERT INTO goal_conditions (goal_id, condition_type, condition_target, satisfied, last_checked)
                VALUES (?, ?, ?, 0, ?)
            """, (goal_id, condition_type, condition_target, time.time()))
            conn.commit()
            return cur.lastrowid

    def update_condition_satisfied(self, condition_id: int, satisfied: bool):
        with self._get_conn() as conn:
            conn.execute("UPDATE goal_conditions SET satisfied = ?, last_checked = ? WHERE condition_id = ?",
                         (1 if satisfied else 0, time.time(), condition_id))
            conn.commit()

    def get_conditions(self, goal_id: str) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM goal_conditions WHERE goal_id = ?", (goal_id,)).fetchall()
            return [dict(r) for r in rows]

    def link_task(self, goal_id: str, task_id: str):
        with self._get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO goal_tasks VALUES (?, ?)", (goal_id, task_id))
            conn.commit()

    def get_root_goals(self, status: str = None) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM goals WHERE parent_id IS NULL"
            params = []
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY priority DESC, created_at ASC"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_all_goals(self, status: str = None, limit: int = 100) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM goals"
            params = []
            if status:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY priority DESC, created_at ASC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> Dict:
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
            by_status = {}
            for s in GoalStatus:
                cnt = conn.execute("SELECT COUNT(*) FROM goals WHERE status = ?", (s.value,)).fetchone()[0]
                by_status[s.value] = cnt
            avg_progress = conn.execute("SELECT AVG(progress) FROM goals WHERE status = 'in_progress'").fetchone()[0] or 0
            return {"total_goals": total, "by_status": by_status, "avg_progress_in_progress": round(avg_progress, 2)}

def check_condition(condition_type: str, condition_target: str) -> bool:
    if not GRAPH_AVAILABLE:
        return False
    try:
        if condition_type == "fact":
            with _graph_db._get_conn() as conn:
                row = conn.execute(
                    "SELECT confidence FROM beliefs WHERE statement LIKE ? AND confidence > 0.7 LIMIT 1",
                    (f"%{condition_target}%",)
                ).fetchone()
                return row is not None
        elif condition_type == "entity_exists":
            with _graph_db._get_conn() as conn:
                row = conn.execute("SELECT 1 FROM entities WHERE name = ?", (condition_target,)).fetchone()
                return row is not None
        elif condition_type == "relation_exists":
            parts = condition_target.split("|")
            if len(parts) != 3:
                return False
            source, rel_type, target = parts
            with _graph_db._get_conn() as conn:
                row = conn.execute("""
                    SELECT 1 FROM relations r
                    JOIN entities e1 ON r.source_id = e1.entity_id
                    JOIN entities e2 ON r.target_id = e2.entity_id
                    WHERE e1.name = ? AND r.relation_type = ? AND e2.name = ?
                """, (source, rel_type, target)).fetchone()
                return row is not None
    except Exception as e:
        _log(f"[GoalManager] Condition check error: {e}")
    return False

class GoalScheduler:
    def __init__(self):
        self.db = GoalsDB()
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="goal_scheduler")
        self._thread.start()
        _log("[GoalManager] Scheduler started")

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _log("[GoalManager] Scheduler stopped")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._process_goals()
            except Exception as e:
                _log(f"[GoalManager] Scheduler error: {e}")
            self._stop_event.wait(GOAL_CHECK_INTERVAL_MIN * 60)

    def _process_goals(self):
        self._update_progress_from_subgoals()
        self._check_conditions()
        self._start_ready_goals()
        self._propagate_completion()

    def _update_progress_from_subgoals(self):
        with self.db._get_conn() as conn:
            composite_goals = conn.execute(
                "SELECT goal_id FROM goals WHERE goal_type = ? AND status != ?",
                (GoalType.COMPOSITE.value, GoalStatus.COMPLETED.value)
            ).fetchall()
        for row in composite_goals:
            goal_id = row[0]
            subgoals = self.db.get_subgoals(goal_id)
            if not subgoals:
                continue
            completed = sum(1 for sg in subgoals if sg["status"] == GoalStatus.COMPLETED.value)
            total = len(subgoals)
            progress = completed / total if total > 0 else 0
            if completed == total and total > 0:
                self.db.update_goal(goal_id, status=GoalStatus.COMPLETED.value, progress=1.0,
                                    completed_at=time.time())
            else:
                self.db.update_goal(goal_id, progress=progress)

    def _check_conditions(self):
        goals = self.db.get_all_goals(status=None, limit=200)
        for goal in goals:
            conditions = self.db.get_conditions(goal["goal_id"])
            if not conditions:
                continue
            all_satisfied = True
            for cond in conditions:
                satisfied = check_condition(cond["condition_type"], cond["condition_target"])
                if satisfied != bool(cond["satisfied"]):
                    self.db.update_condition_satisfied(cond["condition_id"], satisfied)
                if not satisfied:
                    all_satisfied = False
            current_status = goal["status"]
            if all_satisfied and current_status == GoalStatus.BLOCKED.value:
                self.db.update_goal(goal["goal_id"], status=GoalStatus.PENDING.value)
            elif not all_satisfied and current_status not in (GoalStatus.COMPLETED.value, GoalStatus.CANCELLED.value):
                self.db.update_goal(goal["goal_id"], status=GoalStatus.BLOCKED.value)

    def _start_ready_goals(self):
        goals = self.db.get_all_goals(status=GoalStatus.PENDING.value, limit=100)
        for goal in goals:
            goal_id = goal["goal_id"]
            deps = self.db.get_dependencies(goal_id)
            deps_satisfied = True
            for dep_id in deps:
                dep_goal = self.db.get_goal(dep_id)
                if not dep_goal or dep_goal["status"] != GoalStatus.COMPLETED.value:
                    deps_satisfied = False
                    break
            if not deps_satisfied:
                continue
            conditions = self.db.get_conditions(goal_id)
            conditions_satisfied = all(c["satisfied"] for c in conditions)
            if not conditions_satisfied:
                continue
            self._start_goal(goal)

    def _start_goal(self, goal: Dict):
        goal_id = goal["goal_id"]
        goal_type = goal["goal_type"]
        if goal_type == GoalType.ATOMIC.value:
            if not TASK_MANAGER_AVAILABLE:
                _log(f"[GoalManager] TaskManager not available, cannot start atomic goal {goal_id}")
                return
            metadata = json.loads(goal["metadata"]) if goal["metadata"] else {}
            tool_name = metadata.get("tool_name")
            tool_args = metadata.get("tool_args", {})
            if not tool_name:
                _log(f"[GoalManager] Atomic goal {goal_id} missing tool_name in metadata")
                return
            try:
                from mcp_task_manager import submit_task
                result = submit_task(tool_name, tool_args, dialog_id="goal_manager")
                if result.get("task_id"):
                    self.db.link_task(goal_id, result["task_id"])
                    self.db.update_goal(goal_id, status=GoalStatus.IN_PROGRESS.value)
                    _log(f"[GoalManager] Started atomic goal {goal_id} -> task {result['task_id']}")
                    publish("goal_updated", {"goal_id": goal_id, "status": "in_progress"}, source="goal_manager")
                else:
                    _log(f"[GoalManager] Failed to start task for goal {goal_id}: {result}")
                    self.db.update_goal(goal_id, status=GoalStatus.FAILED.value)
            except Exception as e:
                _log(f"[GoalManager] Error starting task for goal {goal_id}: {e}")
                self.db.update_goal(goal_id, status=GoalStatus.FAILED.value)
        elif goal_type == GoalType.COMPOSITE.value:
            self.db.update_goal(goal_id, status=GoalStatus.IN_PROGRESS.value)
            _log(f"[GoalManager] Started composite goal {goal_id}")
        elif goal_type == GoalType.MILESTONE.value:
            self.db.update_goal(goal_id, status=GoalStatus.COMPLETED.value, completed_at=time.time())
            _log(f"[GoalManager] Milestone {goal_id} completed")

    def _propagate_completion(self):
        if not TASK_MANAGER_AVAILABLE:
            return
        with self.db._get_conn() as conn:
            rows = conn.execute("""
                SELECT g.goal_id, gt.task_id
                FROM goal_tasks gt
                JOIN goals g ON gt.goal_id = g.goal_id
                WHERE g.status = ?
            """, (GoalStatus.IN_PROGRESS.value,)).fetchall()
        for row in rows:
            goal_id, task_id = row[0], row[1]
            try:
                from mcp_task_manager import task_status
                status_info = task_status(task_id)
                if status_info.get("status") == "completed":
                    self.db.update_goal(goal_id, status=GoalStatus.COMPLETED.value,
                                        progress=1.0, completed_at=time.time())
                    _log(f"[GoalManager] Goal {goal_id} completed via task {task_id}")
                elif status_info.get("status") == "failed":
                    self.db.update_goal(goal_id, status=GoalStatus.FAILED.value)
                    _log(f"[GoalManager] Goal {goal_id} failed via task {task_id}")
            except Exception as e:
                _log(f"[GoalManager] Error checking task {task_id}: {e}")

_goal_scheduler = GoalScheduler()
_goal_scheduler.start()

def goal_create(title: str, description: str, goal_type: str = "atomic",
                parent_id: str = None, priority: int = 0,
                deadline: float = None, tool_name: str = None,
                tool_args: Dict = None,
                depends_on: List[str] = None,
                conditions: List[Dict] = None,
                context: Dict = None) -> Dict:
    goal_id = hashlib.md5(f"{title}_{time.time()}".encode()).hexdigest()[:12]
    metadata = {}
    if tool_name:
        metadata["tool_name"] = tool_name
        metadata["tool_args"] = tool_args or {}
    db = GoalsDB()
    ok = db.add_goal(goal_id, title, description, goal_type, parent_id, priority, deadline, metadata, context)
    if not ok:
        return {"status": "error", "message": "Goal with same ID already exists"}
    if depends_on:
        for dep_id in depends_on:
            db.add_dependency(goal_id, dep_id)
    if conditions:
        for cond in conditions:
            db.add_condition(goal_id, cond["type"], cond["target"])
    publish("goal_created", {"goal_id": goal_id, "title": title, "type": goal_type}, source="goal_manager")
    return {"status": "created", "goal_id": goal_id, "title": title}

def goal_update(goal_id: str, **kwargs) -> Dict:
    db = GoalsDB()
    db.update_goal(goal_id, **kwargs)
    return {"status": "updated", "goal_id": goal_id}

def goal_get(goal_id: str) -> Dict:
    db = GoalsDB()
    goal = db.get_goal(goal_id)
    if not goal:
        return {"status": "error", "message": "Goal not found"}
    subgoals = db.get_subgoals(goal_id)
    deps = db.get_dependencies(goal_id)
    conditions = db.get_conditions(goal_id)
    return {
        "status": "success",
        "goal": goal,
        "subgoals": subgoals,
        "depends_on": deps,
        "conditions": conditions
    }

def goal_list(status: str = None, limit: int = 50) -> Dict:
    db = GoalsDB()
    goals = db.get_all_goals(status, limit)
    return {"status": "success", "goals": goals, "count": len(goals)}

def goal_list_root(status: str = None) -> Dict:
    db = GoalsDB()
    goals = db.get_root_goals(status)
    return {"status": "success", "root_goals": goals, "count": len(goals)}

def goal_delete(goal_id: str, cascade: bool = True) -> Dict:
    db = GoalsDB()
    with db._get_conn() as conn:
        if cascade:
            conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
        else:
            conn.execute("UPDATE goals SET status = ? WHERE goal_id = ?", (GoalStatus.CANCELLED.value, goal_id))
        conn.commit()
    return {"status": "deleted", "goal_id": goal_id}

def goal_stats() -> Dict:
    db = GoalsDB()
    return db.get_stats()

def goal_force_check() -> Dict:
    _goal_scheduler._process_goals()
    return {"status": "check_completed"}

server = BaseMCPServer("goal-manager", "1.1")

server.register_tool("goal_create", {
    "description": "Создать новую цель (атомарную, составную или веху). Для атомарных целей нужно указать tool_name и tool_args.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "goal_type": {"type": "string", "enum": ["atomic", "composite", "milestone"], "default": "atomic"},
            "parent_id": {"type": "string"},
            "priority": {"type": "integer", "default": 0},
            "deadline": {"type": "number"},
            "tool_name": {"type": "string"},
            "tool_args": {"type": "object"},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "conditions": {"type": "array", "items": {"type": "object", "properties": {"type": {"type": "string"}, "target": {"type": "string"}}}},
            "context": {"type": "object", "description": "Контекст цели (ресурсы, ограничения, сроки)"}
        },
        "required": ["title", "description"]
    }
}, lambda **kw: goal_create(
    kw["title"], kw["description"], kw.get("goal_type", "atomic"),
    kw.get("parent_id"), kw.get("priority", 0), kw.get("deadline"),
    kw.get("tool_name"), kw.get("tool_args", {}),
    kw.get("depends_on"), kw.get("conditions"),
    kw.get("context")
))

server.register_tool("goal_update", {
    "description": "Обновить параметры цели (статус, прогресс, приоритет и т.д.)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "goal_id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "failed", "blocked", "cancelled"]},
            "progress": {"type": "number"},
            "priority": {"type": "integer"}
        },
        "required": ["goal_id"]
    }
}, lambda **kw: goal_update(kw["goal_id"], **{k: v for k, v in kw.items() if k != "goal_id"}))

server.register_tool("goal_get", {
    "description": "Получить детальную информацию о цели, включая подцели, зависимости и условия",
    "inputSchema": {"type": "object", "properties": {"goal_id": {"type": "string"}}, "required": ["goal_id"]}
}, lambda **kw: goal_get(kw["goal_id"]))

server.register_tool("goal_list", {
    "description": "Список целей с фильтрацией по статусу",
    "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "limit": {"type": "integer", "default": 50}}}
}, lambda **kw: goal_list(kw.get("status"), kw.get("limit", 50)))

server.register_tool("goal_list_root", {
    "description": "Список корневых целей (верхнего уровня)",
    "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}
}, lambda **kw: goal_list_root(kw.get("status")))

server.register_tool("goal_delete", {
    "description": "Удалить или отменить цель",
    "inputSchema": {"type": "object", "properties": {"goal_id": {"type": "string"}, "cascade": {"type": "boolean", "default": True}}, "required": ["goal_id"]}
}, lambda **kw: goal_delete(kw["goal_id"], kw.get("cascade", True)))

server.register_tool("goal_stats", {
    "description": "Статистика по целям",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: goal_stats())

server.register_tool("goal_force_check", {
    "description": "Принудительно запустить проверку и выполнение готовых целей",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: goal_force_check())

__mcp_plugin__ = {
    "name": "goal-manager",
    "version": "1.1",
    "description": "Управление целями, подцелями, зависимостями, интеграция с TaskManager, контекстом целей и Cognitive Bus",
    "dependencies": [],
    "on_load": lambda: _log("[GoalManager] v1.1 loaded. Context and events enabled."),
    "on_unload": lambda: _goal_scheduler.stop()
}

if __name__ == "__main__":
    server.run()