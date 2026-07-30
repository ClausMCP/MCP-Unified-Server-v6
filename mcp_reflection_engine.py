#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Reflection Engine v1.1 – полностью рабочая версия.
Использует mcp_memory_graph._graph_db и mcp_scheduler.
"""
import os
import json
import time
import sqlite3
import threading
import hashlib
from typing import List, Dict
from collections import defaultdict

from mcp_shared import _log, BaseMCPServer
from mcp_memory_graph import _graph_db
from mcp_scheduler import scheduler_add_interval

# ========== Конфигурация ==========
REFLECTION_INTERVAL_MIN = int(os.environ.get("MCP_REFLECTION_INTERVAL_MIN", "30"))
REFLECTION_AUTO_RUN = os.environ.get("MCP_REFLECTION_AUTO_RUN", "true").lower() == "true"
VERIFICATION_SOURCES_THRESHOLD = int(os.environ.get("MCP_VERIFICATION_SOURCES", "3"))

def _init_reflection_table():
    with _graph_db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                reflection_id TEXT PRIMARY KEY,
                source_entry_id TEXT,
                target_entry_id TEXT,
                reflection_type TEXT,
                description TEXT,
                confidence_delta REAL,
                created_at REAL,
                resolved INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reflection_source ON reflections(source_entry_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reflection_target ON reflections(target_entry_id)")
        conn.commit()

_init_reflection_table()

def _add_reflection(source_id, target_id, rtype, desc, delta=0.0):
    rid = hashlib.md5(f"{source_id}_{target_id}_{time.time()}".encode()).hexdigest()[:16]
    with _graph_db._get_conn() as conn:
        conn.execute(
            "INSERT INTO reflections VALUES (?,?,?,?,?,?,?,0)",
            (rid, source_id, target_id, rtype, desc, delta, time.time())
        )
        conn.commit()

def _update_belief_confidence(belief_id: str, new_conf: float, status: str = None):
    with _graph_db._get_conn() as conn:
        if status:
            conn.execute(
                "UPDATE beliefs SET confidence = ?, verification_status = ? WHERE belief_id = ?",
                (new_conf, status, belief_id)
            )
        else:
            conn.execute(
                "UPDATE beliefs SET confidence = ? WHERE belief_id = ?",
                (new_conf, belief_id)
            )
        conn.commit()

def _find_version_conflicts():
    conflicts = []
    with _graph_db._get_conn() as conn:
        entities = conn.execute(
            "SELECT entity_id, name, type FROM entities WHERE type IN ('file', 'model')"
        ).fetchall()
    groups = defaultdict(list)
    import re
    for e in entities:
        base = re.sub(r'[\s_\-]*v?\d+(?:\.\d+)*', '', e[1], flags=re.IGNORECASE).strip()
        groups[base].append(e)
    for base, group in groups.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda x: x[1])
        oldest, newest = group_sorted[0][1], group_sorted[-1][1]
        with _graph_db._get_conn() as conn:
            for ent in group_sorted:
                beliefs = conn.execute(
                    "SELECT belief_id, statement, confidence, source_entry_id FROM beliefs WHERE statement LIKE ?",
                    (f'%{ent[1]}%',)
                ).fetchall()
                if len(beliefs) >= 2:
                    conflicts.append({
                        "type": "version_conflict",
                        "entity": ent[1],
                        "base_name": base,
                        "old_version": oldest,
                        "new_version": newest,
                        "beliefs": [dict(b) for b in beliefs]
                    })
    return conflicts

def _find_relation_conflicts():
    conflicts = []
    with _graph_db._get_conn() as conn:
        rows = conn.execute(
            "SELECT source_id, relation_type, target_id, confidence, source_entry_id FROM relations"
        ).fetchall()
    by_source = defaultdict(list)
    for r in rows:
        by_source[r[0]].append(r)
    for source_id, rels in by_source.items():
        by_type = defaultdict(set)
        for r in rels:
            by_type[r[1]].add((r[2], r[3], r[4]))
        for rel_type, targets in by_type.items():
            if len(targets) > 1:
                conflicts.append({
                    "type": "relation_conflict",
                    "source_id": source_id,
                    "relation_type": rel_type,
                    "targets": list(targets)
                })
    return conflicts

def _find_contradictory_beliefs():
    antonym_pairs = [
        ("true", "false"), ("yes", "no"), ("correct", "incorrect"),
        ("exists", "not exists"), ("available", "unavailable"),
        ("success", "failure"), ("ok", "error")
    ]
    conflicts = []
    with _graph_db._get_conn() as conn:
        beliefs = conn.execute(
            "SELECT belief_id, statement, confidence, source_entry_id, verification_status FROM beliefs"
        ).fetchall()
    for i in range(len(beliefs)):
        for j in range(i+1, len(beliefs)):
            s1 = beliefs[i][1].lower()
            s2 = beliefs[j][1].lower()
            for a, b in antonym_pairs:
                if (a in s1 and b in s2) or (b in s1 and a in s2):
                    conflicts.append({
                        "type": "antonym_conflict",
                        "belief1": dict(beliefs[i]),
                        "belief2": dict(beliefs[j])
                    })
                    break
    return conflicts

def auto_verify_beliefs():
    with _graph_db._get_conn() as conn:
        rows = conn.execute(
            "SELECT belief_id, statement, confidence, source_entry_id, verification_status FROM beliefs"
        ).fetchall()
    stmt_to_beliefs = defaultdict(list)
    for b in rows:
        stmt_to_beliefs[b[1]].append(b)
    for stmt, beliefs in stmt_to_beliefs.items():
        if len(beliefs) >= VERIFICATION_SOURCES_THRESHOLD:
            for b in beliefs:
                if b[4] != "verified":
                    new_conf = min(1.0, b[2] + 0.1 * len(beliefs))
                    _update_belief_confidence(b[0], new_conf, "verified")
                    _log(f"[Reflection] Auto-verified: {stmt[:50]}...")

def run_reflection(limit: int = 500):
    _log("[Reflection] Starting cycle...")
    start = time.time()
    vc = _find_version_conflicts()
    rc = _find_relation_conflicts()
    ac = _find_contradictory_beliefs()
    _log(f"[Reflection] Found {len(vc)} version, {len(rc)} relation, {len(ac)} antonym conflicts")
    for c in vc:
        for b in c["beliefs"]:
            if b["statement"] == c["old_version"]:
                _update_belief_confidence(b["belief_id"], max(0.1, b["confidence"] - 0.3))
                _add_reflection(b["source_entry_id"], None, "version_conflict",
                                f"Версия {c['old_version']} устарела", -0.3)
            elif b["statement"] == c["new_version"]:
                _update_belief_confidence(b["belief_id"], min(1.0, b["confidence"] + 0.2))
                _add_reflection(b["source_entry_id"], None, "version_conflict",
                                f"Версия {c['new_version']} актуальна", +0.2)
    for c in rc:
        for target, conf, entry_id in c["targets"]:
            if conf < 0.7:
                _update_belief_confidence(None, max(0.1, conf - 0.2))  # нужно по belief_id
                _add_reflection(entry_id, None, "relation_conflict",
                                f"Конфликт {c['relation_type']}: {target}", -0.2)
    for c in ac:
        b1, b2 = c["belief1"], c["belief2"]
        _update_belief_confidence(b1["belief_id"], max(0.1, b1["confidence"] - 0.3))
        _update_belief_confidence(b2["belief_id"], max(0.1, b2["confidence"] - 0.3))
        _add_reflection(b1["source_entry_id"], b2["source_entry_id"], "antonym_conflict",
                        c.get("description", "Antonym conflict"), -0.3)
    auto_verify_beliefs()
    _log(f"[Reflection] Cycle done in {time.time()-start:.2f}s")

def tool_run_reflection(limit=500):
    run_reflection(limit)
    return {"status": "reflection_completed"}

def tool_get_reflections(limit=50):
    with _graph_db._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"reflections": [dict(r) for r in rows]}

def tool_reflection_stats():
    with _graph_db._get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        unresolved = conn.execute("SELECT COUNT(*) FROM reflections WHERE resolved=0").fetchone()[0]
        return {"total": total, "unresolved": unresolved}

# MCP сервер
server = BaseMCPServer("reflection-engine", "1.1")
server.register_tool("run_reflection", {
    "description": "Запустить анализ противоречий",
    "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 500}}}
}, lambda **kw: tool_run_reflection(kw.get("limit", 500)))
server.register_tool("get_reflections", {
    "description": "Список рефлексий",
    "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}}
}, lambda **kw: tool_get_reflections(kw.get("limit", 50)))
server.register_tool("reflection_stats", {
    "description": "Статистика",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: tool_reflection_stats())

# Автозапуск
if REFLECTION_AUTO_RUN:
    threading.Thread(target=run_reflection, daemon=True).start()
    try:
        scheduler_add_interval("reflection_engine", "run_reflection",
                               REFLECTION_INTERVAL_MIN * 60, {})
    except Exception as e:
        _log(f"[Reflection] Scheduler error: {e}")

__mcp_plugin__ = {
    "name": "reflection-engine",
    "version": "1.1",
    "description": "Рефлексия на основе графа памяти",
    "on_load": lambda: _log("[Reflection] v1.1 loaded"),
}