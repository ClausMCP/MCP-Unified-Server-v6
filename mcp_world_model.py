#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP World Model v2.2 – с forward/backward chaining, подпиской на события,
интеграцией с Cognitive Bus и полными асинхронными обёртками.

КОНТРАКТ ВЫЗОВА (важно для интеграций):
  Модуль экспортирует КАЖДУЮ операцию в двух вариантах:
    • world_<op>        — АСИНХРОННЫЙ (coroutine), для async-кода: `await world_<op>(...)`
    • world_<op>_sync   — СИНХРОННЫЙ, для обычного кода: `world_<op>_sync(...)`
  Правило: из синхронного кода вызывайте ТОЛЬКО *_sync; из async-кода — async-версии.
  Вызов async-версии без await вернёт корутину и НЕ выполнит операцию.
  (Так выглядели прежние баги: cognitive_core/coordinator звали async-функции
  синхронно; имена forward_chaining/query_facts/get_state/predict_effects не
  существуют — правильные world_run_inference / world_get_predictions /
  world_list_rules / world_simulate_action / world_backward_chain.)
"""
import os
import json
import sqlite3
import hashlib
import time
import threading
import asyncio
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple, Union
from contextlib import contextmanager
from collections import defaultdict

from mcp_shared import _log, BaseMCPServer, dialog_ctx

try:
    from mcp_cognitive_bus import publish, subscribe
    COGNITIVE_BUS_AVAILABLE = True
except ImportError:
    COGNITIVE_BUS_AVAILABLE = False
    def publish(*args, **kwargs): pass
    def subscribe(*args, **kwargs): pass

WORLD_MODEL_DB = os.environ.get("MCP_WORLD_MODEL_DB", os.path.join(os.path.dirname(__file__), "world_model.db"))
LEARNING_RATE = float(os.environ.get("MCP_WORLD_MODEL_LEARNING_RATE", "0.1"))

# ============================================================================
# Синхронная БД и основная логика (без изменений, но вынесены в отдельные функции)
# ============================================================================

class WorldModelDB:
    def __init__(self, db_path: str = WORLD_MODEL_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rules (
                    rule_id TEXT PRIMARY KEY,
                    condition TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    confidence REAL DEFAULT 0.7,
                    times_used INTEGER DEFAULT 0,
                    times_succeeded INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_used_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS action_effects (
                    effect_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    args_pattern TEXT,
                    effect_type TEXT,
                    effect_target TEXT,
                    confidence REAL DEFAULT 0.5,
                    times_observed INTEGER DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    prediction_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    source_rule_id TEXT,
                    source_effect_id TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    status TEXT DEFAULT 'active'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_action_effects_tool ON action_effects(tool_name)")
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

    def add_rule(self, rule_id: str, condition: Dict, conclusion: str, confidence: float = 0.7) -> bool:
        now = time.time()
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO rules (rule_id, condition, conclusion, confidence, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (rule_id, json.dumps(condition), conclusion, confidence, now))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_rules(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM rules ORDER BY confidence DESC").fetchall()
            return [dict(r) for r in rows]

    def update_rule_feedback(self, rule_id: str, success: bool):
        with self._get_conn() as conn:
            if success:
                conn.execute("UPDATE rules SET times_used = times_used + 1, times_succeeded = times_succeeded + 1, confidence = MIN(1.0, confidence + ?) WHERE rule_id = ?",
                             (LEARNING_RATE, rule_id))
            else:
                conn.execute("UPDATE rules SET times_used = times_used + 1, confidence = MAX(0.0, confidence - ?) WHERE rule_id = ?",
                             (LEARNING_RATE, rule_id))
            conn.execute("UPDATE rules SET last_used_at = ? WHERE rule_id = ?", (time.time(), rule_id))
            conn.commit()

    def delete_rule(self, rule_id: str):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM rules WHERE rule_id = ?", (rule_id,))
            conn.commit()

    def add_action_effect(self, tool_name: str, args_pattern: Dict, effect_type: str, effect_target: Dict, confidence: float = 0.5) -> str:
        effect_id = hashlib.md5(f"{tool_name}_{json.dumps(args_pattern)}_{effect_type}".encode()).hexdigest()[:12]
        now = time.time()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO action_effects
                (effect_id, tool_name, args_pattern, effect_type, effect_target, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (effect_id, tool_name, json.dumps(args_pattern), effect_type, json.dumps(effect_target), confidence, now))
            conn.commit()
        return effect_id

    def get_action_effects(self, tool_name: str, args: Dict) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM action_effects WHERE tool_name = ?", (tool_name,)).fetchall()
            results = []
            for row in rows:
                pattern = json.loads(row["args_pattern"])
                match = True
                for k, v in pattern.items():
                    if args.get(k) != v:
                        match = False
                        break
                if match:
                    results.append(dict(row))
            return results

    def update_effect_feedback(self, effect_id: str, success: bool):
        with self._get_conn() as conn:
            if success:
                conn.execute("UPDATE action_effects SET times_observed = times_observed + 1, confidence = MIN(1.0, confidence + ?) WHERE effect_id = ?",
                             (LEARNING_RATE, effect_id))
            else:
                conn.execute("UPDATE action_effects SET times_observed = times_observed + 1, confidence = MAX(0.0, confidence - ?) WHERE effect_id = ?",
                             (LEARNING_RATE, effect_id))
            conn.commit()

    def add_prediction(self, statement: str, confidence: float, source_rule_id: str = None,
                       source_effect_id: str = None, ttl_seconds: int = 3600) -> str:
        pred_id = hashlib.md5(f"{statement}_{time.time()}".encode()).hexdigest()[:12]
        now = time.time()
        expires = now + ttl_seconds
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO predictions (prediction_id, statement, confidence, source_rule_id, source_effect_id, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
            """, (pred_id, statement, confidence, source_rule_id, source_effect_id, now, expires))
            conn.commit()
        return pred_id

    def get_active_predictions(self, limit: int = 50) -> List[Dict]:
        now = time.time()
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM predictions
                WHERE status = 'active' AND expires_at > ?
                ORDER BY confidence DESC, created_at DESC LIMIT ?
            """, (now, limit)).fetchall()
            return [dict(r) for r in rows]

    def mark_prediction_outdated(self, prediction_id: str):
        with self._get_conn() as conn:
            conn.execute("UPDATE predictions SET status = 'outdated' WHERE prediction_id = ?", (prediction_id,))
            conn.commit()

    def confirm_prediction(self, prediction_id: str):
        with self._get_conn() as conn:
            conn.execute("UPDATE predictions SET status = 'confirmed' WHERE prediction_id = ?", (prediction_id,))
            conn.commit()

    def fact_exists(self, statement: str) -> bool:
        try:
            from mcp_memory_graph import _graph_db
            with _graph_db._get_conn() as conn:
                row = conn.execute("SELECT 1 FROM facts WHERE statement = ?", (statement,)).fetchone()
                return row is not None
        except Exception:
            return False

# ============================================================================
# Синхронный класс WorldModel (основная логика)
# ============================================================================

class WorldModel:
    def __init__(self):
        self.db = WorldModelDB()
        self._graph_available = False
        self.graph_db = None
        try:
            from mcp_memory_graph import _graph_db
            self.graph_db = _graph_db
            self._graph_available = True
        except ImportError:
            _log("[WorldModel] Memory graph not available")

    def evaluate_condition(self, condition: Dict, context: Dict = None) -> bool:
        if not self._graph_available:
            return False
        cond_type = condition.get("type")
        if cond_type == "fact":
            statement = condition.get("statement", "")
            with self.graph_db._get_conn() as conn:
                row = conn.execute(
                    "SELECT confidence FROM facts WHERE statement LIKE ? AND confidence > 0.8 LIMIT 1",
                    (f"%{statement}%",)
                ).fetchone()
                return row is not None
        elif cond_type == "entity_exists":
            entity_name = condition.get("name", "")
            with self.graph_db._get_conn() as conn:
                row = conn.execute("SELECT 1 FROM entities WHERE name = ?", (entity_name,)).fetchone()
                return row is not None
        elif cond_type == "relation_exists":
            source = condition.get("source")
            rel = condition.get("relation")
            target = condition.get("target")
            if not all([source, rel, target]):
                return False
            with self.graph_db._get_conn() as conn:
                row = conn.execute("""
                    SELECT 1 FROM relations r
                    JOIN entities e1 ON r.source_id = e1.entity_id
                    JOIN entities e2 ON r.target_id = e2.entity_id
                    WHERE e1.name = ? AND r.relation_type = ? AND e2.name = ?
                """, (source, rel, target)).fetchone()
                return row is not None
        elif cond_type == "result_check" and context:
            result = context.get("result")
            expected = condition.get("expected")
            if expected and result:
                return expected in str(result)
        return False

    def add_fact(self, statement: str, confidence: float, source_tool: str = None) -> str:
        if not self._graph_available:
            _log("[WorldModel] Cannot add fact: graph unavailable")
            return ""
        fact_id = self.graph_db.add_fact(statement, confidence, source_tool=source_tool)
        publish("fact_added", {"statement": statement, "confidence": confidence, "fact_id": fact_id}, source="world_model")
        return fact_id

    def run_forward_chaining(self, max_iterations: int = 10) -> List[Dict]:
        new_facts = []
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            added_in_this_cycle = 0
            rules = self.db.get_rules()
            for rule in rules:
                condition = json.loads(rule["condition"])
                if self.evaluate_condition(condition):
                    conclusion = rule["conclusion"]
                    if not self.db.fact_exists(conclusion):
                        fact_id = self.add_fact(conclusion, confidence=rule["confidence"], source_tool="forward_chaining")
                        new_facts.append({
                            "statement": conclusion,
                            "confidence": rule["confidence"],
                            "source_rule": rule["rule_id"],
                            "fact_id": fact_id
                        })
                        added_in_this_cycle += 1
            if added_in_this_cycle == 0:
                break
        if new_facts:
            _log(f"[WorldModel] Forward chaining produced {len(new_facts)} new facts")
        return new_facts

    def backward_chaining(self, goal_statement: str, max_depth: int = 5) -> List[Dict]:
        def _extract_subgoals_from_condition(cond):
            subgoals = []
            if isinstance(cond, dict):
                if cond.get("type") == "all":
                    for sub in cond.get("conditions", []):
                        subgoals.extend(_extract_subgoals_from_condition(sub))
                elif cond.get("type") == "fact":
                    subgoals.append(cond.get("statement", ""))
                elif cond.get("type") == "entity_exists":
                    subgoals.append(f"entity:{cond.get('name')}")
                elif cond.get("type") == "relation_exists":
                    subgoals.append(f"relation:{cond.get('source')}|{cond.get('relation')}|{cond.get('target')}")
            return subgoals

        def _backward(goal, depth, visited):
            if depth > max_depth or goal in visited:
                return None
            visited.add(goal)
            rules = self.db.get_rules()
            for rule in rules:
                if rule["conclusion"] == goal:
                    condition = json.loads(rule["condition"])
                    subgoals = _extract_subgoals_from_condition(condition)
                    if not subgoals:
                        if self.evaluate_condition(condition):
                            return [{"rule_id": rule["rule_id"], "goal": goal, "confidence": rule["confidence"]}]
                    else:
                        chain = []
                        all_ok = True
                        for sub in subgoals:
                            sub_proof = _backward(sub, depth+1, visited.copy())
                            if sub_proof is None:
                                all_ok = False
                                break
                            chain.extend(sub_proof)
                        if all_ok:
                            chain.append({"rule_id": rule["rule_id"], "goal": goal, "confidence": rule["confidence"]})
                            return chain
            return None

        result = _backward(goal_statement, 0, set())
        return result if result else []

    def predict_effects(self, tool_name: str, args: Dict, context: Dict = None) -> List[Dict]:
        effects = self.db.get_action_effects(tool_name, args)
        predictions = []
        for eff in effects:
            effect_type = eff["effect_type"]
            effect_target = json.loads(eff["effect_target"])
            confidence = eff["confidence"]
            statement = f"After {tool_name} with {args}, {effect_type}: {effect_target}"
            pred_id = self.db.add_prediction(
                statement=statement,
                confidence=confidence,
                source_effect_id=eff["effect_id"],
                ttl_seconds=600
            )
            predictions.append({
                "prediction_id": pred_id,
                "type": effect_type,
                "target": effect_target,
                "confidence": confidence
            })
        if tool_name == "write_file" and self._graph_available:
            path = args.get("file_path") or args.get("path")
            if path:
                predictions.append({
                    "type": "add_entity",
                    "target": {"name": path, "type": "file"},
                    "confidence": 0.9,
                    "builtin": True
                })
        return predictions

    def observe_outcome(self, tool_name: str, args: Dict, outcome: Any, expected_effects: List[Dict]):
        for effect in expected_effects:
            effect_type = effect.get("type")
            target = effect.get("target")
            confidence = effect.get("confidence")
            pred_id = effect.get("prediction_id")
            success = False
            if effect_type == "add_fact":
                fact_statement = target.get("statement")
                if self._graph_available and fact_statement:
                    with self.graph_db._get_conn() as conn:
                        row = conn.execute("SELECT 1 FROM facts WHERE statement LIKE ?", (f"%{fact_statement}%",)).fetchone()
                        success = row is not None
            elif effect_type == "add_entity":
                entity_name = target.get("name")
                if self._graph_available and entity_name:
                    with self.graph_db._get_conn() as conn:
                        row = conn.execute("SELECT 1 FROM entities WHERE name = ?", (entity_name,)).fetchone()
                        success = row is not None
            if pred_id:
                with self.db._get_conn() as conn:
                    row = conn.execute("SELECT source_effect_id FROM predictions WHERE prediction_id = ?", (pred_id,)).fetchone()
                    if row and row["source_effect_id"]:
                        self.db.update_effect_feedback(row["source_effect_id"], success)
                if success:
                    self.db.confirm_prediction(pred_id)
                else:
                    self.db.mark_prediction_outdated(pred_id)
            elif effect.get("builtin"):
                pattern = {k: args.get(k) for k in ["file_path", "path"] if args.get(k)}
                if pattern:
                    effect_target = {"name": target.get("name"), "type": target.get("type")}
                    self.db.add_action_effect(tool_name, pattern, "add_entity", effect_target, confidence=(0.9 if success else 0.1))

    def get_predictions(self, limit: int = 50) -> List[Dict]:
        return self.db.get_active_predictions(limit)

    def add_builtin_rules(self):
        self.db.add_rule(
            "transitive_located",
            {"type": "relation_exists", "source": "?X", "relation": "located_at", "target": "?Y"},
            "If ?X located_at ?Y and ?Y located_at ?Z then ?X located_at ?Z",
            confidence=0.8
        )
        self.db.add_rule(
            "symmetric_connected",
            {"type": "relation_exists", "source": "?A", "relation": "connected_to", "target": "?B"},
            "?B connected_to ?A",
            confidence=0.9
        )

# ============================================================================
# Глобальный экземпляр и синхронные публичные функции
# ============================================================================

_world_model = WorldModel()
_world_model.add_builtin_rules()

def _subscribe_to_events():
    if not COGNITIVE_BUS_AVAILABLE:
        return
    try:
        subscribe("fact_added", lambda data: _world_model.run_forward_chaining())
        subscribe("rule_added", lambda data: _world_model.run_forward_chaining())
        _log("[WorldModel] Subscribed to cognitive events")
    except Exception as e:
        _log(f"[WorldModel] Event subscription failed: {e}")

_subscribe_to_events()

# ----- Синхронные обёртки (используются внутри MCP сервера и асинхронных функций) -----
def world_add_rule_sync(condition: Dict, conclusion: str, confidence: float = 0.7, rule_id: str = None) -> Dict:
    if not rule_id:
        rule_id = hashlib.md5(f"{json.dumps(condition)}_{conclusion}".encode()).hexdigest()[:12]
    ok = _world_model.db.add_rule(rule_id, condition, conclusion, confidence)
    if ok:
        publish("rule_added", {"rule_id": rule_id, "condition": condition, "conclusion": conclusion}, source="world_model")
        return {"status": "success", "rule_id": rule_id}
    else:
        return {"status": "error", "message": "Rule ID already exists"}

def world_list_rules_sync() -> Dict:
    rules = _world_model.db.get_rules()
    return {"status": "success", "rules": rules, "count": len(rules)}

def world_delete_rule_sync(rule_id: str) -> Dict:
    _world_model.db.delete_rule(rule_id)
    return {"status": "deleted", "rule_id": rule_id}

def world_run_inference_sync() -> Dict:
    predictions = _world_model.run_forward_chaining()
    return {"status": "success", "new_facts": predictions, "count": len(predictions)}

def world_get_predictions_sync(limit: int = 50) -> Dict:
    preds = _world_model.get_predictions(limit)
    return {"status": "success", "predictions": preds, "count": len(preds)}

def world_add_action_effect_sync(tool_name: str, args_pattern: Dict, effect_type: str, effect_target: Dict, confidence: float = 0.5) -> Dict:
    effect_id = _world_model.db.add_action_effect(tool_name, args_pattern, effect_type, effect_target, confidence)
    return {"status": "success", "effect_id": effect_id}

def world_simulate_action_sync(tool_name: str, args: Dict) -> Dict:
    effects = _world_model.predict_effects(tool_name, args)
    return {"status": "success", "effects": effects, "count": len(effects)}

def world_backward_chain_sync(goal: str, max_depth: int = 5) -> Dict:
    chain = _world_model.backward_chaining(goal, max_depth)
    return {"status": "success", "goal": goal, "proof_chain": chain, "found": chain is not None}

def world_add_fact_sync(statement: str, confidence: float = 0.9, source_tool: str = None) -> Dict:
    fact_id = _world_model.add_fact(statement, confidence, source_tool=source_tool or "mcp_tool")
    return {"status": "success", "fact_id": fact_id, "statement": statement}

# ============================================================================
# АСИНХРОННЫЕ ОБЁРТКИ (для использования в плагинах и cognitive_core)
# ============================================================================

async def world_add_rule(condition: Dict, conclusion: str, confidence: float = 0.7, rule_id: str = None) -> Dict:
    """Асинхронная обёртка для world_add_rule_sync"""
    return await asyncio.to_thread(world_add_rule_sync, condition, conclusion, confidence, rule_id)

async def world_list_rules() -> Dict:
    return await asyncio.to_thread(world_list_rules_sync)

async def world_delete_rule(rule_id: str) -> Dict:
    return await asyncio.to_thread(world_delete_rule_sync, rule_id)

async def world_run_inference() -> Dict:
    return await asyncio.to_thread(world_run_inference_sync)

async def world_get_predictions(limit: int = 50) -> Dict:
    return await asyncio.to_thread(world_get_predictions_sync, limit)

async def world_add_action_effect(tool_name: str, args_pattern: Dict, effect_type: str, effect_target: Dict, confidence: float = 0.5) -> Dict:
    return await asyncio.to_thread(world_add_action_effect_sync, tool_name, args_pattern, effect_type, effect_target, confidence)

async def world_simulate_action(tool_name: str, args: Dict) -> Dict:
    return await asyncio.to_thread(world_simulate_action_sync, tool_name, args)

async def world_backward_chain(goal: str, max_depth: int = 5) -> Dict:
    return await asyncio.to_thread(world_backward_chain_sync, goal, max_depth)

async def world_add_fact(statement: str, confidence: float = 0.9, source_tool: str = None) -> Dict:
    return await asyncio.to_thread(world_add_fact_sync, statement, confidence, source_tool)

# ============================================================================
# MCP сервер (синхронные вызовы, но внутри вызывают синхронные функции)
# ============================================================================

server = BaseMCPServer("world-model", "2.2")

server.register_tool("world_add_rule", {
    "description": "Добавить правило вывода: если условие истинно, то делаем вывод",
    "inputSchema": {"type": "object", "properties": {"condition": {"type": "object"}, "conclusion": {"type": "string"}, "confidence": {"type": "number"}}, "required": ["condition", "conclusion"]}
}, lambda **kw: world_add_rule_sync(kw["condition"], kw["conclusion"], kw.get("confidence", 0.7), kw.get("rule_id")))

server.register_tool("world_list_rules", {
    "description": "Список всех правил",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: world_list_rules_sync())

server.register_tool("world_delete_rule", {
    "description": "Удалить правило по ID",
    "inputSchema": {"type": "object", "properties": {"rule_id": {"type": "string"}}, "required": ["rule_id"]}
}, lambda **kw: world_delete_rule_sync(kw["rule_id"]))

server.register_tool("world_run_inference", {
    "description": "Запустить прямой вывод по правилам, сгенерировать новые факты",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: world_run_inference_sync())

server.register_tool("world_get_predictions", {
    "description": "Получить активные предсказания",
    "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}}
}, lambda **kw: world_get_predictions_sync(kw.get("limit", 50)))

server.register_tool("world_add_action_effect", {
    "description": "Добавить ожидаемый эффект от выполнения инструмента (для симуляции)",
    "inputSchema": {"type": "object", "properties": {"tool_name": {"type": "string"}, "args_pattern": {"type": "object"}, "effect_type": {"type": "string"}, "effect_target": {"type": "object"}, "confidence": {"type": "number"}}, "required": ["tool_name", "effect_type", "effect_target"]}
}, lambda **kw: world_add_action_effect_sync(kw["tool_name"], kw.get("args_pattern", {}), kw["effect_type"], kw["effect_target"], kw.get("confidence", 0.5)))

server.register_tool("world_simulate_action", {
    "description": "Симулировать эффекты выполнения действия без фактического выполнения",
    "inputSchema": {"type": "object", "properties": {"tool_name": {"type": "string"}, "args": {"type": "object"}}, "required": ["tool_name"]}
}, lambda **kw: world_simulate_action_sync(kw["tool_name"], kw.get("args", {})))

server.register_tool("world_backward_chain", {
    "description": "Обратный вывод: найти доказательства для цели",
    "inputSchema": {"type": "object", "properties": {"goal": {"type": "string"}, "max_depth": {"type": "integer", "default": 5}}, "required": ["goal"]}
}, lambda **kw: world_backward_chain_sync(kw["goal"], kw.get("max_depth", 5)))

server.register_tool("world_add_fact", {
    "description": "Непосредственно добавить факт в граф знаний",
    "inputSchema": {"type": "object", "properties": {"statement": {"type": "string"}, "confidence": {"type": "number", "default": 0.9}, "source_tool": {"type": "string"}}, "required": ["statement"]}
}, lambda **kw: world_add_fact_sync(kw["statement"], kw.get("confidence", 0.9), kw.get("source_tool")))

__mcp_plugin__ = {
    "name": "world-model",
    "version": "2.2",
    "description": "Модель мира с forward/backward chaining, правилами, предсказаниями и Cognitive Bus, асинхронные обёртки",
    "dependencies": [],
    "on_load": lambda: _log("[WorldModel v2.2] loaded with async wrappers and event integration")
}

if __name__ == "__main__":
    server.run()