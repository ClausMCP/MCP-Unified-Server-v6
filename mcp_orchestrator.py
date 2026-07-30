#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Orchestrator v3.9 (Cognitive Integration + Context-Isolated + !command + caching + LLM Fallback + Auto-Index + Validation + Offline Search + Large Text Saving)
Natural language entry point with dynamic tool resolution, bounded fallback,
explicit command syntax (!tool key=value ...), optional plan caching and LLM planning.
Now includes cognitive tools: Hypothesis, World Model, Planning, Goal Manager, Reflection,
and large text saving to file.
"""
import os
import re
import time
import json
import hashlib
import importlib
import threading
import socket
import asyncio
import inspect
from typing import List, Dict, Any, Optional, Tuple

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx, is_online
)
from mcp_rate_limiter import safe_call, rate_limiter, circuit_breaker

# ─── Интеграция mcp_fs_advanced ──────────────────────────────────────────────
try:
    import mcp_fs_advanced as fs_adv
    FS_ADV_AVAILABLE = True
except ImportError:
    FS_ADV_AVAILABLE = False
    _log("[Orchestrator] mcp_fs_advanced not available")

# ─── Фоновая авто-индексация при старте сервера ──────────────────────────────
def _auto_index_on_startup():
    """Фоновая авто-индексация путей из переменной окружения MCP_AUTO_INDEX_PATHS."""
    if not FS_ADV_AVAILABLE: return
    auto_index_paths = os.environ.get("MCP_AUTO_INDEX_PATHS", "")
    if not auto_index_paths: return
    paths = [p.strip() for p in auto_index_paths.split(';') if p.strip()]
    for path in paths:
        try:
            _log(f"[Orchestrator] 🚀 Auto-indexing starting for: {path}")
            res = fs_adv.index_all_files_content(path, force_reindex=False, max_files=5000)
            _log(f"[Orchestrator] ✅ Auto-indexing finished for {path}: {res.get('indexed', 0)} files indexed.")
        except Exception as e:
            _log(f"[Orchestrator] ❌ Auto-indexing failed for {path}: {e}")

if os.environ.get("MCP_AUTO_INDEX", "false").lower() == "true":
    threading.Thread(target=_auto_index_on_startup, daemon=True, name="auto-indexer").start()

# ─── Configuration ──────────────────────────────────────────────────────────
CACHE_ENABLED = os.environ.get("MCP_ORCHESTRATOR_CACHE", "true").lower() == "true"
CACHE_TTL_SEC = int(os.environ.get("MCP_ORCHESTRATOR_CACHE_TTL", "3600"))
MAX_CACHE_ENTRIES = int(os.environ.get("MCP_ORCHESTRATOR_CACHE_SIZE", "100"))

# ─── Command parser for !syntax ─────────────────────────────────────────────
COMMAND_PATTERN = re.compile(r'^[!@]([a-zA-Z_][a-zA-Z0-9_]*)\s+(.*)$', re.IGNORECASE)
ARG_PATTERN = re.compile(r'([a-zA-Z_][a-zA-Z0-9_-]*)=("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|(?:[^\s]+))', re.UNICODE)

def parse_command(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    text = text.strip()
    if not text or text[0] not in ('!', '@'): return None
    m = COMMAND_PATTERN.match(text)
    if not m: return None
    tool_name = m.group(1)
    args_str = m.group(2).strip()
    if not args_str: return tool_name, {}
    args = {}
    for match in ARG_PATTERN.finditer(args_str):
        key = match.group(1)
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'): value = value[1:-1].replace('\\"', '"')
        elif value.startswith("'") and value.endswith("'"): value = value[1:-1].replace("\\'", "'")
        if value.isdigit(): value = int(value)
        elif value.replace('.', '', 1).isdigit() and value.count('.') == 1: value = float(value)
        elif value.lower() in ('true', 'false'): value = value.lower() == 'true'
        args[key] = value
    return tool_name, args

# ─── Plan cache ─────────────────────────────────────────────────────────────
class PlanCache:
    def __init__(self): self._cache = {}
    def _make_key(self, query: str, dialog_id: str, context_hash: str = "") -> str:
        return hashlib.md5(f"{dialog_id}:{context_hash}:{query}".encode('utf-8')).hexdigest()
    def get(self, query: str, dialog_id: str, context_hash: str = "") -> Optional[Dict]:
        if not CACHE_ENABLED: return None
        key = self._make_key(query, dialog_id, context_hash)
        entry = self._cache.get(key)
        if entry:
            plan, ts = entry
            if time.time() - ts < CACHE_TTL_SEC: return plan
            else: del self._cache[key]
        return None
    def set(self, query: str, dialog_id: str, plan: Dict, context_hash: str = ""):
        if not CACHE_ENABLED: return
        key = self._make_key(query, dialog_id, context_hash)
        self._cache[key] = (plan, time.time())
        if len(self._cache) > MAX_CACHE_ENTRIES:
            sorted_items = sorted(self._cache.items(), key=lambda x: x[1][1])
            for k, _ in sorted_items[:MAX_CACHE_ENTRIES // 2]: del self._cache[k]
    def clear(self, dialog_id: str = None):
        if dialog_id:
            to_delete = [k for k, v in self._cache.items() if k.startswith(hashlib.md5(f"{dialog_id}:".encode()).hexdigest()[:8])]
            for k in to_delete: del self._cache[k]
        else: self._cache.clear()

_plan_cache = PlanCache()

# ─── Dynamic Tool Registry (добавлены когнитивные инструменты) ───────────────
def _resolve_tool(module_name: str, func_name: str):
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, func_name, None)
    except Exception as e:
        _log(f"[Orchestrator] Failed to load {module_name}.{func_name}: {e}")
        return None

class TaskPlanner:
    def __init__(self):
        self.tools = {
            "search_files": _resolve_tool("mcp_fs_search", "search_files"),
            "find_duplicates": _resolve_tool("mcp_fs_search", "find_duplicates"),
            "batch_move": _resolve_tool("mcp_fs_batch", "batch_move"),
            "analyze_directory": _resolve_tool("mcp_fs_search", "analyze_directory"),
            "check_consistency": _resolve_tool("logic_verifier_server", "check_consistency"),
            "smart_search": _resolve_tool("mcp_smart_search", "smart_search"),
            "mempalace_search": _resolve_tool("mcp_mempalace", "mempalace_search"),
            "conversation_memory_query": lambda query="", **kw: conversation_memory.query(query=query, limit=10, hours=24, dialog=dialog_ctx.get()),
            # Когнитивные инструменты
            "hyp_create_hypothesis": _resolve_tool("mcp_hypothesis_engine", "hyp_create_hypothesis"),
            "hyp_add_evidence": _resolve_tool("mcp_hypothesis_engine", "hyp_add_evidence"),
            "hyp_list_hypotheses": _resolve_tool("mcp_hypothesis_engine", "hyp_list_hypotheses"),
            "hyp_verify_now": _resolve_tool("mcp_hypothesis_engine", "hyp_verify_now"),
            "world_add_rule": _resolve_tool("mcp_world_model", "world_add_rule"),
            "world_list_rules": _resolve_tool("mcp_world_model", "world_list_rules"),
            "world_run_inference": _resolve_tool("mcp_world_model", "world_run_inference"),
            "world_get_predictions": _resolve_tool("mcp_world_model", "world_get_predictions"),
            "planning_create_plan": _resolve_tool("mcp_planning_engine", "planning_create_plan"),
            "planning_get_plan": _resolve_tool("mcp_planning_engine", "planning_get_plan"),
            "planning_list_plans": _resolve_tool("mcp_planning_engine", "planning_list_plans"),
            "planning_abort_plan": _resolve_tool("mcp_planning_engine", "planning_abort_plan"),
            "planning_replan": _resolve_tool("mcp_planning_engine", "planning_replan"),
            "goal_create": _resolve_tool("mcp_goal_manager", "goal_create"),
            "goal_update": _resolve_tool("mcp_goal_manager", "goal_update"),
            "goal_get": _resolve_tool("mcp_goal_manager", "goal_get"),
            "goal_list": _resolve_tool("mcp_goal_manager", "goal_list"),
            "goal_delete": _resolve_tool("mcp_goal_manager", "goal_delete"),
            "goal_stats": _resolve_tool("mcp_goal_manager", "goal_stats"),
            "run_reflection": _resolve_tool("mcp_reflection_engine", "run_reflection"),
            "get_reflections": _resolve_tool("mcp_reflection_engine", "get_reflections"),
            "task_submit": _resolve_tool("mcp_task_manager", "submit_task"),
            "task_status": _resolve_tool("mcp_task_manager", "task_status"),
            "task_list": _resolve_tool("mcp_task_manager", "task_list"),
            # Прямая запись текста в файл (без RAG)
            "write_text_to_file": self._write_text_to_file,
        }
        if FS_ADV_AVAILABLE:
            self.tools.update({
                "extract_text_from_file": fs_adv.extract_text_from_file,
                "extract_text_from_folder": fs_adv.extract_text_from_folder,
                "batch_move_files": fs_adv.batch_move_files,
                "batch_copy_files": fs_adv.batch_copy_files,
                "batch_delete_files": fs_adv.batch_delete_files,
                "version_file": fs_adv.version_file,
                "list_versions": fs_adv.list_versions,
                "index_all_files_content": fs_adv.index_all_files_content,
                "search_all_indexed_files": fs_adv.search_all_indexed_files,
            })
        self.command_blacklist = {"delete_all", "format_disk", "shutdown_system"}

    def is_allowed_command(self, tool_name: str) -> bool:
        return tool_name not in self.command_blacklist

    # --------------------- Вспомогательный метод записи текста в файл -----------------
    def _write_text_to_file(self, content: str, file_path: str, append: bool = False, encoding: str = "utf-8") -> Dict:
        """Записать текстовое содержимое в файл. Возвращает статус и путь."""
        try:
            # Создаём директорию, если её нет
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            mode = "a" if append else "w"
            with open(file_path, mode, encoding=encoding) as f:
                f.write(content)
            return {"status": "success", "file_path": file_path, "bytes_written": len(content.encode(encoding))}
        except Exception as e:
            return {"status": "error", "file_path": file_path, "error": str(e)}

    # --------------------- Расширенный parse_intent -------------------------
    def parse_intent(self, query: str) -> Dict:
        query_lower = query.lower()
        plan = {"intent": "unknown", "steps": [], "params": {}, "confidence": 0.0}

        # ─── Search intent detection (оригинал) ─────────────────────────────────
        search_keywords = ["найди", "поиск", "ищи", "какая информация", "найти", "search", "find"]
        if any(kw in query_lower for kw in search_keywords):
            plan["intent"] = "search"
            plan["params"]["query"] = query
            if is_online():
                plan["steps"].append({
                    "tool": "smart_search",
                    "args": {"query": query, "sources": ["web", "kb", "memory"], "limit": 5}
                })
            else:
                plan["steps"].append({
                    "tool": "search_all_indexed_files",
                    "args": {"path": ".", "query": query, "limit": 10}
                })
                plan["steps"].append({
                    "tool": "mempalace_search",
                    "args": {"query": query, "mode": "kb", "limit": 5}
                })
            plan["confidence"] = 0.9

        elif "duplicate" in query_lower or "dupes" in query_lower or "дубликаты" in query_lower:
            plan["intent"] = "find_duplicates"
            plan["params"]["path"] = self._extract_path(query) or "."
            plan["steps"].append({"tool": "find_duplicates", "args": {"path": plan["params"]["path"]}})
            plan["confidence"] = 0.9

        # ─── Hypothesis Engine intent ─────────────────────────────────────
        elif any(phrase in query_lower for phrase in [
            "гипотеза", "предположение", "проверить гипотезу", "создать гипотезу",
            "hypothesis", "what if", "maybe", "perhaps"
        ]):
            plan["intent"] = "hypothesis"
            statement = query
            confidence = 0.5
            plan["params"] = {"statement": statement, "confidence": confidence}
            plan["steps"].append({
                "tool": "hyp_create_hypothesis",
                "args": {"statement": statement, "confidence": confidence, "verification_plan": []}
            })
            plan["confidence"] = 0.8

        # ─── World Model intent ───────────────────────────────────────────
        elif any(phrase in query_lower for phrase in [
            "правило", "если то", "вывести", "предсказать", "логический вывод",
            "rule", "infer", "predict", "consequence"
        ]):
            plan["intent"] = "world_model"
            if "если" in query_lower and "то" in query_lower:
                parts = query_lower.split("то")
                condition = parts[0].replace("если", "").strip()
                conclusion = parts[1].strip()
            else:
                condition = query
                conclusion = ""
            plan["params"] = {"condition": condition, "conclusion": conclusion}
            plan["steps"].append({
                "tool": "world_add_rule",
                "args": {"condition": condition, "conclusion": conclusion}
            })
            plan["confidence"] = 0.8

        # ─── Planning Engine intent ───────────────────────────────────────
        elif any(phrase in query_lower for phrase in [
            "план", "спланируй", "распланировать", "шаги", "последовательность",
            "plan", "steps", "how to"
        ]):
            plan["intent"] = "planning"
            goal = query
            plan["params"] = {"goal_text": goal}
            # Сначала создаём цель, затем план. goal_create возвращает goal_id,
            # который подставляется в следующий шаг через {{goal_id}} (см. _substitute_vars).
            plan["steps"].append({
                "tool": "goal_create",
                "args": {"title": goal[:50], "description": goal, "goal_type": "composite"}
            })
            plan["steps"].append({
                "tool": "planning_create_plan",
                "args": {"goal_id": "{{goal_id}}"}   # будет подставлено после выполнения первого шага
            })
            plan["confidence"] = 0.9

        # ─── Goal Manager intent ──────────────────────────────────────────
        elif any(phrase in query_lower for phrase in [
            "цель", "достичь", "достижение", "задача", "поставить цель",
            "goal", "objective", "milestone"
        ]):
            plan["intent"] = "goal"
            plan["params"]["title"] = query[:50]
            plan["params"]["description"] = query
            plan["steps"].append({
                "tool": "goal_create",
                "args": {"title": query[:50], "description": query, "goal_type": "atomic"}
            })
            plan["confidence"] = 0.85

        # ─── Reflection intent ────────────────────────────────────────────
        elif any(phrase in query_lower for phrase in [
            "противоречие", "рефлексия", "анализ памяти", "найти конфликт",
            "reflection", "contradiction", "analyze memory"
        ]):
            plan["intent"] = "reflection"
            plan["steps"].append({"tool": "run_reflection", "args": {"limit": 100}})
            plan["steps"].append({"tool": "get_reflections", "args": {"limit": 20}})
            plan["confidence"] = 0.8

        # ─── Existing intents (cleanup, organize, etc.) остаются без изменений ──
        elif "clean" in query_lower or "delete" in query_lower or "remove" in query_lower or "удали" in query_lower:
            if "все" in query_lower or "*" in query_lower or "файлы" in query_lower:
                pass
            else:
                plan["intent"] = "cleanup"
                plan["params"]["path"] = self._extract_path(query) or "."
                plan["steps"].append({"tool": "analyze_directory", "args": {"path": plan["params"]["path"]}})
                plan["confidence"] = 0.8

        elif "move" in query_lower or "organize" in query_lower or "sort" in query_lower or "перемести" in query_lower:
            if "из" in query_lower and "в" in query_lower:
                pass
            else:
                plan["intent"] = "organize"
                plan["params"]["source"] = self._extract_path(query) or "."
                plan["steps"].append({"tool": "search_files", "args": {"path": plan["params"]["source"], "pattern": "*"}})
                plan["confidence"] = 0.85

        # ─── Advanced FS Tools intents (оставляем без изменений) ───────────────
        elif "извлеки текст из папки" in query_lower or "прочитай все файлы в папке" in query_lower:
            folder_path = self._extract_path(query)
            if not folder_path:
                plan["intent"] = "error"
                plan["error"] = "Не указана папка"
                plan["confidence"] = 0.0
                return plan
            plan["intent"] = "extract_folder"
            plan["params"]["folder_path"] = folder_path
            plan["steps"].append({"tool": "extract_text_from_folder", "args": {"folder_path": folder_path}})
            plan["confidence"] = 0.85

        elif any(kw in query_lower for kw in ["верси", "бэкап", "backup", "version", "сохрани копию", "резервную", "скопируй версию"]):
            file_path = self._extract_path(query)
            if not file_path:
                plan["intent"] = "error"
                plan["error"] = "Не удалось определить путь к файлу"
                plan["confidence"] = 0.0
                return plan
            plan["intent"] = "versioning"
            plan["params"]["file_path"] = file_path
            plan["steps"].append({"tool": "version_file", "args": {"file_path": file_path}})
            plan["confidence"] = 0.85

        elif any(kw in query_lower for kw in ["извлеки текст", "прочитай содержимое", "extract text", "что внутри"]):
            file_path = self._extract_path(query)
            if not file_path:
                plan["intent"] = "error"
                plan["error"] = "Не удалось определить путь к файлу"
                plan["confidence"] = 0.0
                return plan
            plan["intent"] = "extract"
            plan["params"]["file_path"] = file_path
            plan["steps"].append({"tool": "extract_text_from_file", "args": {"file_path": file_path}})
            plan["confidence"] = 0.9

        elif any(kw in query_lower for kw in ["проиндексируй", "обнови индекс", "индекс файлов", "index files", "индексируй"]):
            plan["intent"] = "index_content"
            path = self._extract_path(query) or os.getcwd()
            plan["params"]["path"] = path
            plan["steps"].append({"tool": "index_all_files_content", "args": {"path": path}})
            plan["confidence"] = 0.85

        elif any(phrase in query_lower for phrase in ["найди в индексе", "fts", "быстрый поиск", "полнотекстовый"]):
            search_term = ""
            for kw in ["найди в индексе", "fts", "быстрый поиск", "полнотекстовый"]:
                if kw in query_lower:
                    search_term = query_lower.split(kw)[-1].strip()
                    break
            if not search_term:
                match = re.search(r'"([^"]+)"', query)
                if match:
                    search_term = match.group(1)
            if not search_term:
                plan["intent"] = "error"
                plan["error"] = "Не указан поисковый запрос."
                plan["confidence"] = 0.0
                return plan
            folder_path = self._extract_path(query) or "."
            plan["intent"] = "index_search"
            plan["params"]["path"] = folder_path
            plan["params"]["query"] = search_term
            plan["steps"].append({
                "tool": "search_all_indexed_files",
                "args": {"path": folder_path, "query": search_term}
            })
            plan["confidence"] = 0.9

        elif ("перемести" in query_lower or "переместить" in query_lower or "batch move" in query_lower) and "в" in query_lower:
            paths = self._extract_paths(query)
            if len(paths) >= 2:
                src, dst = paths[0], paths[1]
                plan["intent"] = "batch_move"
                plan["params"].update({"source": src, "target": dst})
                plan["steps"].append({
                    "tool": "batch_move_files",
                    "args": {"source_folder": src, "pattern": "*", "target_dir": dst, "dry_run": False}
                })
                plan["confidence"] = 0.8
            else:
                plan["intent"] = "error"
                plan["error"] = "Укажите исходную и целевую папки"
                plan["confidence"] = 0.0
                return plan

        elif ("скопируй" in query_lower or "копировать" in query_lower or "batch copy" in query_lower) and "в" in query_lower:
            paths = self._extract_paths(query)
            if len(paths) >= 2:
                src, dst = paths[0], paths[1]
                plan["intent"] = "batch_copy"
                plan["params"].update({"source": src, "target": dst})
                plan["steps"].append({
                    "tool": "batch_copy_files",
                    "args": {"source_folder": src, "pattern": "*", "target_dir": dst, "dry_run": False}
                })
                plan["confidence"] = 0.8
            else:
                plan["intent"] = "error"
                plan["error"] = "Укажите исходную и целевую папки"
                plan["confidence"] = 0.0
                return plan

        elif ("удали" in query_lower or "очисти" in query_lower or "batch delete" in query_lower):
            path = self._extract_path(query)
            match = re.search(r'\*\.[a-zA-Z0-9]+', query)
            pattern = match.group(0) if match else "*"
            if path:
                plan["intent"] = "batch_delete"
                plan["params"].update({"source": path, "pattern": pattern})
                plan["steps"].append({
                    "tool": "batch_delete_files",
                    "args": {"source_folder": path, "pattern": pattern, "dry_run": False, "use_trash": True}
                })
                plan["confidence"] = 0.8
            else:
                plan["intent"] = "error"
                plan["error"] = "Не удалось определить папку для удаления."
                plan["confidence"] = 0.0
                return plan

        elif any(kw in query_lower for kw in [
            "код", "функция", "класс", "метод", "алгоритм", "реализация",
            "исходник", "library", "api", "пример использования", "напиши функцию",
            "как реализовать", "пример кода", "фрагмент кода", "исходный код"
        ]):
            plan["intent"] = "code_search"
            plan["params"]["query"] = query
            plan["steps"].append({"tool": "mempalace_search", "args": {"query": query, "mode": "code", "limit": 5}})
            plan["steps"].append({"tool": "smart_search", "args": {"query": query, "sources": ["web", "kb"], "limit": 3}})
            plan["confidence"] = 0.95

        # ─── NEW: Detecting large text saving (save to file) ─────────────────────────────
        # Срабатывает при очень длинном сообщении (>5000 символов) или явной команде сохранения
        is_long = len(query) > 5000
        is_save_command = any(word in query_lower for word in ["сохрани", "запиши", "файл", "текст", "save", "write", "сохранить", "записать"])
        if (is_long or is_save_command) and plan.get("intent") == "unknown":
            # Извлечение пути к файлу
            file_path = self._extract_path(query)
            if not file_path:
                # Попробовать найти фразы "в файл", "в файле", "файл:", "сохранить как"
                match = re.search(r'(?:в файл|в файле|файл:|сохранить как)\s+([^\s]+)', query_lower)
                if match:
                    candidate = match.group(1)
                    if re.match(r'^[A-Za-z]:\\', candidate) or '/' in candidate:
                        file_path = candidate
            if not file_path:
                import time
                timestamp = int(time.time())
                file_path = f"./saved_text_{timestamp}.txt"

            # Извлечение текстового содержимого
            content = None
            # Сначала пробуем взять содержимое в тройных кавычках или одинарных/двойных
            triple_quote_pattern = r'(""".*?"""|\'\'\'.*?\'\'\')'
            triple_match = re.search(triple_quote_pattern, query, re.DOTALL)
            if triple_match:
                content = triple_match.group(1).strip('"\'')
            else:
                # Ищем после ключевых слов-разделителей: "текст:", "содержимое:", "сохрани:"
                separators = [r'текст:', r'содержимое:', r'сохрани:', r'write:', r'save:', r'text:']
                for sep in separators:
                    parts = re.split(sep, query_lower, maxsplit=1)
                    if len(parts) > 1:
                        content = parts[1].strip()
                        break
            if not content:
                # Если не нашли явный разделитель, но команда сохранения - берём весь запрос,
                # убирая из него саму команду (первые 2-3 слова)
                words = query.split()
                cmd_words = ["сохрани", "запиши", "сохранить", "записать", "save", "write"]
                i = 0
                while i < len(words) and i < 3 and words[i].lower() in cmd_words:
                    i += 1
                content = " ".join(words[i:]).strip()
            if not content:
                plan["intent"] = "error"
                plan["error"] = "Не удалось извлечь текст для сохранения."
                plan["confidence"] = 0.0
                return plan

            # Логируем информацию о сохранении
            if len(content) > 200:
                _log(f"[Orchestrator] Saving large text ({len(content)} chars) to {file_path}")
            else:
                _log(f"[Orchestrator] Saving text to {file_path}: {content[:100]}")

            plan["intent"] = "save_large_text"
            plan["params"] = {"content": content, "file_path": file_path}
            plan["steps"] = [
                {"tool": "write_text_to_file", "args": {"content": content, "file_path": file_path, "append": False}}
            ]
            plan["confidence"] = 0.95

        else:
            plan["intent"] = "general_search"
            plan["params"]["query"] = query
            plan["steps"].append({"tool": "search_files", "args": {"path": ".", "pattern": f"*{query}*"}})
            plan["confidence"] = 0.6

        return plan

    def refine_plan(self, original_query: str, error_context: str) -> Dict:
        _log(f"[Orchestrator] Refining plan due to: {error_context}")
        if "validation_failed" in error_context.lower():
            return {"intent": "investigate", "steps": [{"tool": "search_files", "args": {"path": ".", "pattern": "*"}}], "params": {}, "confidence": 0.5}
        return self.parse_intent(original_query)

    def _extract_path(self, text: str) -> Optional[str]:
        """Extract first Windows path from text."""
        match = re.search(r'[A-Z]:\\(?:[^\\/:*?"<>\r\n]+\\)*[^\\/:*?"<>\r\n]*', text)
        return match.group(0) if match else None

    def _extract_paths(self, text: str) -> List[str]:
        """Extract all Windows paths from text."""
        return re.findall(r'[A-Z]:\\(?:[^\\/:*?"<>\r\n]+\\)*[^\\/:*?"<>\r\n]*', text)

    def _substitute_vars(self, args: Dict, context: Dict) -> Dict:
        """Рекурсивно заменяет {{key}} на значения из контекста."""
        if not isinstance(args, dict):
            return args
        result = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith('{{') and v.endswith('}}'):
                key = v[2:-2].strip()
                if key in context:
                    result[k] = context[key]
                else:
                    _log(f"[Orchestrator] ⚠️ Unresolved template var '{{{{{key}}}}}' — "
                         f"passing empty value instead of literal")
                    result[k] = ""
            elif isinstance(v, dict):
                result[k] = self._substitute_vars(v, context)
            elif isinstance(v, list):
                result[k] = [self._substitute_vars(item, context) if isinstance(item, dict) else item for item in v]
            else:
                result[k] = v
        return result

    def execute_plan(self, plan: Dict, depth: int = 0) -> Dict:
        results = []
        context = {}
        start_time = time.time()

        for i, step in enumerate(plan["steps"]):
            tool_name = step["tool"]
            args = step.get("args", {})

            # Подстановка переменных из контекста предыдущих шагов
            args = self._substitute_vars(args, context)

            if not is_online() and tool_name in ["smart_search", "web_search", "rag_query"]:
                _log(f"[Orchestrator] ⚠️ Skipping '{tool_name}' due to offline mode.")
                results.append({"step": i + 1, "tool": tool_name, "status": "skipped", "reason": "offline"})
                continue

            func = self.tools.get(tool_name)
            if not func:
                _log(f"[Orchestrator] Tool '{tool_name}' unavailable")
                results.append({"step": i + 1, "tool": tool_name, "error": "Tool not loaded"})
                continue

            service = f"orchestrator_{tool_name}"
            def _call():
                r = func(**args)
                # Часть инструментов (например, async-функции mcp_world_model)
                # возвращают корутину. В синхронном исполнителе её нужно явно
                # выполнить, иначе шаг ложно помечался бы success без эффекта.
                if inspect.iscoroutine(r):
                    return asyncio.run(r)
                return r
            res = safe_call(service, _call)

            # Сохраняем результат в контекст для последующих шагов
            if isinstance(res, dict):
                for key, value in res.items():
                    context[f"step_{i+1}_{key}"] = value
                    context[key] = value   # также сохраняем по прямому имени
                # Если результат содержит task_id, можно добавить специальную обработку
                if "task_id" in res:
                    context["last_task_id"] = res["task_id"]

            if isinstance(res, dict) and "error" in res and res.get("error", "").startswith(("Rate limit", "Сервис")):
                results.append({"step": i + 1, "tool": tool_name, "error": res["error"]})
                if depth < 1 and plan["confidence"] > 0.4:
                    _log("[Orchestrator] Triggering bounded fallback due to rate limiting")
                    new_plan = self.refine_plan(context.get("query", ""), res["error"])
                    fallback_res = self.execute_plan(new_plan, depth=depth + 1)
                    results.append({"step": i + 1, "tool": tool_name, "fallback": fallback_res})
                    break
            elif isinstance(res, dict) and "error" in res:
                results.append({"step": i + 1, "tool": tool_name, "error": res["error"]})
                if depth < 1 and plan["confidence"] > 0.4:
                    new_plan = self.refine_plan(context.get("query", ""), res["error"])
                    fallback_res = self.execute_plan(new_plan, depth=depth + 1)
                    results.append({"step": i + 1, "tool": tool_name, "fallback": fallback_res})
                    break
            else:
                results.append({"step": i + 1, "tool": tool_name, "status": "success", "summary": str(res)[:200]})

            context[tool_name] = res

        elapsed = time.time() - start_time
        return {
            "status": "completed", "plan": plan, "results": results,
            "context_summary": {k: type(v).__name__ for k, v in context.items()},
            "elapsed_sec": round(elapsed, 2)
        }

def _get_context_hash(dialog_id: str) -> str:
    try:
        recent = conversation_memory.query(dialog=dialog_id, limit=20, hours=1)
        if recent:
            combined = " ".join(str(r.get("context", "")) for r in recent)
            return hashlib.md5(combined.encode('utf-8')).hexdigest()[:8]
    except Exception: pass
    return ""

def _llm_plan_query(query: str, dialog_id: str) -> Optional[Dict]:
    try: from mcp_shared import query_llm
    except ImportError: return None
    prompt = f"""You are a task planner. Given the user query, output a JSON plan with "intent", "steps" (each step has "tool" and "args"). Available tools include: search_files, mempalace_search, rag_query, batch_move_files, sync_directories, excel_sort, run_shell, extract_text_from_file, extract_text_from_folder, version_file, index_all_files_content, search_all_indexed_files, hyp_create_hypothesis, world_add_rule, planning_create_plan, goal_create, run_reflection, task_submit, write_text_to_file. Query: "{query}"."""
    response = query_llm(prompt)
    if not response: return None
    try:
        response = response.strip()
        if response.startswith("```json"): response = response[7:]
        if response.endswith("```"): response = response[:-3]
        plan = json.loads(response)
        if isinstance(plan, dict) and "steps" in plan: return plan
    except Exception: pass
    return None

def run_task(natural_language_query: str) -> Dict:
    dialog_id = dialog_ctx.get()
    planner = TaskPlanner()

    cmd_result = parse_command(natural_language_query)
    if cmd_result:
        tool_name, args = cmd_result
        if not planner.is_allowed_command(tool_name):
            return {"status": "error", "message": f"Tool '{tool_name}' is blacklisted", "dialog_id": dialog_id, "query": natural_language_query}
        func = planner.tools.get(tool_name)
        if not func:
            for module_name in ["mcp_fs_search", "mcp_fs_batch", "mcp_fs_operations", "mcp_fs_advanced",
                                "knowledge_base_server", "logic_verifier_server", "mcp_smart_search",
                                "mcp_calendar", "mcp_db_client", "mcp_mempalace",
                                "mcp_hypothesis_engine", "mcp_world_model", "mcp_planning_engine",
                                "mcp_goal_manager", "mcp_reflection_engine", "mcp_task_manager"]:
                try:
                    mod = importlib.import_module(module_name)
                    func = getattr(mod, tool_name, None)
                    if func: break
                except ImportError: continue
        if not func:
            return {"status": "error", "message": f"Tool '{tool_name}' not found.", "dialog_id": dialog_id}
        service = f"command_{tool_name}"
        def _call(): return func(**args)
        result = safe_call(service, _call)
        conversation_memory.add(op=f"command_{tool_name}", paths={"args": args}, status="success" if "error" not in result else "error", dialog=dialog_id, context=f"Direct command: {tool_name}")
        return {"status": "command_executed", "tool": tool_name, "result": result, "dialog_id": dialog_id}

    context_hash = _get_context_hash(dialog_id)
    cached_plan = _plan_cache.get(natural_language_query, dialog_id, context_hash)
    if cached_plan:
        _log(f"[Orchestrator] Using cached plan for: {natural_language_query[:50]}")
        plan = cached_plan
    else:
        plan = planner.parse_intent(natural_language_query)
        if plan.get("confidence", 0) < 0.6:
            llm_plan = _llm_plan_query(natural_language_query, dialog_id)
            if llm_plan:
                plan = llm_plan
                _log(f"[Orchestrator] Using LLM-generated plan for: {natural_language_query[:50]}")
        _plan_cache.set(natural_language_query, dialog_id, plan, context_hash)

    plan["params"]["original_query"] = natural_language_query

    if plan.get("intent") == "error":
        conversation_memory.add(
            op="orchestrate_error", paths={"query": natural_language_query},
            status="error", dialog=dialog_id, context=plan.get("error", "Validation failed")
        )
        return {
            "query": natural_language_query, "dialog_id": dialog_id,
            "intent": "error", "execution": {"status": "failed", "message": plan.get("error")},
            "context_warning": None
        }

    ctx_check = _check_context_history(plan["intent"], plan["params"])
    if ctx_check.get("requires_confirmation"):
        _log(f"[Orchestrator] Context warning: {ctx_check['warning']}")

    execution = planner.execute_plan(plan, depth=0)
    conversation_memory.add(op=f"orchestrate_{plan['intent']}", paths={"query": natural_language_query}, status=execution["status"], dialog=dialog_id, context=f"Executed plan for '{natural_language_query}'. Intent: {plan['intent']}")
    
    return {"query": natural_language_query, "dialog_id": dialog_id, "intent": plan["intent"], "execution": execution, "context_warning": ctx_check if ctx_check.get("requires_confirmation") else None}

def _check_context_history(intent: str, params: Dict) -> Dict:
    dialog_id = dialog_ctx.get()
    history = conversation_memory.query(op=f"orchestrate_{intent}", hours=1, limit=1, dialog=dialog_id)
    if history:
        last_op = history[0]
        if str(params.get("path")) in str(last_op.get("paths", {})) or str(params.get("file_path")) in str(last_op.get("paths", {})):
            return {"warning": "Similar operation performed recently", "last_execution": last_op["ts"], "requires_confirmation": True}
    return {"requires_confirmation": False}

def clear_plan_cache(dialog_id: str = None) -> Dict:
    _plan_cache.clear(dialog_id)
    return {"status": "cleared", "dialog_id": dialog_id or "all"}

def orchestrator_stats() -> Dict:
    return {"cache_enabled": CACHE_ENABLED, "cache_entries": len(_plan_cache._cache), "cache_ttl_sec": CACHE_TTL_SEC, "max_cache_entries": MAX_CACHE_ENTRIES}

server = BaseMCPServer("orchestrator", "3.9")
server.register_tool("run_task", {
    "description": "Execute a complex task described in natural language. Supports !command syntax.",
    "inputSchema": {"type": "object", "properties": {"natural_language_query": {"type": "string"}}, "required": ["natural_language_query"]}
}, lambda **kw: run_task(kw["natural_language_query"]))

server.register_tool("clear_plan_cache", {
    "description": "Clear the orchestration plan cache",
    "inputSchema": {"type": "object", "properties": {"dialog_id": {"type": "string"}}}
}, lambda **kw: clear_plan_cache(kw.get("dialog_id")))

server.register_tool("orchestrator_stats", {
    "description": "Get cache and orchestrator statistics",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: orchestrator_stats())

def rate_limiter_status():
    return {"rate_limiter": {"max_calls": 25, "window_sec": 60}, "circuit_breaker": {"failure_threshold": 5, "recovery_timeout": 45}}

server.register_tool("rate_limiter_status", {
    "description": "Показать текущие настройки rate limiter и circuit breaker",
    "inputSchema": {"type": "object", "properties": {}}
}, rate_limiter_status)

if __name__ == "__main__":
    server.run()