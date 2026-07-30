#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Memory Tools v1.0 – инструменты для работы с многоуровневой памятью
Включает: explain_fact, verify_fact, deprecate_fact, get_working_memory,
а также улучшенный log_conversation.
"""
import json
from typing import Dict, Optional
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

__mcp_plugin__ = {
    "name": "memory-tools",
    "version": "1.0",
    "description": "Инструменты для верификации фактов и рабочей памяти",
    "dependencies": [],
    "on_load": lambda: _log("[MemoryTools] Loaded. Working memory and fact verification ready."),
    "on_unload": lambda: _log("[MemoryTools] Unloaded.")
}

def explain_fact(entry_id: str) -> Dict:
    """Детальный разбор факта: источник, уверенность, статус, противоречия, история."""
    conn = conversation_memory._open_conn()
    try:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return {"status": "error", "message": f"Entry {entry_id} not found"}
        entry = dict(row)
        entry['paths'] = json.loads(entry.get('paths_json', '{}')) if entry.get('paths_json') else {}
        entry['meta'] = json.loads(entry.get('meta_json', '{}')) if entry.get('meta_json') else {}
        
        contradictions = conn.execute(
            "SELECT * FROM contradictions WHERE entry_id_1 = ? OR entry_id_2 = ?",
            (entry_id, entry_id)
        ).fetchall()
        verif_log = conn.execute(
            "SELECT old_status, new_status, updated_at, updated_by FROM verification_log WHERE entry_id = ? ORDER BY updated_at",
            (entry_id,)
        ).fetchall()
        
        return {
            "status": "success",
            "fact": {
                "id": entry['id'],
                "text": entry.get('context', ''),
                "memory_type": entry.get('memory_type'),
                "confidence": entry.get('confidence'),
                "confidence_source": entry.get('confidence_source'),
                "verification_status": entry.get('verification_status'),
                "created_at": entry.get('ts'),
                "source_tool": entry.get('source_tool'),
                "source_entry_id": entry.get('source_entry_id'),
                "source_dialog_id": entry.get('source_dialog_id'),
                "trace_id": entry.get('trace_id'),
                "op": entry.get('op'),
                "paths": entry['paths']
            },
            "contradictions": [dict(c) for c in contradictions],
            "verification_history": [
                {"old_status": v['old_status'], "new_status": v['new_status'], "at": v['updated_at'], "by": v['updated_by']}
                for v in verif_log
            ]
        }
    finally:
        conn.close()

def verify_fact(entry_id: str, dialog_id: Optional[str] = None) -> Dict:
    """Перевести факт в статус verified (подтверждён)."""
    d_id = dialog_id or dialog_ctx.get()
    conn = conversation_memory._open_conn()
    try:
        row = conn.execute("SELECT verification_status FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return {"status": "error", "message": f"Entry {entry_id} not found"}
        old_status = row['verification_status']
        if old_status == 'verified':
            return {"status": "already_verified", "entry_id": entry_id}
        conversation_memory._update_verification_status(entry_id, 'verified')
        conversation_memory._log_verification(entry_id, old_status, 'verified', updated_by=d_id)
        return {"status": "verified", "entry_id": entry_id, "old_status": old_status}
    finally:
        conn.close()

def deprecate_fact(entry_id: str, dialog_id: Optional[str] = None) -> Dict:
    """Пометить факт как устаревший (deprecated)."""
    d_id = dialog_id or dialog_ctx.get()
    conn = conversation_memory._open_conn()
    try:
        row = conn.execute("SELECT verification_status FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return {"status": "error", "message": f"Entry {entry_id} not found"}
        old_status = row['verification_status']
        if old_status == 'deprecated':
            return {"status": "already_deprecated", "entry_id": entry_id}
        conversation_memory._update_verification_status(entry_id, 'deprecated')
        conversation_memory._log_verification(entry_id, old_status, 'deprecated', updated_by=d_id)
        return {"status": "deprecated", "entry_id": entry_id, "old_status": old_status}
    finally:
        conn.close()

def get_working_memory(limit: int = 100, dialog_id: Optional[str] = None) -> Dict:
    """Получить рабочую память для LLM (отфильтрованные записи с высоким confidence)."""
    d_id = dialog_id or dialog_ctx.get()
    entries = conversation_memory.get_working_memory(dialog_id=d_id, limit=limit)
    context_lines = []
    for e in entries:
        line = f"[{e['memory_type']} | conf={e['confidence']:.2f} | {e['verification_status']}] {e.get('context', '')}"
        context_lines.append(line)
    context = "\n".join(context_lines)
    return {
        "status": "success",
        "dialog_id": d_id,
        "count": len(entries),
        "entries": entries,
        "context_for_llm": context
    }

def log_conversation(role: str, content: str, dialog_id: Optional[str] = None, trace_id: Optional[str] = None) -> Dict:
    """Сохранить сообщение диалога (user/assistant) как факт с высоким confidence."""
    d_id = dialog_id or dialog_ctx.get()
    if role not in ('user', 'assistant'):
        return {"status": "error", "message": "role must be 'user' or 'assistant'"}
    entry_id = conversation_memory.add(
        op="conversation",
        paths={"role": role},
        status="logged",
        dialog=d_id,
        context=content.strip(),
        category="chat",
        tags=[role],
        memory_type='fact',
        confidence=0.90,
        confidence_source='user_quote' if role == 'user' else 'assistant_response',
        verification_status='unverified',
        trace_id=trace_id
    )
    return {"status": "ok", "entry_id": entry_id, "role": role, "content_preview": content[:100]}

def merge_duplicate_facts(dry_run: bool = True, dialog_id: Optional[str] = None) -> Dict:
    """
    Схлопывает точные дубли фактов (одинаковый нормализованный текст), чтобы БД
    не росла от повторов. Канонической остаётся запись с наибольшим confidence
    (при равенстве — самая ранняя); остальные помечаются deprecated со ссылкой
    'merged_into:<id>' в журнале верификации (НЕ удаляются — для аудита).
    Канонической присваивается максимальный confidence группы.

    dry_run=True (по умолчанию) — только показать, что будет слито, ничего не меняя.
    """
    import re
    d_id = dialog_id or dialog_ctx.get()
    conn = conversation_memory._open_conn()
    try:
        rows = conn.execute("""
            SELECT id, context, confidence, ts, verification_status
            FROM entries
            WHERE memory_type = 'fact'
              AND verification_status NOT IN ('deprecated', 'contradicted')
              AND context IS NOT NULL AND TRIM(context) != ''
        """).fetchall()

        groups: Dict[str, list] = {}
        for r in rows:
            norm = re.sub(r'\s+', ' ', (r["context"] or "").strip().lower())
            if len(norm) < 8:
                continue
            groups.setdefault(norm, []).append(dict(r))

        plan = []
        for norm, items in groups.items():
            if len(items) < 2:
                continue
            # канон: макс confidence, при равенстве — самый ранний ts
            items.sort(key=lambda x: (-(x["confidence"] or 0), x["ts"] or ""))
            canonical = items[0]
            dups = items[1:]
            max_conf = max((i["confidence"] or 0) for i in items)
            plan.append({
                "canonical_id": canonical["id"],
                "text_preview": (canonical["context"] or "")[:80],
                "merged_ids": [d["id"] for d in dups],
                "new_canonical_confidence": round(max_conf, 4),
            })

        merged_count = sum(len(p["merged_ids"]) for p in plan)
        if dry_run:
            return {
                "status": "preview",
                "dry_run": True,
                "duplicate_groups": len(plan),
                "entries_to_merge": merged_count,
                "plan": plan,
                "note": "Повторите с dry_run=false, чтобы применить.",
            }

        for p in plan:
            # канону — максимальный confidence группы
            conn.execute("UPDATE entries SET confidence = ? WHERE id = ?",
                         (p["new_canonical_confidence"], p["canonical_id"]))
            for dup_id in p["merged_ids"]:
                old = conn.execute("SELECT verification_status FROM entries WHERE id = ?",
                                   (dup_id,)).fetchone()
                old_status = old["verification_status"] if old else "unverified"
                # Все записи через ЭТО соединение (одна транзакция) — иначе вложенные
                # вызовы хелперов открыли бы второе write-соединение → database is locked.
                conn.execute("UPDATE entries SET verification_status = 'deprecated' WHERE id = ?",
                             (dup_id,))
                conn.execute(
                    "INSERT INTO verification_log (entry_id, old_status, new_status, updated_by) "
                    "VALUES (?, ?, 'deprecated', ?)",
                    (dup_id, old_status, f"merged_into:{p['canonical_id']}")
                )
        conn.commit()
        return {
            "status": "success",
            "dry_run": False,
            "duplicate_groups": len(plan),
            "merged_entries": merged_count,
            "plan": plan,
        }
    finally:
        conn.close()


def register_tools(server: BaseMCPServer):
    server.register_tool("explain_fact", {
        "description": "Показать детальную информацию о факте: источник, уверенность, статус, противоречия",
        "inputSchema": {
            "type": "object",
            "properties": {"entry_id": {"type": "string"}},
            "required": ["entry_id"]
        }
    }, lambda **kw: explain_fact(kw["entry_id"]))

    server.register_tool("verify_fact", {
        "description": "Подтвердить факт (перевести в статус verified)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "dialog_id": {"type": "string"}
            },
            "required": ["entry_id"]
        }
    }, lambda **kw: verify_fact(kw["entry_id"], kw.get("dialog_id")))

    server.register_tool("deprecate_fact", {
        "description": "Пометить факт как устаревший (deprecated)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "dialog_id": {"type": "string"}
            },
            "required": ["entry_id"]
        }
    }, lambda **kw: deprecate_fact(kw["entry_id"], kw.get("dialog_id")))

    server.register_tool("get_working_memory", {
        "description": "Получить рабочую память для LLM – отфильтрованные факты с confidence >= 0.8",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
                "dialog_id": {"type": "string"}
            }
        }
    }, lambda **kw: get_working_memory(kw.get("limit", 100), kw.get("dialog_id")))

    server.register_tool("log_conversation", {
        "description": "Сохранить сообщение пользователя или ассистента в память как факт",
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": ["user", "assistant"]},
                "content": {"type": "string"},
                "dialog_id": {"type": "string"},
                "trace_id": {"type": "string"}
            },
            "required": ["role", "content"]
        }
    }, lambda **kw: log_conversation(kw["role"], kw["content"], kw.get("dialog_id"), kw.get("trace_id")))

    server.register_tool("merge_duplicate_facts", {
        "description": "Merge exact-duplicate facts (identical normalized text) to stop DB growth from "
                       "repeats. Keeps the highest-confidence entry; marks others deprecated (not deleted). "
                       "Defaults to dry_run=true (preview only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "Preview only (default true); false to apply"},
                "dialog_id": {"type": "string"}
            }
        }
    }, lambda **kw: merge_duplicate_facts(kw.get("dry_run", True), kw.get("dialog_id")))

if __name__ == "__main__":
    server = BaseMCPServer("memory-tools", "1.0")
    register_tools(server)
    server.run()