#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Memory Graph Server v1.2 – исправленная версия
Графовая память: сущности, отношения, убеждения, факты, гипотезы.
Извлекает знания из существующих записей conversation_memory без NLP.
"""
import os
import re
import json
import sqlite3
import hashlib
import threading
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx,
    normalize_path, _ensure_allowed
)

# ========== Конфигурация ==========
GRAPH_DB_PATH = os.environ.get("MCP_GRAPH_DB", os.path.join(os.path.dirname(__file__), "memory_graph.db"))
AUTO_EXTRACT = os.environ.get("MCP_GRAPH_AUTO_EXTRACT", "true").lower() == "true"

# ========== База данных графа ==========
class GraphDB:
    def __init__(self, db_path: str = GRAPH_DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()
        
    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_name ON entities(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(type)")
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS relations (
                    relation_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    confidence REAL DEFAULT 0.8,
                    source_entry_id TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES entities(entity_id),
                    FOREIGN KEY(target_id) REFERENCES entities(entity_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_relation_source ON relations(source_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_relation_target ON relations(target_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_relation_type ON relations(relation_type)")
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS beliefs (
                    belief_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    source_entry_id TEXT,
                    source_dialog_id TEXT,
                    source_tool TEXT,
                    verification_status TEXT DEFAULT 'unverified',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_belief_entry ON beliefs(source_entry_id)")
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    fact_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    confidence REAL DEFAULT 0.9,
                    source_entry_id TEXT,
                    source_dialog_id TEXT,
                    source_tool TEXT,
                    verification_status TEXT DEFAULT 'verified',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_entry ON facts(source_entry_id)")
            
            # Провенанс: какие НЕЗАВИСИМЫЕ записи подтверждают одно утверждение.
            # Раньше add_belief/add_fact (INSERT OR REPLACE по детерминированному id)
            # схлопывали утверждение из разных источников в одну строку и теряли,
            # кто и сколько раз его подтвердил. Эта таблица сохраняет все источники.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS statement_support (
                    statement_id TEXT NOT NULL,
                    kind TEXT NOT NULL,            -- 'belief' | 'fact'
                    source_entry_id TEXT NOT NULL,
                    source_dialog_id TEXT,
                    source_tool TEXT,
                    confidence REAL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (statement_id, source_entry_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_support_stmt ON statement_support(statement_id)")
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    confidence REAL DEFAULT 0.3,
                    evidence TEXT,
                    source_entry_id TEXT,
                    source_dialog_id TEXT,
                    source_tool TEXT,
                    status TEXT DEFAULT 'proposed',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hypothesis_entry ON hypotheses(source_entry_id)")
            
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.commit()

    def _get_conn(self):
        """
        Возвращает новое соединение с SQLite.
        Соединение само поддерживает контекстный менеджер (with),
        поэтому его можно использовать как: with self._get_conn() as conn:
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _make_id(self, prefix: str, name: str) -> str:
        safe = re.sub(r'[^\w\-]', '_', name.lower())
        return f"{prefix}_{hashlib.md5(safe.encode()).hexdigest()[:12]}"

    def add_entity(self, name: str, entity_type: str = "unknown", confidence: float = 1.0) -> str:
        entity_id = self._make_id("ent", name)
        now = datetime.now().timestamp()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO entities (entity_id, name, type, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?)
            """, (entity_id, name, entity_type, now, now))
            if conn.total_changes == 0:
                conn.execute("UPDATE entities SET last_seen = ? WHERE entity_id = ?", (now, entity_id))
            conn.commit()
        return entity_id

    def add_relation(self, source_id: str, relation_type: str, target_id: str, 
                     confidence: float = 0.8, source_entry_id: str = None):
        rel_id = self._make_id("rel", f"{source_id}_{relation_type}_{target_id}")
        now = datetime.now().timestamp()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO relations 
                (relation_id, source_id, relation_type, target_id, confidence, source_entry_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (rel_id, source_id, relation_type, target_id, confidence, source_entry_id, now))
            conn.commit()

    def add_belief(self, statement: str, confidence: float, source_entry_id: str = None, 
                   source_dialog_id: str = None, source_tool: str = None,
                   verification_status: str = "unverified") -> str:
        belief_id = self._make_id("bel", statement[:100])
        now = datetime.now().timestamp()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO beliefs
                (belief_id, statement, confidence, source_entry_id, source_dialog_id, source_tool,
                 verification_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (belief_id, statement, confidence, source_entry_id, source_dialog_id, 
                  source_tool, verification_status, now))
            if source_entry_id:
                conn.execute("""
                    INSERT OR REPLACE INTO statement_support
                    (statement_id, kind, source_entry_id, source_dialog_id, source_tool, confidence, created_at)
                    VALUES (?, 'belief', ?, ?, ?, ?, ?)
                """, (belief_id, source_entry_id, source_dialog_id, source_tool, confidence, now))
                # Уверенность растёт с числом независимых подтверждений (capped).
                support = conn.execute(
                    "SELECT COUNT(DISTINCT source_entry_id) FROM statement_support WHERE statement_id = ?",
                    (belief_id,)
                ).fetchone()[0]
                if support > 1:
                    boosted = min(0.99, round(confidence + 0.02 * (support - 1), 4))
                    conn.execute("UPDATE beliefs SET confidence = ? WHERE belief_id = ?", (boosted, belief_id))
            conn.commit()
        return belief_id

    def add_fact(self, statement: str, confidence: float, source_entry_id: str = None,
                 source_dialog_id: str = None, source_tool: str = None) -> str:
        fact_id = self._make_id("fact", statement[:100])
        now = datetime.now().timestamp()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO facts
                (fact_id, statement, confidence, source_entry_id, source_dialog_id, source_tool,
                 verification_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (fact_id, statement, confidence, source_entry_id, source_dialog_id,
                  source_tool, 'verified', now))
            if source_entry_id:
                conn.execute("""
                    INSERT OR REPLACE INTO statement_support
                    (statement_id, kind, source_entry_id, source_dialog_id, source_tool, confidence, created_at)
                    VALUES (?, 'fact', ?, ?, ?, ?, ?)
                """, (fact_id, source_entry_id, source_dialog_id, source_tool, confidence, now))
                support = conn.execute(
                    "SELECT COUNT(DISTINCT source_entry_id) FROM statement_support WHERE statement_id = ?",
                    (fact_id,)
                ).fetchone()[0]
                if support > 1:
                    boosted = min(0.99, round(confidence + 0.02 * (support - 1), 4))
                    conn.execute("UPDATE facts SET confidence = ? WHERE fact_id = ?", (boosted, fact_id))
            conn.commit()
        # Публикация события
        try:
            from mcp_cognitive_bus import publish
            publish("fact_added", {
                "fact_id": fact_id,
                "statement": statement,
                "confidence": confidence,
                "source": source_tool or "memory_graph"
            }, source="memory_graph")
        except ImportError:
            pass
        return fact_id

    def add_hypothesis(self, statement: str, evidence: str, confidence: float = 0.3,
                       source_entry_id: str = None, source_dialog_id: str = None, 
                       source_tool: str = None, status: str = "proposed") -> str:
        hypothesis_id = self._make_id("hyp", statement[:100])
        now = datetime.now().timestamp()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO hypotheses
                (hypothesis_id, statement, confidence, evidence, source_entry_id, source_dialog_id,
                 source_tool, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (hypothesis_id, statement, confidence, evidence, source_entry_id, 
                  source_dialog_id, source_tool, status, now))
            conn.commit()
        return hypothesis_id

    def query_entities(self, name_filter: str = None, entity_type: str = None, limit: int = 50) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT entity_id, name, type, created_at, last_seen FROM entities WHERE 1=1"
            params = []
            if name_filter:
                sql += " AND name LIKE ?"
                params.append(f"%{name_filter}%")
            if entity_type:
                sql += " AND type = ?"
                params.append(entity_type)
            sql += " ORDER BY last_seen DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def query_relations(self, source_id: str = None, target_id: str = None, 
                        relation_type: str = None, limit: int = 50) -> List[Dict]:
        with self._get_conn() as conn:
            sql = """
                SELECT r.relation_id, r.source_id, r.relation_type, r.target_id, 
                       r.confidence, r.source_entry_id, r.created_at,
                       e1.name as source_name, e2.name as target_name
                FROM relations r
                JOIN entities e1 ON r.source_id = e1.entity_id
                JOIN entities e2 ON r.target_id = e2.entity_id
                WHERE 1=1
            """
            params = []
            if source_id:
                sql += " AND r.source_id = ?"
                params.append(source_id)
            if target_id:
                sql += " AND r.target_id = ?"
                params.append(target_id)
            if relation_type:
                sql += " AND r.relation_type = ?"
                params.append(relation_type)
            sql += " ORDER BY r.created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_beliefs_by_entry(self, entry_id: str) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT belief_id, statement, confidence, verification_status, created_at FROM beliefs WHERE source_entry_id = ?",
                (entry_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_statement_support(self, statement_id: str) -> List[Dict]:
        """Все независимые источники (записи), подтверждающие утверждение."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT source_entry_id, source_dialog_id, source_tool, confidence, created_at "
                "FROM statement_support WHERE statement_id = ? ORDER BY created_at",
                (statement_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_corroborated_statements(self, min_sources: int = 2, limit: int = 200) -> List[Dict]:
        """
        Утверждения, подтверждённые >= min_sources независимыми записями.
        Возвращает [{statement_id, kind, support, source_entry_ids}], отсортировано
        по убыванию числа подтверждений. Используется рефлексией для усиления
        confidence и для аналитики провенанса.
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT statement_id, kind, COUNT(DISTINCT source_entry_id) AS support, "
                "GROUP_CONCAT(source_entry_id) AS ids "
                "FROM statement_support GROUP BY statement_id "
                "HAVING support >= ? ORDER BY support DESC LIMIT ?",
                (min_sources, limit)
            ).fetchall()
            out = []
            for r in rows:
                out.append({
                    "statement_id": r["statement_id"],
                    "kind": r["kind"],
                    "support": r["support"],
                    "source_entry_ids": (r["ids"] or "").split(","),
                })
            return out

    def query_facts(self, statement_filter: str = None, limit: int = 50) -> List[Dict]:
        """
        Поиск фактов по частичному совпадению с утверждением.
        Возвращает список фактов, отсортированных по убыванию confidence.
        """
        with self._get_conn() as conn:
            sql = "SELECT * FROM facts WHERE 1=1"
            params = []
            if statement_filter:
                sql += " AND statement LIKE ?"
                params.append(f"%{statement_filter}%")
            sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def query_hypotheses(self, status: str = None, limit: int = 50) -> List[Dict]:
        with self._get_conn() as conn:
            sql = "SELECT * FROM hypotheses WHERE 1=1"
            params = []
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def update_hypothesis_status(self, hypothesis_id: str, status: str):
        with self._get_conn() as conn:
            conn.execute("UPDATE hypotheses SET status = ? WHERE hypothesis_id = ?", (status, hypothesis_id))
            conn.commit()

    def get_stats(self) -> Dict:
        with self._get_conn() as conn:
            ent_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            rel_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            bel_count = conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]
            fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            hyp_count = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
            return {
                "entities": ent_count,
                "relations": rel_count,
                "beliefs": bel_count,
                "facts": fact_count,
                "hypotheses": hyp_count
            }

# ========== Извлечение сущностей и отношений из записи ==========
class SimpleExtractor:
    PATTERNS = {
        "file": re.compile(r'[A-Za-z]:\\(?:[^\\/:*?"<>\r\n]+\\)*[^\\/:*?"<>\r\n]*', re.IGNORECASE),
        "url": re.compile(r'https?://[^\s]+', re.IGNORECASE),
        "ip": re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
        "model": re.compile(r'\b(Qwen|GPT|LLaMA|Mistral|Gemma|Phi|Claude|DeepSeek|Llama)\s*[\d\.]+\b', re.IGNORECASE),
        "tool": re.compile(r'(batch_move|search_files|run_shell|index_all_files_content|extract_text)'),
        "dialog_id": re.compile(r'dialog_[a-f0-9_]+'),
    }
    
    RELATION_KEYWORDS = {
        "uses": ["использует", "uses", "запустил", "вызвал", "через"],
        "contains": ["содержит", "находится в", "внутри", "в папке"],
        "depends_on": ["зависит от", "нужен", "требует", "depends on"],
        "generated": ["создал", "сгенерировал", "создан", "сгенерирован"],
        "imports": ["импортирует", "import", "from"],
        "located_at": ["лежит в", "путь", "расположен"],
    }

    @classmethod
    def extract_from_entry(cls, entry: Dict, dialog_id: str = None, tool: str = None) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        entities = []
        relations = []
        beliefs = []
        
        context = entry.get("context", "")
        op = entry.get("op", "")
        paths = entry.get("paths", {})
        entry_id = entry.get("id")
        confidence = entry.get("confidence", 0.8)
        
        found_names = set()
        
        for match in cls.PATTERNS["file"].finditer(context):
            name = match.group(0)
            if name not in found_names:
                found_names.add(name)
                entities.append({"name": name, "type": "file"})
                
        for match in cls.PATTERNS["url"].finditer(context):
            name = match.group(0)
            if name not in found_names:
                found_names.add(name)
                entities.append({"name": name, "type": "url"})
                
        for match in cls.PATTERNS["model"].finditer(context):
            name = match.group(0)
            if name not in found_names:
                found_names.add(name)
                entities.append({"name": name, "type": "model"})
                
        for match in cls.PATTERNS["tool"].finditer(context):
            name = match.group(0)
            if name not in found_names:
                found_names.add(name)
                entities.append({"name": name, "type": "tool"})
                
        for path_val in paths.values():
            if isinstance(path_val, str) and len(path_val) > 3:
                if ":" in path_val or path_val.startswith("\\\\"):
                    entities.append({"name": path_val, "type": "file"})
                    found_names.add(path_val)
                    
        if "source" in paths and "destination" in paths:
            src = paths["source"]
            dst = paths["destination"]
            entities.append({"name": src, "type": "file"})
            entities.append({"name": dst, "type": "file"})
            relations.append({
                "source": src, "type": "moved" if op == "move_file" else "copied",
                "target": dst, "confidence": confidence
            })
        elif "file_path" in paths:
            file_ent = paths["file_path"]
            entities.append({"name": file_ent, "type": "file"})
            relations.append({
                "source": file_ent, "type": op, "target": op,
                "confidence": confidence
            })
            
        if entry.get("memory_type") == "fact" and confidence > 0.7:
            statement = context[:200]
            beliefs.append({
                "statement": statement,
                "confidence": confidence,
                "source_entry_id": entry_id,
                "source_dialog_id": dialog_id,
                "source_tool": tool
            })
            
        unique_entities = {}
        for e in entities:
            name = e["name"]
            if name not in unique_entities:
                unique_entities[name] = e
        entities = list(unique_entities.values())
        
        return entities, relations, beliefs

# ========== Интеграция с ConversationMemory ==========
_graph_db = GraphDB()

def process_entry(entry_id: str):
    if not AUTO_EXTRACT:
        return
    try:
        conn = conversation_memory._open_conn()
        try:
            row = conn.execute(
                "SELECT id, context, op, paths_json, confidence, memory_type, verification_status, dialog FROM entries WHERE id = ?",
                (entry_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            row = conn.execute(
                "SELECT id, context, op, paths_json, confidence, memory_type, verification_status FROM entries WHERE id = ?",
                (entry_id,)
            ).fetchone()
        conn.close()
        
        if not row:
            return
            
        entry = dict(row)
        entry["paths"] = json.loads(entry["paths_json"]) if entry.get("paths_json") else {}
        dialog_id = entry.get("dialog") or dialog_ctx.get()
        tool = entry.get("op")
        
        entities, relations, beliefs = SimpleExtractor.extract_from_entry(entry, dialog_id, tool)
        
        for ent in entities:
            _graph_db.add_entity(ent["name"], ent.get("type", "unknown"), confidence=entry.get("confidence", 0.8))
            
        for rel in relations:
            src_id = _graph_db._make_id("ent", rel["source"])
            tgt_id = _graph_db._make_id("ent", rel["target"])
            _graph_db.add_entity(rel["source"], "file")
            _graph_db.add_entity(rel["target"], "file")
            _graph_db.add_relation(src_id, rel["type"], tgt_id, confidence=rel.get("confidence", 0.8), source_entry_id=entry_id)
            
        for bel in beliefs:
            _graph_db.add_belief(
                statement=bel["statement"],
                confidence=bel["confidence"],
                source_entry_id=bel.get("source_entry_id", entry_id),
                source_dialog_id=bel.get("source_dialog_id"),
                source_tool=bel.get("source_tool"),
                verification_status=entry.get("verification_status", "unverified")
            )
    except Exception as e:
        _log(f"[MemoryGraph] Failed to process entry {entry_id}: {e}")

_original_add = conversation_memory.add

def _patched_add(*args, **kwargs):
    entry_id = _original_add(*args, **kwargs)
    if entry_id and AUTO_EXTRACT:
        threading.Thread(target=process_entry, args=(entry_id,), daemon=True).start()
    return entry_id

conversation_memory.add = _patched_add

# ========== Инструменты MCP ==========
def graph_query_entities(name_filter: str = None, entity_type: str = None, limit: int = 50) -> Dict:
    results = _graph_db.query_entities(name_filter, entity_type, limit)
    return {"status": "success", "entities": results, "count": len(results)}

def graph_query_relations(source_name: str = None, target_name: str = None,
                          relation_type: str = None, limit: int = 50) -> Dict:
    src_id = _graph_db._make_id("ent", source_name) if source_name else None
    tgt_id = _graph_db._make_id("ent", target_name) if target_name else None
    results = _graph_db.query_relations(src_id, tgt_id, relation_type, limit)
    return {"status": "success", "relations": results, "count": len(results)}

def graph_get_beliefs(entry_id: str = None, statement_filter: str = None, limit: int = 50) -> Dict:
    if entry_id:
        beliefs = _graph_db.get_beliefs_by_entry(entry_id)
    else:
        with _graph_db._get_conn() as conn:
            sql = "SELECT belief_id, statement, confidence, verification_status, created_at FROM beliefs"
            params = []
            if statement_filter:
                sql += " WHERE statement LIKE ?"
                params.append(f"%{statement_filter}%")
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            beliefs = [dict(r) for r in rows]
    return {"status": "success", "beliefs": beliefs, "count": len(beliefs)}

def graph_query_facts(statement_filter: str = None, limit: int = 50) -> Dict:
    results = _graph_db.query_facts(statement_filter, limit)
    return {"status": "success", "facts": results, "count": len(results)}

def graph_query_hypotheses(status: str = None, limit: int = 50) -> Dict:
    results = _graph_db.query_hypotheses(status, limit)
    return {"status": "success", "hypotheses": results, "count": len(results)}

def graph_update_hypothesis_status(hypothesis_id: str, status: str) -> Dict:
    _graph_db.update_hypothesis_status(hypothesis_id, status)
    return {"status": "success", "hypothesis_id": hypothesis_id, "new_status": status}

def graph_stats() -> Dict:
    return _graph_db.get_stats()

# ========== Регистрация сервера ==========
server = BaseMCPServer("memory-graph", "1.1")

server.register_tool("graph_query_entities", {
    "description": "Поиск сущностей в графе (файлы, модели, URL, инструменты)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "name_filter": {"type": "string"},
            "entity_type": {"type": "string", "enum": ["file", "model", "url", "tool", "unknown"]},
            "limit": {"type": "integer", "default": 50}
        }
    }
}, lambda **kw: graph_query_entities(kw.get("name_filter"), kw.get("entity_type"), kw.get("limit", 50)))

server.register_tool("graph_query_relations", {
    "description": "Поиск отношений между сущностями",
    "inputSchema": {
        "type": "object",
        "properties": {
            "source_name": {"type": "string"},
            "target_name": {"type": "string"},
            "relation_type": {"type": "string"},
            "limit": {"type": "integer", "default": 50}
        }
    }
}, lambda **kw: graph_query_relations(kw.get("source_name"), kw.get("target_name"), 
                                      kw.get("relation_type"), kw.get("limit", 50)))

server.register_tool("graph_get_beliefs", {
    "description": "Получить убеждения из графа",
    "inputSchema": {
        "type": "object",
        "properties": {
            "entry_id": {"type": "string"},
            "statement_filter": {"type": "string"},
            "limit": {"type": "integer", "default": 50}
        }
    }
}, lambda **kw: graph_get_beliefs(kw.get("entry_id"), kw.get("statement_filter"), kw.get("limit", 50)))

server.register_tool("graph_query_facts", {
    "description": "Поиск подтвержденных фактов в графе",
    "inputSchema": {
        "type": "object",
        "properties": {
            "statement_filter": {"type": "string"},
            "limit": {"type": "integer", "default": 50}
        }
    }
}, lambda **kw: graph_query_facts(kw.get("statement_filter"), kw.get("limit", 50)))

server.register_tool("graph_query_hypotheses", {
    "description": "Поиск гипотез в графе",
    "inputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["proposed", "testing", "confirmed", "rejected"]},
            "limit": {"type": "integer", "default": 50}
        }
    }
}, lambda **kw: graph_query_hypotheses(kw.get("status"), kw.get("limit", 50)))

server.register_tool("graph_update_hypothesis_status", {
    "description": "Обновить статус гипотезы (proposed, testing, confirmed, rejected)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "hypothesis_id": {"type": "string"},
            "status": {"type": "string", "enum": ["proposed", "testing", "confirmed", "rejected"]}
        },
        "required": ["hypothesis_id", "status"]
    }
}, lambda **kw: graph_update_hypothesis_status(kw["hypothesis_id"], kw["status"]))

server.register_tool("graph_stats", {
    "description": "Статистика графа: количество сущностей, отношений, убеждений, фактов, гипотез",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: graph_stats())

server.register_tool("graph_process_entry", {
    "description": "Принудительно извлечь знания из записи по ID",
    "inputSchema": {"type": "object", "properties": {"entry_id": {"type": "string"}}, "required": ["entry_id"]}
}, lambda **kw: process_entry(kw["entry_id"]) or {"status": "processed", "entry_id": kw["entry_id"]})

# ========== Плагин для авто-загрузки ==========
__mcp_plugin__ = {
    "name": "memory-graph",
    "version": "1.1",
    "description": "Графовая память: сущности, отношения, убеждения, факты, гипотезы. Авто-извлечение при добавлении записей.",
    "dependencies": [],
    "on_load": lambda: _log("[MemoryGraph] v1.1 loaded. Auto-extract enabled. Facts and Hypotheses supported."),
    "on_unload": lambda: _log("[MemoryGraph] Unloaded.")
}

if __name__ == "__main__":
    server.run()