#!/usr/bin/env python3
"""
MCP Reflection Engine v1.0
Автоматическое выявление противоречий и корректировка confidence.
Запускается по расписанию через mcp_scheduler.
"""

import os
import re
import json
import time
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)
from mcp_memory_graph import _graph_db, SimpleExtractor

# Конфигурация через переменные окружения
REFLECTION_INTERVAL_MIN = int(os.environ.get("MCP_REFLECTION_INTERVAL_MIN", "30"))
REFLECTION_LOOKBACK_DAYS = int(os.environ.get("MCP_REFLECTION_LOOKBACK_DAYS", "7"))
REFLECTION_CONFIDENCE_THRESHOLD = float(os.environ.get("MCP_REFLECTION_CONFIDENCE_THRESHOLD", "0.6"))
REFLECTION_AUTO_FIX = os.environ.get("MCP_REFLECTION_AUTO_FIX", "true").lower() == "true"

class ReflectionEngine:
    def __init__(self):
        self._last_run = 0
        self._lock = threading.Lock()
    
    def run_cycle(self, force: bool = False):
        """Основной цикл рефлексии. Вызывается по расписанию."""
        now = time.time()
        if not force and (now - self._last_run) < REFLECTION_INTERVAL_MIN * 60:
            return
        with self._lock:
            _log("[Reflection] Starting reflection cycle...")
            try:
                self._detect_contradictions()
                self._update_confidence_from_graph()
                self._cleanup_old_reflections()
                self._last_run = now
                _log("[Reflection] Cycle completed.")
            except Exception as e:
                _log(f"[Reflection] Error in cycle: {e}")
    
    def _detect_contradictions(self):
        """
        Поиск противоречий:
        - Один файл упоминается с разными путями
        - Одна модель с разными версиями
        - Один и тот же факт с разными значениями
        """
        # 1. Получаем недавние записи с высоким confidence
        cutoff = (datetime.now() - timedelta(days=REFLECTION_LOOKBACK_DAYS)).isoformat()
        conn = conversation_memory._open_conn()
        rows = conn.execute("""
            SELECT id, context, op, paths_json, confidence, memory_type, verification_status, ts
            FROM entries
            WHERE ts > ? AND confidence >= ? AND verification_status NOT IN ('deprecated', 'contradicted')
            ORDER BY ts
        """, (cutoff, REFLECTION_CONFIDENCE_THRESHOLD)).fetchall()
        conn.close()
        
        if not rows:
            return
        
        entries = []
        for r in rows:
            e = dict(r)
            e["paths"] = json.loads(e["paths_json"]) if e["paths_json"] else {}
            entries.append(e)
        
        # Группируем по извлечённым сущностям (используем SimpleExtractor)
        # Для простоты будем группировать по именам файлов и моделям
        file_map = defaultdict(list)   # имя файла -> список записей
        model_map = defaultdict(list)  # имя модели -> список записей
        
        for entry in entries:
            context = entry.get("context", "")
            # Ищем файлы
            for match in SimpleExtractor.PATTERNS["file"].finditer(context):
                fname = match.group(0)
                file_map[fname].append(entry)
            # Ищем модели
            for match in SimpleExtractor.PATTERNS["model"].finditer(context):
                mname = match.group(0)
                model_map[mname].append(entry)
        
        # Обрабатываем файловые противоречия
        for fname, entries_list in file_map.items():
            if len(entries_list) < 2:
                continue
            # Сортируем по времени
            entries_list.sort(key=lambda x: x["ts"])
            # Ищем изменения пути
            paths = [e.get("paths", {}).get("path") or e.get("paths", {}).get("source") for e in entries_list if e.get("paths")]
            unique_paths = [p for p in paths if p]
            if len(set(unique_paths)) > 1:
                # Противоречие: файл в разное время находился в разных местах
                # Последняя запись, вероятно, актуальна. Предыдущие понижаем.
                latest = entries_list[-1]
                for older in entries_list[:-1]:
                    self._resolve_contradiction(older, latest, reason=f"File path changed: {fname}")
        
        # Обрабатываем модельные противоречия (версии)
        for mname, entries_list in model_map.items():
            if len(entries_list) < 2:
                continue
            entries_list.sort(key=lambda x: x["ts"])
            # Извлекаем версии из контекста
            versions = []
            for e in entries_list:
                ctx = e["context"]
                ver_match = re.search(r'version\s+([\d\.]+)', ctx, re.IGNORECASE)
                if ver_match:
                    versions.append((e, ver_match.group(1)))
            if len(versions) < 2:
                continue
            # Если версии разные, последняя актуальнее
            latest_ver = versions[-1][1]
            latest_entry = versions[-1][0]
            for older_entry, old_ver in versions[:-1]:
                if old_ver != latest_ver:
                    self._resolve_contradiction(older_entry, latest_entry, reason=f"Model version changed: {mname} {old_ver} -> {latest_ver}")
    
    def _resolve_contradiction(self, old_entry: Dict, new_entry: Dict, reason: str):
        """
        Разрешает противоречие:
        - Понижает confidence у старой записи
        - Повышает у новой (если не максимальная)
        - Создаёт запись рефлексии
        """
        if not REFLECTION_AUTO_FIX:
            _log(f"[Reflection] Contradiction detected: {reason} (auto-fix disabled)")
            return
        
        old_id = old_entry["id"]
        new_id = new_entry["id"]
        old_conf = old_entry["confidence"]
        new_conf = new_entry["confidence"]
        
        # Понижаем старую, но не ниже 0.1
        new_old_conf = max(0.1, old_conf * 0.5)
        # Повышаем новую, но не выше 0.99
        new_new_conf = min(0.99, new_conf * 1.2)
        
        # Обновляем в БД
        conn = conversation_memory._open_conn()
        conn.execute("UPDATE entries SET confidence = ?, verification_status = 'contradicted' WHERE id = ?",
                     (new_old_conf, old_id))
        conn.execute("UPDATE entries SET confidence = ?, verification_status = 'verified' WHERE id = ?",
                     (new_new_conf, new_id))
        # Создаём запись рефлексии
        reflection_id = conversation_memory.add(
            op="reflection",
            paths={"reason": reason},
            status="contradiction_resolved",
            context=f"Auto-resolved contradiction: {reason}. Old confidence {old_conf:.2f}->{new_old_conf:.2f}, New {new_conf:.2f}->{new_new_conf:.2f}",
            related=[old_id, new_id],
            confidence=0.95,
            memory_type='reflection'
        )
        conn.commit()
        conn.close()
        
        # Обновляем граф (если есть модуль)
        try:
            from mcp_memory_graph import process_entry
            process_entry(reflection_id)
            process_entry(old_id)
            process_entry(new_id)
        except ImportError:
            pass
        
        _log(f"[Reflection] Resolved contradiction: {reason}")
    
    def _update_confidence_from_graph(self):
        """
        Повышает confidence фактов, подтверждённых несколькими НЕЗАВИСИМЫМИ
        записями памяти (corroboration). Объединяет два сигнала:
          1) точное совпадение нормализованного текста фактов в таблице entries;
          2) провенанс графа знаний (statement_support): одно утверждение,
             порождённое >= 2 разными записями.
        Прирост: +0.03 за каждое доп. подтверждение (макс +0.15, потолок 0.98).
        Записи deprecated/contradicted не трогаются.
        """
        if not REFLECTION_AUTO_FIX:
            return

        # entry_id -> число независимых подтверждений (берём максимум по сигналам)
        corroboration: Dict[str, int] = {}

        # ── Сигнал 1: одинаковый нормализованный текст фактов ────────────────
        cutoff = (datetime.now() - timedelta(days=REFLECTION_LOOKBACK_DAYS)).isoformat()
        conn = conversation_memory._open_conn()
        try:
            rows = conn.execute("""
                SELECT id, context, verification_status
                FROM entries
                WHERE ts > ? AND memory_type = 'fact'
                  AND verification_status NOT IN ('deprecated', 'contradicted')
                  AND context IS NOT NULL AND TRIM(context) != ''
            """, (cutoff,)).fetchall()
            groups: Dict[str, set] = defaultdict(set)
            for r in rows:
                norm = re.sub(r'\s+', ' ', (r["context"] or "").strip().lower())
                if len(norm) >= 8:
                    groups[norm].add(r["id"])
            for ids in groups.values():
                if len(ids) >= 2:
                    for eid in ids:
                        corroboration[eid] = max(corroboration.get(eid, 0), len(ids))
        except Exception as e:
            _log(f"[Reflection] Corroboration (text) failed: {e}")
        finally:
            conn.close()

        # ── Сигнал 2: провенанс графа знаний ────────────────────────────────
        try:
            from mcp_memory_graph import _graph_db
            for st in _graph_db.get_corroborated_statements(min_sources=2):
                support = st.get("support", 0)
                for eid in st.get("source_entry_ids", []):
                    if eid:
                        corroboration[eid] = max(corroboration.get(eid, 0), support)
        except Exception as e:
            _log(f"[Reflection] Corroboration (graph) skipped: {e}")

        if not corroboration:
            return

        updated = 0
        conn = conversation_memory._open_conn()
        try:
            for eid, support in corroboration.items():
                row = conn.execute(
                    "SELECT confidence, verification_status FROM entries WHERE id = ?",
                    (eid,)
                ).fetchone()
                if not row or row["verification_status"] in ("deprecated", "contradicted"):
                    continue
                old_conf = row["confidence"] if row["confidence"] is not None else 0.5
                boost = min(0.15, 0.03 * (support - 1))
                new_conf = min(0.98, round(old_conf + boost, 4))
                if new_conf > old_conf + 1e-6:
                    conn.execute(
                        "UPDATE entries SET confidence = ?, "
                        "confidence_source = 'corroboration' WHERE id = ?",
                        (new_conf, eid)
                    )
                    updated += 1
            conn.commit()
        except Exception as e:
            _log(f"[Reflection] Confidence corroboration failed: {e}")
        finally:
            conn.close()

        if updated:
            _log(f"[Reflection] Confidence boosted for {updated} corroborated entries")
    
    def _cleanup_old_reflections(self):
        """Удаляет старые записи рефлексии (старше 30 дней)"""
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        conn = conversation_memory._open_conn()
        conn.execute("DELETE FROM entries WHERE op = 'reflection' AND ts < ?", (cutoff,))
        conn.commit()
        conn.close()

# Глобальный экземпляр
_reflection = ReflectionEngine()

def run_reflection_cycle():
    """Функция для вызова из шедулера"""
    _reflection.run_cycle()

def register_tools(server: BaseMCPServer):
    server.register_tool("run_reflection_now", {
        "description": "Запустить цикл рефлексии вручную (поиск противоречий и корректировка confidence)",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: _reflection.run_cycle(force=True) or {"status": "completed"})
    
    server.register_tool("get_reflection_stats", {
        "description": "Статистика рефлексии",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: {
        "last_run": datetime.fromtimestamp(_reflection._last_run).isoformat() if _reflection._last_run else None,
        "interval_min": REFLECTION_INTERVAL_MIN,
        "auto_fix": REFLECTION_AUTO_FIX
    })

# Плагин
__mcp_plugin__ = {
    "name": "reflection-engine",
    "version": "1.0",
    "description": "Автоматическое выявление противоречий и корректировка confidence",
    "dependencies": [],
    "on_load": lambda: _log("[Reflection] Engine loaded. Will run every {} min.".format(REFLECTION_INTERVAL_MIN)),
    "on_unload": lambda: _log("[Reflection] Unloaded.")
}

# Автоматическая регистрация в шедулере при загрузке (если шедулер активен)
def _schedule_reflection():
    try:
        from mcp_scheduler import scheduler_add_interval
        scheduler_add_interval("reflection_cycle", "run_reflection_cycle", REFLECTION_INTERVAL_MIN * 60, {})
        _log("[Reflection] Scheduled periodic cycle.")
    except ImportError:
        _log("[Reflection] Scheduler not available, will not run automatically.")

# Запускаем регистрацию в шедулере после загрузки модуля
threading.Timer(10, _schedule_reflection).start()