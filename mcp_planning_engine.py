#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Planning Engine v2.1 – с replanning, предусловиями/постусловиями, иерархией,
интеграцией с World Model, событиями Cognitive Bus.
"""
import os
import json
import time
import sqlite3
import threading
import hashlib
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from dataclasses import dataclass, asdict
from contextlib import contextmanager
from enum import Enum

from mcp_shared import _log, BaseMCPServer, dialog_ctx

try:
    from mcp_cognitive_bus import publish
    COGNITIVE_BUS_AVAILABLE = True
except ImportError:
    COGNITIVE_BUS_AVAILABLE = False
    def publish(*args, **kwargs): pass

PLANNING_DB = os.environ.get("MCP_PLANNING_DB", os.path.join(os.path.dirname(__file__), "planning.db"))
MAX_REPLAN_ATTEMPTS = int(os.environ.get("MCP_MAX_REPLAN_ATTEMPTS", "3"))
REPLAN_TIMEOUT = int(os.environ.get("MCP_REPLAN_TIMEOUT", "60"))

class PlanningDB:
    def __init__(self, db_path: str = PLANNING_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    name TEXT,
                    steps_json TEXT NOT NULL,
                    current_step INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    parent_plan_id TEXT,
                    hierarchy_level INTEGER DEFAULT 0,
                    replan_attempts INTEGER DEFAULT 0,
                    evaluation_metrics TEXT,
                    FOREIGN KEY(parent_plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plan_steps (
                    step_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    tool_args TEXT,
                    precondition TEXT,
                    postcondition TEXT,
                    expected_effects TEXT,
                    status TEXT DEFAULT 'pending',
                    task_id TEXT,
                    started_at REAL,
                    finished_at REAL,
                    retry_count INTEGER DEFAULT 0,
                    failure_reason TEXT,
                    FOREIGN KEY(plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS replan_history (
                    replan_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    triggered_at REAL NOT NULL,
                    reason TEXT,
                    failed_step_index INTEGER,
                    new_steps_json TEXT,
                    success INTEGER DEFAULT 0,
                    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_goal ON plans(goal_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_parent ON plans(parent_plan_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_plan ON plan_steps(plan_id)")
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

    def create_plan(self, plan_id: str, goal_id: str, name: str, steps: List[Dict],
                    parent_plan_id: str = None, hierarchy_level: int = 0) -> bool:
        now = time.time()
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO plans (plan_id, goal_id, name, steps_json, current_step, status,
                                       created_at, updated_at, parent_plan_id, hierarchy_level, replan_attempts)
                    VALUES (?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?, 0)
                """, (plan_id, goal_id, name, json.dumps(steps), now, now, parent_plan_id, hierarchy_level))
                for idx, step in enumerate(steps):
                    conn.execute("""
                        INSERT INTO plan_steps
                        (plan_id, step_index, tool_name, tool_args, precondition, postcondition, expected_effects, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    """, (plan_id, idx, step["tool_name"],
                          json.dumps(step.get("args", {})),
                          json.dumps(step.get("precondition", {})),
                          json.dumps(step.get("postcondition", {})),
                          json.dumps(step.get("expected_effects", {}))))
                conn.commit()
                publish("plan_created", {"plan_id": plan_id, "goal_id": goal_id, "steps": len(steps)}, source="planning_engine")
                return True
            except sqlite3.IntegrityError:
                return False

    def get_plan(self, plan_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            plan = conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if not plan:
                return None
            steps = conn.execute("SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY step_index", (plan_id,)).fetchall()
            result = dict(plan)
            result["steps"] = [dict(s) for s in steps]
            return result

    def update_plan_status(self, plan_id: str, status: str, evaluation_metrics: Dict = None):
        updates = {"status": status, "updated_at": time.time()}
        if evaluation_metrics:
            updates["evaluation_metrics"] = json.dumps(evaluation_metrics)
        if status == "completed":
            updates["completed_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [plan_id]
        with self._get_conn() as conn:
            conn.execute(f"UPDATE plans SET {set_clause} WHERE plan_id = ?", values)
            conn.commit()
            if status == "failed":
                publish("plan_failed", {"plan_id": plan_id, "reason": evaluation_metrics.get("error", "unknown") if evaluation_metrics else "unknown"}, source="planning_engine")

    def increment_replan_attempts(self, plan_id: str) -> int:
        with self._get_conn() as conn:
            conn.execute("UPDATE plans SET replan_attempts = replan_attempts + 1, updated_at = ? WHERE plan_id = ?",
                         (time.time(), plan_id))
            conn.commit()
            row = conn.execute("SELECT replan_attempts FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            return row[0] if row else 0

    def update_step(self, plan_id: str, step_index: int, status: str = None,
                    task_id: str = None, retry_count: int = None, failure_reason: str = None):
        updates = {}
        if status:
            updates["status"] = status
        if task_id:
            updates["task_id"] = task_id
        if retry_count is not None:
            updates["retry_count"] = retry_count
        if failure_reason:
            updates["failure_reason"] = failure_reason
        if status in ("completed", "failed", "skipped"):
            updates["finished_at"] = time.time()
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [plan_id, step_index]
        with self._get_conn() as conn:
            conn.execute(f"UPDATE plan_steps SET {set_clause} WHERE plan_id = ? AND step_index = ?", params)
            conn.commit()
            if status == "completed":
                publish("plan_step_completed", {
                    "plan_id": plan_id,
                    "step_index": step_index,
                    "tool_name": updates.get("tool_name", "unknown")
                }, source="planning_engine")

    def get_pending_plans(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM plans WHERE status IN ('pending', 'in_progress', 'replanning')").fetchall()
            return [dict(r) for r in rows]

    def get_subplans(self, parent_plan_id: str) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM plans WHERE parent_plan_id = ?", (parent_plan_id,)).fetchall()
            return [dict(r) for r in rows]

    def log_replan(self, plan_id: str, reason: str, failed_step_index: int, new_steps: List[Dict]) -> str:
        replan_id = hashlib.md5(f"{plan_id}_{time.time()}".encode()).hexdigest()[:12]
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO replan_history (replan_id, plan_id, triggered_at, reason, failed_step_index, new_steps_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (replan_id, plan_id, time.time(), reason, failed_step_index, json.dumps(new_steps)))
            conn.commit()
        return replan_id

    def mark_replan_success(self, replan_id: str, success: int = 1):
        with self._get_conn() as conn:
            conn.execute("UPDATE replan_history SET success = ? WHERE replan_id = ?", (success, replan_id))
            conn.commit()

class ActionPlanner:
    def __init__(self):
        self.db = PlanningDB()
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()
        self.world_model = None
        self.task_manager = None
        self.goal_manager = None

    def set_world_model(self, wm):
        self.world_model = wm

    def set_task_manager(self, tm):
        self.task_manager = tm

    def set_goal_manager(self, gm):
        self.goal_manager = gm

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="planning_engine")
        self._thread.start()
        _log("[PlanningEngine v2] Started with replanning support and Cognitive Bus")

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _log("[PlanningEngine v2] Stopped")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._process_plans()
            except Exception as e:
                _log(f"[PlanningEngine] Error in main loop: {e}")
            self._stop_event.wait(5)

    def _process_plans(self):
        plans = self.db.get_pending_plans()
        for plan in plans:
            if plan["status"] == "pending":
                self._start_plan(plan["plan_id"])
            elif plan["status"] == "in_progress":
                self._advance_plan(plan["plan_id"])
            elif plan["status"] == "replanning":
                pass

    def _start_plan(self, plan_id: str):
        plan = self.db.get_plan(plan_id)
        if not plan:
            return
        _log(f"[PlanningEngine] Starting plan {plan_id} for goal {plan['goal_id']}")
        self.db.update_plan_status(plan_id, "in_progress")
        self._advance_plan(plan_id)

    def _advance_plan(self, plan_id: str):
        plan = self.db.get_plan(plan_id)
        if not plan:
            return
        steps = plan["steps"]
        current_idx = plan["current_step"]
        while current_idx < len(steps):
            step = steps[current_idx]
            if step["status"] == "pending":
                if not self._check_precondition(step, plan):
                    _log(f"[PlanningEngine] Precondition failed for step {current_idx} in plan {plan_id}")
                    self._replan(plan_id, current_idx, reason="precondition_failed", step_info=step)
                    return
                self._execute_step(plan_id, current_idx, step)
                return
            elif step["status"] == "completed":
                current_idx += 1
                continue
            elif step["status"] == "failed":
                _log(f"[PlanningEngine] Step {current_idx} failed, replanning plan {plan_id}")
                self._replan(plan_id, current_idx, reason="step_failed", step_info=step)
                return
            elif step["status"] == "running":
                return
            else:
                current_idx += 1
        self._finalize_plan(plan_id)

    def _check_precondition(self, step: Dict, plan: Dict) -> bool:
        precondition = step.get("precondition")
        if not precondition or precondition == "{}" or not precondition.strip():
            return True
        try:
            pre = json.loads(precondition)
        except Exception:
            return True
        if self.world_model:
            return self.world_model.evaluate_condition(pre)
        return True

    def _check_postcondition(self, step: Dict, plan: Dict, execution_result: Any) -> bool:
        postcondition = step.get("postcondition")
        if not postcondition or postcondition == "{}" or not postcondition.strip():
            return True
        try:
            post = json.loads(postcondition)
        except Exception:
            return True
        if self.world_model:
            return self.world_model.evaluate_condition(post, context={"result": execution_result})
        return True

    def _execute_step(self, plan_id: str, step_idx: int, step: Dict):
        tool_name = step["tool_name"]
        tool_args = json.loads(step["tool_args"]) if step["tool_args"] else {}
        _log(f"[PlanningEngine] Executing step {step_idx}: {tool_name}({tool_args})")

        if not self.task_manager:
            _log("[PlanningEngine] TaskManager not available")
            self.db.update_step(plan_id, step_idx, status="failed", failure_reason="TaskManager missing")
            return

        expected_effects = None
        if self.world_model:
            expected_effects = self.world_model.predict_effects(tool_name, tool_args, context={"plan_id": plan_id})
            with self.db._get_conn() as conn:
                conn.execute("UPDATE plan_steps SET expected_effects = ? WHERE plan_id = ? AND step_index = ?",
                             (json.dumps(expected_effects), plan_id, step_idx))
                conn.commit()

        result = self.task_manager.submit_task_sync(tool_name, tool_args, dialog_id="planning_engine")
        if result and result.get("task_id"):
            task_id = result["task_id"]
            self.db.update_step(plan_id, step_idx, task_id=task_id)
            final_result = self.task_manager.wait_for_task(task_id, timeout=60)
            success = final_result and final_result.get("status") == "completed"
        else:
            success = False
            final_result = result

        post_ok = self._check_postcondition(step, self.db.get_plan(plan_id), final_result)
        if success and post_ok:
            self.db.update_step(plan_id, step_idx, status="completed")
            if self.world_model:
                self.world_model.observe_outcome(tool_name, tool_args, final_result, expected_effects)
        else:
            reason = "postcondition_failed" if not post_ok else "execution_failed"
            self.db.update_step(plan_id, step_idx, status="failed", failure_reason=reason)

    def _replan(self, plan_id: str, failed_step_index: int, reason: str, step_info: Dict):
        plan = self.db.get_plan(plan_id)
        if not plan:
            return
        replan_attempts = plan["replan_attempts"] + 1
        if replan_attempts > MAX_REPLAN_ATTEMPTS:
            _log(f"[PlanningEngine] Max replan attempts exceeded for plan {plan_id}")
            self.db.update_plan_status(plan_id, "failed", evaluation_metrics={"error": "max replan attempts"})
            if self.goal_manager:
                self.goal_manager.update_goal(plan["goal_id"], status="failed")
            return

        _log(f"[PlanningEngine] Replanning plan {plan_id} due to {reason}, attempt {replan_attempts}")
        self.db.update_plan_status(plan_id, "replanning")
        self.db.increment_replan_attempts(plan_id)

        context = {
            "failed_step_index": failed_step_index,
            "failed_step": step_info,
            "reason": reason,
            "remaining_steps": plan["steps"][failed_step_index+1:],
            "goal_id": plan["goal_id"],
            "previous_attempts": replan_attempts
        }

        new_steps = self._generate_alternative_steps(plan, context)
        if not new_steps:
            _log(f"[PlanningEngine] Could not generate alternative steps for plan {plan_id}")
            self.db.update_plan_status(plan_id, "failed")
            return

        steps_json = json.loads(plan["steps_json"])
        steps_json = steps_json[:failed_step_index] + new_steps
        with self.db._get_conn() as conn:
            conn.execute("UPDATE plans SET steps_json = ?, current_step = ?, status = 'in_progress', updated_at = ? WHERE plan_id = ?",
                         (json.dumps(steps_json), failed_step_index, time.time(), plan_id))
            conn.execute("DELETE FROM plan_steps WHERE plan_id = ? AND step_index >= ?", (plan_id, failed_step_index))
            for idx, step in enumerate(new_steps):
                new_idx = failed_step_index + idx
                conn.execute("""
                    INSERT INTO plan_steps
                    (plan_id, step_index, tool_name, tool_args, precondition, postcondition, expected_effects, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """, (plan_id, new_idx, step["tool_name"],
                      json.dumps(step.get("args", {})),
                      json.dumps(step.get("precondition", {})),
                      json.dumps(step.get("postcondition", {})),
                      json.dumps(step.get("expected_effects", {}))))
            conn.commit()

        self.db.log_replan(plan_id, reason, failed_step_index, new_steps)
        _log(f"[PlanningEngine] Replan completed for {plan_id}, new steps: {new_steps}")

    def _generate_alternative_steps(self, plan: Dict, context: Dict) -> List[Dict]:
        failed_step = context["failed_step"]
        tool_name = failed_step["tool_name"]
        args = json.loads(failed_step["tool_args"]) if failed_step["tool_args"] else {}
        reason = context["reason"]

        alternatives = {
            "read_file": ["fs_read_file", "mempalace_search"],
            "write_file": ["fs_write_file", "append_to_file"],
            "search_files": ["grep_search", "find_file"],
            "run_shell": ["run_python", "run_powershell"],
        }
        alt_tools = alternatives.get(tool_name, [])
        if alt_tools:
            new_step = failed_step.copy()
            new_step["tool_name"] = alt_tools[0]
            new_step["args"] = args
            return [new_step]

        retry_count = failed_step.get("retry_count", 0) + 1
        if retry_count <= 2:
            new_step = failed_step.copy()
            new_step["retry_count"] = retry_count
            return [new_step]

        return []

    def _finalize_plan(self, plan_id: str):
        plan = self.db.get_plan(plan_id)
        if not plan:
            return
        metrics = self._evaluate_plan(plan)
        self.db.update_plan_status(plan_id, "completed", evaluation_metrics=metrics)
        if self.goal_manager:
            self.goal_manager.update_goal(plan["goal_id"], status="completed", completed_at=time.time())
        _log(f"[PlanningEngine] Plan {plan_id} completed with metrics: {metrics}")

    def _evaluate_plan(self, plan: Dict) -> Dict:
        steps = plan["steps"]
        total = len(steps)
        completed = sum(1 for s in steps if s["status"] == "completed")
        failed = sum(1 for s in steps if s["status"] == "failed")
        skipped = sum(1 for s in steps if s["status"] == "skipped")
        success_rate = completed / total if total > 0 else 0
        return {
            "total_steps": total,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "success_rate": success_rate,
            "replan_attempts": plan["replan_attempts"]
        }

    def create_plan_for_goal(self, goal_id: str, parent_plan_id: str = None) -> Optional[str]:
        if not self.goal_manager:
            _log("[PlanningEngine] GoalManager not available")
            return None
        goal = self.goal_manager.get_goal(goal_id)
        if not goal:
            return None
        goal_type = goal["goal_type"]
        metadata = json.loads(goal["metadata"]) if goal["metadata"] else {}
        context = json.loads(goal["context_json"]) if goal.get("context_json") else {}
        steps = []
        if goal_type == "atomic":
            tool_name = metadata.get("tool_name")
            tool_args = metadata.get("tool_args", {})
            precondition = metadata.get("precondition", {})
            postcondition = metadata.get("postcondition", {})
            if tool_name:
                steps.append({
                    "tool_name": tool_name,
                    "args": tool_args,
                    "precondition": precondition,
                    "postcondition": postcondition
                })
        elif goal_type == "composite":
            subgoals = self.goal_manager.get_subgoals(goal_id)
            for sg in subgoals:
                sg_meta = json.loads(sg["metadata"]) if sg["metadata"] else {}
                sg_tool = sg_meta.get("tool_name")
                sg_args = sg_meta.get("tool_args", {})
                sg_pre = sg_meta.get("precondition", {})
                sg_post = sg_meta.get("postcondition", {})
                if sg_tool:
                    steps.append({
                        "tool_name": sg_tool,
                        "args": sg_args,
                        "precondition": sg_pre,
                        "postcondition": sg_post
                    })
        else:
            return None

        if not steps:
            _log(f"[PlanningEngine] No steps generated for goal {goal_id}")
            return None

        plan_id = hashlib.md5(f"{goal_id}_{time.time()}".encode()).hexdigest()[:12]
        hierarchy_level = 0
        if parent_plan_id:
            parent = self.db.get_plan(parent_plan_id)
            hierarchy_level = (parent["hierarchy_level"] + 1) if parent else 0
        ok = self.db.create_plan(plan_id, goal_id, f"Plan for {goal['title']}", steps,
                                  parent_plan_id, hierarchy_level)
        if ok:
            _log(f"[PlanningEngine] Created plan {plan_id} for goal {goal_id}")
            return plan_id
        return None

    def replan_manually(self, plan_id: str, failed_step_index: int, reason: str) -> Dict:
        plan = self.db.get_plan(plan_id)
        if not plan:
            return {"status": "error", "message": "Plan not found"}
        failed_step = plan["steps"][failed_step_index] if failed_step_index < len(plan["steps"]) else None
        if not failed_step:
            return {"status": "error", "message": "Invalid step index"}
        self._replan(plan_id, failed_step_index, reason, failed_step)
        return {"status": "replanning_started", "plan_id": plan_id}

_planner = ActionPlanner()
_planner.start()

def planning_create_plan(goal_id: str) -> Dict:
    plan_id = _planner.create_plan_for_goal(goal_id)
    if plan_id:
        return {"status": "success", "plan_id": plan_id, "goal_id": goal_id}
    else:
        return {"status": "error", "message": "Could not create plan for goal"}

def planning_get_plan(plan_id: str) -> Dict:
    plan = _planner.db.get_plan(plan_id)
    if plan:
        return {"status": "success", "plan": plan}
    else:
        return {"status": "error", "message": "Plan not found"}

def planning_list_plans(status: str = None) -> Dict:
    plans = _planner.db.get_pending_plans()
    if status:
        plans = [p for p in plans if p["status"] == status]
    return {"status": "success", "plans": plans, "count": len(plans)}

def planning_abort_plan(plan_id: str) -> Dict:
    plan = _planner.db.get_plan(plan_id)
    if not plan:
        return {"status": "error", "message": "Plan not found"}
    _planner.db.update_plan_status(plan_id, "cancelled")
    return {"status": "cancelled", "plan_id": plan_id}

def planning_replan(plan_id: str, failed_step_index: int, reason: str) -> Dict:
    return _planner.replan_manually(plan_id, failed_step_index, reason)

server = BaseMCPServer("planning-engine", "2.1")

server.register_tool("planning_create_plan", {
    "description": "Создать план для достижения цели (по её ID)",
    "inputSchema": {"type": "object", "properties": {"goal_id": {"type": "string"}}, "required": ["goal_id"]}
}, lambda **kw: planning_create_plan(kw["goal_id"]))

server.register_tool("planning_get_plan", {
    "description": "Получить детали плана по ID",
    "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string"}}, "required": ["plan_id"]}
}, lambda **kw: planning_get_plan(kw["plan_id"]))

server.register_tool("planning_list_plans", {
    "description": "Список активных планов",
    "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}
}, lambda **kw: planning_list_plans(kw.get("status")))

server.register_tool("planning_abort_plan", {
    "description": "Отменить выполнение плана",
    "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string"}}, "required": ["plan_id"]}
}, lambda **kw: planning_abort_plan(kw["plan_id"]))

server.register_tool("planning_replan", {
    "description": "Запустить перепланирование с указанного шага",
    "inputSchema": {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "failed_step_index": {"type": "integer"},
            "reason": {"type": "string"}
        },
        "required": ["plan_id", "failed_step_index"]
    }
}, lambda **kw: planning_replan(kw["plan_id"], kw["failed_step_index"], kw.get("reason", "manual")))

__mcp_plugin__ = {
    "name": "planning-engine",
    "version": "2.1",
    "description": "Планировщик с replanning, предусловиями/постусловиями, иерархией, событиями Cognitive Bus",
    "dependencies": [],
    "on_load": lambda: _log("[PlanningEngine v2.1] loaded with Cognitive Bus integration"),
    "on_unload": lambda: _planner.stop()
}

if __name__ == "__main__":
    server.run()