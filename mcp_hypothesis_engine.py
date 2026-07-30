#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Hypothesis Engine v1.1 – с жизненным циклом статусов и интеграцией Cognitive Bus.
Статусы: proposed → investigating → supported → verified → (promoted или rejected)
"""
import os
import json
import time
import sqlite3
import threading
import hashlib
from typing import Dict, List, Optional, Any, Tuple, Callable
from datetime import datetime
from contextlib import contextmanager
from collections import defaultdict

from mcp_shared import _log, BaseMCPServer, dialog_ctx
from mcp_cognitive_bus import publish

# ========== Конфигурация ==========
HYPOTHESIS_DB = os.environ.get("MCP_HYPOTHESIS_DB", os.path.join(os.path.dirname(__file__), "hypothesis.db"))
AUTO_VERIFY = os.environ.get("MCP_HYPOTHESIS_AUTO_VERIFY", "true").lower() == "true"
VERIFICATION_TIMEOUT = int(os.environ.get("MCP_HYPOTHESIS_VERIFICATION_TIMEOUT", "300"))
PROMOTION_CONFIDENCE_THRESHOLD = float(os.environ.get("MCP_HYPOTHESIS_PROMOTION_THRESHOLD", "0.85"))
EVIDENCE_REQUIRED = int(os.environ.get("MCP_HYPOTHESIS_EVIDENCE_REQUIRED", "3"))

# Допустимые статусы
HYPOTHESIS_STATUSES = [
    "proposed",      # только создана
    "investigating", # идёт сбор свидетельств
    "supported",     # есть поддержка, но не подтверждена
    "verified",      # подтверждена
    "rejected",      # опровергнута
    "promoted"       # продвинута до факта
]

# ========== База данных гипотез (расширенная) ==========
class HypothesisDB:
    def __init__(self, db_path: str = HYPOTHESIS_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    confidence REAL DEFAULT 0.3,
                    status TEXT DEFAULT 'proposed',
                    explanation TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL,
                    verification_plan TEXT,
                    source_entry_id TEXT,
                    source_dialog_id TEXT,
                    source_tool TEXT,
                    rejection_reason TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_entry_id TEXT,
                    confidence REAL DEFAULT 0.5,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS verification_jobs (
                    job_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    result TEXT,
                    started_at REAL,
                    finished_at REAL,
                    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hyp_status ON hypotheses(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hyp_updated ON hypotheses(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_hyp ON evidence(hypothesis_id)")
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

    def create_hypothesis(self, hypothesis_id: str, statement: str, confidence: float,
                          explanation: str, verification_plan: List[Dict],
                          source_entry_id: str = None, source_dialog_id: str = None,
                          source_tool: str = None, ttl_seconds: int = 86400) -> bool:
        now = time.time()
        expires = now + ttl_seconds
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO hypotheses
                    (hypothesis_id, statement, confidence, status, explanation, created_at, updated_at,
                     expires_at, verification_plan, source_entry_id, source_dialog_id, source_tool)
                    VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)
                """, (hypothesis_id, statement, confidence, explanation, now, now, expires,
                      json.dumps(verification_plan), source_entry_id, source_dialog_id, source_tool))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_hypothesis(self, hypothesis_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id = ?", (hypothesis_id,)).fetchone()
            return dict(row) if row else None

    def update_hypothesis_status(self, hypothesis_id: str, status: str, confidence: float = None,
                                 rejection_reason: str = None):
        if status not in HYPOTHESIS_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        updates = ["status = ?", "updated_at = ?"]
        params = [status, time.time()]
        if confidence is not None:
            updates.append("confidence = ?")
            params.append(confidence)
        if rejection_reason is not None:
            updates.append("rejection_reason = ?")
            params.append(rejection_reason)
        params.append(hypothesis_id)
        with self._get_conn() as conn:
            conn.execute(f"UPDATE hypotheses SET {', '.join(updates)} WHERE hypothesis_id = ?", params)
            conn.commit()
            # Публикация события
            try:
                publish("hypothesis_updated", {
                    "hypothesis_id": hypothesis_id,
                    "status": status,
                    "confidence": confidence
                }, source="hypothesis_engine")
                if status == "verified":
                    publish("hypothesis_verified", {
                        "hypothesis_id": hypothesis_id,
                        "statement": self.get_hypothesis(hypothesis_id)["statement"],
                        "confidence": confidence or 0.5
                    }, source="hypothesis_engine")
                elif status == "rejected":
                    publish("hypothesis_rejected", {
                        "hypothesis_id": hypothesis_id,
                        "reason": rejection_reason
                    }, source="hypothesis_engine")
            except Exception:
                pass

    def add_evidence(self, hypothesis_id: str, statement: str, source: str,
                     source_entry_id: str = None, confidence: float = 0.5) -> str:
        evidence_id = hashlib.md5(f"{hypothesis_id}_{statement}_{time.time()}".encode()).hexdigest()[:12]
        now = time.time()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO evidence (evidence_id, hypothesis_id, statement, source, source_entry_id, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (evidence_id, hypothesis_id, statement, source, source_entry_id, confidence, now))
            conn.commit()
            self._recompute_confidence(hypothesis_id)
        return evidence_id

    def _recompute_confidence(self, hypothesis_id: str):
        with self._get_conn() as conn:
            hyp = conn.execute("SELECT confidence FROM hypotheses WHERE hypothesis_id = ?", (hypothesis_id,)).fetchone()
            if not hyp:
                return
            base_conf = hyp[0]
            ev_list = conn.execute("SELECT confidence FROM evidence WHERE hypothesis_id = ?", (hypothesis_id,)).fetchall()
            if not ev_list:
                return
            ev_confs = [e[0] for e in ev_list]
            total = base_conf + sum(ev_confs)
            new_conf = total / (1 + len(ev_confs))
            new_conf = max(0.0, min(1.0, new_conf))
            conn.execute("UPDATE hypotheses SET confidence = ?, updated_at = ? WHERE hypothesis_id = ?",
                         (new_conf, time.time(), hypothesis_id))
            conn.commit()

    def get_evidence(self, hypothesis_id: str) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM evidence WHERE hypothesis_id = ? ORDER BY created_at DESC", (hypothesis_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_hypotheses_by_status(self, status: str = None, limit: int = 100) -> List[Dict]:
        with self._get_conn() as conn:
            if status:
                rows = conn.execute("SELECT * FROM hypotheses WHERE status = ? ORDER BY confidence DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM hypotheses ORDER BY confidence DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_hypotheses_needing_verification(self, limit: int = 10) -> List[Dict]:
        now = time.time()
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM hypotheses
                WHERE status IN ('proposed', 'investigating', 'supported') AND verification_plan IS NOT NULL
                AND expires_at > ?
                ORDER BY confidence DESC LIMIT ?
            """, (now, limit)).fetchall()
            return [dict(r) for r in rows]

    def create_verification_job(self, hypothesis_id: str) -> str:
        job_id = hashlib.md5(f"{hypothesis_id}_{time.time()}".encode()).hexdigest()[:12]
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO verification_jobs (job_id, hypothesis_id, status, started_at)
                VALUES (?, ?, 'pending', ?)
            """, (job_id, hypothesis_id, time.time()))
            conn.commit()
        return job_id

    def update_verification_job(self, job_id: str, status: str, result: str = None):
        finished = time.time() if status in ('completed', 'failed') else None
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE verification_jobs
                SET status = ?, result = ?, finished_at = ?
                WHERE job_id = ?
            """, (status, result, finished, job_id))
            conn.commit()

    def delete_hypothesis(self, hypothesis_id: str):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM hypotheses WHERE hypothesis_id = ?", (hypothesis_id,))
            conn.commit()

# ========== Движок гипотез (с жизненным циклом) ==========
class HypothesisEngine:
    def __init__(self):
        self.db = HypothesisDB()
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()
        self.memory_graph = None
        self.world_model = None
        self.task_manager = None
        self._verification_handlers = {}

    def set_memory_graph(self, mg):
        self.memory_graph = mg

    def set_world_model(self, wm):
        self.world_model = wm

    def set_task_manager(self, tm):
        self.task_manager = tm

    def register_verification_handler(self, action_type: str, handler: Callable):
        self._verification_handlers[action_type] = handler

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hypothesis_engine")
        self._thread.start()
        _log("[HypothesisEngine] Started")

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _log("[HypothesisEngine] Stopped")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._process_pending_verifications()
                self._auto_promote_hypotheses()
                self._cleanup_expired()
            except Exception as e:
                _log(f"[HypothesisEngine] Error: {e}")
            self._stop_event.wait(10)

    def _process_pending_verifications(self):
        hyps = self.db.get_hypotheses_needing_verification(limit=5)
        for hyp in hyps:
            hyp_id = hyp["hypothesis_id"]
            if hyp["status"] == "proposed":
                self.db.update_hypothesis_status(hyp_id, "investigating")
            self._verify_hypothesis(hyp_id)

    def _verify_hypothesis(self, hypothesis_id: str):
        hyp = self.db.get_hypothesis(hypothesis_id)
        if not hyp or hyp["status"] not in ("proposed", "investigating", "supported"):
            return
        plan = json.loads(hyp["verification_plan"]) if hyp["verification_plan"] else []
        if not plan:
            return

        job_id = self.db.create_verification_job(hypothesis_id)
        _log(f"[HypothesisEngine] Verifying {hypothesis_id}: {hyp['statement'][:80]}...")

        try:
            results = []
            all_success = True
            for step in plan:
                action = step.get("action")
                if action == "check_fact":
                    fact_statement = step.get("statement")
                    if self.memory_graph:
                        with self.memory_graph.graph_db._get_conn() as conn:
                            row = conn.execute(
                                "SELECT confidence FROM facts WHERE statement LIKE ? AND confidence > 0.8 LIMIT 1",
                                (f"%{fact_statement}%",)
                            ).fetchone()
                            success = row is not None
                            results.append({"action": "check_fact", "success": success})
                            if not success:
                                all_success = False
                elif action == "call_tool":
                    tool_name = step.get("tool")
                    tool_args = step.get("args", {})
                    if self.task_manager:
                        result = self.task_manager.submit_task_sync(tool_name, tool_args)
                        success = result and "error" not in result
                        results.append({"action": "call_tool", "tool": tool_name, "success": success})
                        if not success:
                            all_success = False
                else:
                    handler = self._verification_handlers.get(action)
                    if handler:
                        success = handler(step)
                        results.append({"action": action, "success": success})
                        if not success:
                            all_success = False
                    else:
                        _log(f"[HypothesisEngine] Unknown verification action: {action}")

            new_status = "supported" if all_success else "investigating"
            # Если есть достаточно свидетельств и уверенность высока, переводим в verified
            evidence_count = len(self.db.get_evidence(hypothesis_id))
            if all_success and (hyp["confidence"] >= PROMOTION_CONFIDENCE_THRESHOLD or evidence_count >= EVIDENCE_REQUIRED):
                new_status = "verified"
                self.db.update_hypothesis_status(hypothesis_id, new_status, confidence=min(1.0, hyp["confidence"] + 0.2))
                self.db.update_verification_job(job_id, "completed", json.dumps(results))
                _log(f"[HypothesisEngine] Hypothesis {hypothesis_id} VERIFIED")
                # Автоматически продвигаем до факта, если нужно
                self._try_promote_to_fact(hypothesis_id)
            elif all_success and new_status == "supported":
                self.db.update_hypothesis_status(hypothesis_id, "supported", confidence=min(1.0, hyp["confidence"] + 0.1))
                self.db.update_verification_job(job_id, "completed", json.dumps(results))
                _log(f"[HypothesisEngine] Hypothesis {hypothesis_id} SUPPORTED")
            else:
                new_conf = max(0.0, hyp["confidence"] - 0.1)
                if new_conf < 0.2:
                    self.db.update_hypothesis_status(hypothesis_id, "rejected", confidence=new_conf, rejection_reason="Verification failed")
                    _log(f"[HypothesisEngine] Hypothesis {hypothesis_id} REJECTED")
                else:
                    self.db.update_hypothesis_status(hypothesis_id, "investigating", confidence=new_conf)
                self.db.update_verification_job(job_id, "failed", json.dumps(results))
        except Exception as e:
            _log(f"[HypothesisEngine] Verification error for {hypothesis_id}: {e}")
            self.db.update_verification_job(job_id, "failed", str(e))

    def _auto_promote_hypotheses(self):
        hyps = self.db.get_hypotheses_by_status("verified", limit=20)
        for hyp in hyps:
            if hyp["confidence"] >= PROMOTION_CONFIDENCE_THRESHOLD:
                self._try_promote_to_fact(hyp["hypothesis_id"])

    def _try_promote_to_fact(self, hypothesis_id: str):
        hyp = self.db.get_hypothesis(hypothesis_id)
        if not hyp or hyp["status"] != "verified":
            return
        evidence_list = self.db.get_evidence(hypothesis_id)
        if len(evidence_list) >= EVIDENCE_REQUIRED or hyp["confidence"] >= PROMOTION_CONFIDENCE_THRESHOLD:
            if self.world_model:
                try:
                    self.world_model.add_fact(
                        statement=hyp["statement"],
                        confidence=hyp["confidence"],
                        source_tool="hypothesis_engine"
                    )
                    self.db.update_hypothesis_status(hypothesis_id, "promoted")
                    publish("hypothesis_promoted", {
                        "hypothesis_id": hypothesis_id,
                        "statement": hyp["statement"],
                        "confidence": hyp["confidence"]
                    }, source="hypothesis_engine")
                    _log(f"[HypothesisEngine] Promoted hypothesis {hypothesis_id} to fact")
                except Exception as e:
                    _log(f"[HypothesisEngine] Failed to promote: {e}")

    def _cleanup_expired(self):
        now = time.time()
        with self.db._get_conn() as conn:
            conn.execute("DELETE FROM hypotheses WHERE expires_at < ? AND status IN ('proposed', 'investigating', 'supported')", (now,))
            conn.commit()

    # Публичные методы
    def create_hypothesis(self, statement: str, confidence: float = 0.3,
                          explanation: str = "", verification_plan: List[Dict] = None,
                          source_entry_id: str = None, source_dialog_id: str = None,
                          source_tool: str = None, ttl_seconds: int = 86400) -> str:
        hyp_id = hashlib.md5(f"{statement}_{time.time()}".encode()).hexdigest()[:12]
        if verification_plan is None:
            verification_plan = []
        self.db.create_hypothesis(hyp_id, statement, confidence, explanation, verification_plan,
                                  source_entry_id, source_dialog_id, source_tool, ttl_seconds)
        _log(f"[HypothesisEngine] Created hypothesis {hyp_id}: {statement[:80]}")
        try:
            publish("hypothesis_created", {
                "hypothesis_id": hyp_id,
                "statement": statement,
                "confidence": confidence
            }, source="hypothesis_engine")
        except Exception:
            pass
        return hyp_id

    def add_evidence(self, hypothesis_id: str, evidence_statement: str, source: str,
                     source_entry_id: str = None, confidence: float = 0.5):
        self.db.add_evidence(hypothesis_id, evidence_statement, source, source_entry_id, confidence)

    def get_hypotheses(self, status: str = None) -> List[Dict]:
        return self.db.get_hypotheses_by_status(status)

    def get_hypothesis_details(self, hypothesis_id: str) -> Dict:
        hyp = self.db.get_hypothesis(hypothesis_id)
        if hyp:
            hyp["evidence"] = self.db.get_evidence(hypothesis_id)
        return hyp

    def manually_verify(self, hypothesis_id: str) -> Dict:
        self._verify_hypothesis(hypothesis_id)
        return {"status": "verification_started", "hypothesis_id": hypothesis_id}

    def promote_to_fact(self, hypothesis_id: str) -> Dict:
        """Принудительно продвинуть гипотезу до факта (вызывается извне)."""
        self._try_promote_to_fact(hypothesis_id)
        return {"status": "promotion_attempted", "hypothesis_id": hypothesis_id}

    def reject_hypothesis(self, hypothesis_id: str, reason: str) -> Dict:
        """Отклонить гипотезу вручную."""
        hyp = self.db.get_hypothesis(hypothesis_id)
        if not hyp:
            return {"error": "Hypothesis not found"}
        self.db.update_hypothesis_status(hypothesis_id, "rejected", rejection_reason=reason)
        return {"status": "rejected", "hypothesis_id": hypothesis_id, "reason": reason}

    # НОВАЯ ФУНКЦИЯ: обновление статуса гипотезы (без верификации)
    def update_hypothesis_status(self, hypothesis_id: str, status: str, confidence: float = None, rejection_reason: str = None) -> Dict:
        """Обновить статус гипотезы вручную."""
        hyp = self.db.get_hypothesis(hypothesis_id)
        if not hyp:
            return {"error": "Hypothesis not found"}
        if status not in HYPOTHESIS_STATUSES:
            return {"error": f"Invalid status: {status}"}
        self.db.update_hypothesis_status(hypothesis_id, status, confidence, rejection_reason)
        return {"status": "updated", "hypothesis_id": hypothesis_id, "new_status": status}

# ========== Глобальный экземпляр и MCP-инструменты ==========
_hypothesis_engine = HypothesisEngine()
_hypothesis_engine.start()

def hyp_create_hypothesis(statement: str, confidence: float = 0.3, explanation: str = "",
                          verification_plan: List[Dict] = None, source_tool: str = None) -> Dict:
    hyp_id = _hypothesis_engine.create_hypothesis(
        statement, confidence, explanation,
        verification_plan or [],
        source_tool=source_tool or "mcp_tool"
    )
    return {"status": "success", "hypothesis_id": hyp_id, "statement": statement}

def hyp_add_evidence(hypothesis_id: str, evidence: str, source: str, confidence: float = 0.5) -> Dict:
    _hypothesis_engine.add_evidence(hypothesis_id, evidence, source, confidence=confidence)
    return {"status": "success", "hypothesis_id": hypothesis_id}

def hyp_list_hypotheses(status: str = None) -> Dict:
    hyps = _hypothesis_engine.get_hypotheses(status)
    return {"status": "success", "hypotheses": hyps, "count": len(hyps)}

def hyp_get_hypothesis(hypothesis_id: str) -> Dict:
    hyp = _hypothesis_engine.get_hypothesis_details(hypothesis_id)
    if hyp:
        return {"status": "success", "hypothesis": hyp}
    else:
        return {"status": "error", "message": "Hypothesis not found"}

def hyp_verify_now(hypothesis_id: str) -> Dict:
    return _hypothesis_engine.manually_verify(hypothesis_id)

def hyp_promote_to_fact(hypothesis_id: str) -> Dict:
    return _hypothesis_engine.promote_to_fact(hypothesis_id)

def hyp_reject_hypothesis(hypothesis_id: str, reason: str) -> Dict:
    return _hypothesis_engine.reject_hypothesis(hypothesis_id, reason)

# НОВЫЙ ИНСТРУМЕНТ
def hyp_update_hypothesis_status(hypothesis_id: str, status: str, confidence: float = None, rejection_reason: str = None) -> Dict:
    """Обновить статус гипотезы (без запуска верификации)."""
    return _hypothesis_engine.update_hypothesis_status(hypothesis_id, status, confidence, rejection_reason)

# MCP сервер
server = BaseMCPServer("hypothesis-engine", "1.1")

server.register_tool("hyp_create_hypothesis", {
    "description": "Создать новую гипотезу с планом верификации",
    "inputSchema": {
        "type": "object",
        "properties": {
            "statement": {"type": "string"},
            "confidence": {"type": "number", "default": 0.3},
            "explanation": {"type": "string"},
            "verification_plan": {"type": "array", "items": {"type": "object"}},
            "source_tool": {"type": "string"}
        },
        "required": ["statement"]
    }
}, lambda **kw: hyp_create_hypothesis(kw["statement"], kw.get("confidence", 0.3), kw.get("explanation", ""),
                                      kw.get("verification_plan"), kw.get("source_tool")))

server.register_tool("hyp_add_evidence", {
    "description": "Добавить свидетельство в поддержку гипотезы",
    "inputSchema": {
        "type": "object",
        "properties": {
            "hypothesis_id": {"type": "string"},
            "evidence": {"type": "string"},
            "source": {"type": "string"},
            "confidence": {"type": "number", "default": 0.5}
        },
        "required": ["hypothesis_id", "evidence", "source"]
    }
}, lambda **kw: hyp_add_evidence(kw["hypothesis_id"], kw["evidence"], kw["source"], kw.get("confidence", 0.5)))

server.register_tool("hyp_list_hypotheses", {
    "description": "Список гипотез с возможной фильтрацией по статусу",
    "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}
}, lambda **kw: hyp_list_hypotheses(kw.get("status")))

server.register_tool("hyp_get_hypothesis", {
    "description": "Получить детали гипотезы со всеми свидетельствами",
    "inputSchema": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}}, "required": ["hypothesis_id"]}
}, lambda **kw: hyp_get_hypothesis(kw["hypothesis_id"]))

server.register_tool("hyp_verify_now", {
    "description": "Запустить верификацию гипотезы немедленно",
    "inputSchema": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}}, "required": ["hypothesis_id"]}
}, lambda **kw: hyp_verify_now(kw["hypothesis_id"]))

server.register_tool("hyp_promote_to_fact", {
    "description": "Продвинуть подтверждённую гипотезу до факта (вызвать World Model)",
    "inputSchema": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}}, "required": ["hypothesis_id"]}
}, lambda **kw: hyp_promote_to_fact(kw["hypothesis_id"]))

server.register_tool("hyp_reject_hypothesis", {
    "description": "Отклонить гипотезу с указанием причины",
    "inputSchema": {
        "type": "object",
        "properties": {
            "hypothesis_id": {"type": "string"},
            "reason": {"type": "string"}
        },
        "required": ["hypothesis_id", "reason"]
    }
}, lambda **kw: hyp_reject_hypothesis(kw["hypothesis_id"], kw["reason"]))

# НОВЫЙ ИНСТРУМЕНТ
server.register_tool("hyp_update_hypothesis_status", {
    "description": "Обновить статус гипотезы (без запуска верификации). Статусы: proposed, investigating, supported, verified, rejected, promoted",
    "inputSchema": {
        "type": "object",
        "properties": {
            "hypothesis_id": {"type": "string"},
            "status": {"type": "string", "enum": HYPOTHESIS_STATUSES},
            "confidence": {"type": "number"},
            "rejection_reason": {"type": "string"}
        },
        "required": ["hypothesis_id", "status"]
    }
}, lambda **kw: hyp_update_hypothesis_status(kw["hypothesis_id"], kw["status"], kw.get("confidence"), kw.get("rejection_reason")))

__mcp_plugin__ = {
    "name": "hypothesis-engine",
    "version": "1.1",
    "description": "Жизненный цикл гипотез: proposed → investigating → supported → verified → fact/rejected",
    "dependencies": [],
    "on_load": lambda: _log("[HypothesisEngine] v1.1 loaded with full lifecycle and Cognitive Bus"),
    "on_unload": lambda: _hypothesis_engine.stop()
}

if __name__ == "__main__":
    server.run()