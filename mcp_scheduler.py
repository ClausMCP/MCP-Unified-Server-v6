#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Scheduler v2.0 – миграция на mcp_storage + настоящая поддержка cron

Изменения относительно v1.1 (API всех 7 инструментов сохранён полностью):
  • Все 8 вызовов sqlite3.connect() заменены на mcp_storage — постоянное
    соединение, WAL, busy_timeout, retry. Существующий mcp_scheduler.db
    подхватывается автоматически.
  • ИСПРАВЛЕН БАГ: SchedulerDB() создавался заново в КАЖДОЙ функции
    (scheduler_list, scheduler_delete и т.д.) — по 2 подключения и повторный
    CREATE TABLE на каждый вызов инструмента. Теперь один экземпляр на модуль.
  • ИСПРАВЛЕН БАГ: cron-выражения полностью игнорировались — любая cron-задача
    выполнялась «примерно раз в минуту» независимо от выражения. Теперь
    встроенный разбор стандартного 5-польного cron (мин час день месяц день_недели,
    поддержка * , - / диапазонов и списков) без внешних зависимостей.
    Зависимость от библиотеки schedule удалена — она и была лишь заглушкой.
  • ИСПРАВЛЕН БАГ: обработчик SIGINT «проглатывал» Ctrl+C — планировщик
    останавливался, но процесс продолжал висеть. Теперь после остановки
    процесс корректно завершается.
  • Замыкания в _schedule_*_job держали устаревшую копию job — устранено:
    задача перечитывается из БД перед каждым запуском.
"""
import os
import sys
import json
import threading
import time
import atexit
import signal
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Set

import mcp_storage as storage
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Конфигурация ─────────────────────────────────────────────────────────
_LEGACY_DB = os.environ.get(
    "MCP_SCHEDULER_DB",
    os.path.join(os.path.dirname(__file__), "mcp_scheduler.db")
)
DB_NAME = "scheduler"
DB_PATH = storage.register(DB_NAME, legacy_path=_LEGACY_DB)

CHECK_INTERVAL_SEC = 1   # интервал проверки pending задач (сек)
DEFAULT_INTERVAL_SEC = 3600
CRON_SCAN_LIMIT_DAYS = 366 * 4 + 2   # до 4 лет вперёд (покрывает «0 0 29 2 *»)


# ─── Разбор cron-выражений (без внешних зависимостей) ─────────────────────
def _parse_cron_field(field: str, lo: int, hi: int) -> Set[int]:
    """Разбирает одно поле cron: '*', '*/n', 'a', 'a-b', 'a-b/n', 'a,b,c'."""
    values: Set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"Неверный шаг в cron-поле: {field}")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start < lo or end > hi or start > end:
            raise ValueError(f"Значение вне диапазона [{lo},{hi}]: {field}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> Dict[str, Set[int]]:
    """Разбирает стандартное 5-польное cron-выражение.
    Поля: минута час день_месяца месяц день_недели (0/7 = воскресенье).
    Бросает ValueError при некорректном выражении.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"Ожидается 5 полей cron, получено {len(fields)}: '{expr}'")
    minute, hour, dom, month, dow = fields
    dow_set = _parse_cron_field(dow.replace("7", "0") if dow.strip() == "7" else dow, 0, 7)
    # 7 == 0 == воскресенье
    if 7 in dow_set:
        dow_set.discard(7)
        dow_set.add(0)
    return {
        "minute": _parse_cron_field(minute, 0, 59),
        "hour": _parse_cron_field(hour, 0, 23),
        "dom": _parse_cron_field(dom, 1, 31),
        "month": _parse_cron_field(month, 1, 12),
        "dow": dow_set,
        "dom_star": dom.strip() == "*",
        "dow_star": dow.strip() == "*",
    }


def _cron_matches(parsed: Dict, dt: datetime) -> bool:
    """Проверяет соответствие момента cron-выражению (стандартная семантика:
    если ограничены И день месяца, И день недели — срабатывает по ЛЮБОМУ из них)."""
    if dt.minute not in parsed["minute"]:
        return False
    if dt.hour not in parsed["hour"]:
        return False
    if dt.month not in parsed["month"]:
        return False
    cron_dow = (dt.weekday() + 1) % 7  # Пн=0..Вс=6 -> Вс=0..Сб=6
    dom_ok = dt.day in parsed["dom"]
    dow_ok = cron_dow in parsed["dow"]
    if parsed["dom_star"] and parsed["dow_star"]:
        return True
    if parsed["dom_star"]:
        return dow_ok
    if parsed["dow_star"]:
        return dom_ok
    return dom_ok or dow_ok


def _cron_day_matches(parsed: Dict, dt: datetime) -> bool:
    """Проверка только календарной части (месяц, день месяца, день недели)."""
    if dt.month not in parsed["month"]:
        return False
    cron_dow = (dt.weekday() + 1) % 7
    dom_ok = dt.day in parsed["dom"]
    dow_ok = cron_dow in parsed["dow"]
    if parsed["dom_star"] and parsed["dow_star"]:
        return True
    if parsed["dom_star"]:
        return dow_ok
    if parsed["dow_star"]:
        return dom_ok
    return dom_ok or dow_ok


def cron_next_run(expr: str, after: datetime) -> Optional[datetime]:
    """Ближайший момент срабатывания cron-выражения строго после `after`.

    Подневный алгоритм: неподходящие дни пропускаются целиком, внутри
    подходящего дня выбирается ближайшая пара час:минута. Горизонт — 4 года
    (покрывает редкие выражения вроде «0 0 29 2 *»). None — если выражение
    некорректно или не срабатывает в пределах горизонта.
    """
    try:
        parsed = parse_cron(expr)
    except (ValueError, TypeError) as e:
        _log(f"[Scheduler] Некорректное cron-выражение '{expr}': {e}")
        return None

    hours = sorted(parsed["hour"])
    minutes = sorted(parsed["minute"])
    dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    for _ in range(CRON_SCAN_LIMIT_DAYS):
        if _cron_day_matches(parsed, dt):
            # Ищем ближайшую (час, минута) в этом дне, не раньше dt
            for h in hours:
                if h < dt.hour:
                    continue
                for m in minutes:
                    if h == dt.hour and m < dt.minute:
                        continue
                    return dt.replace(hour=h, minute=m)
        # День не подошёл или все слоты дня уже позади — на следующий день 00:00
        dt = (dt + timedelta(days=1)).replace(hour=0, minute=0)

    _log(f"[Scheduler] Cron '{expr}' не срабатывает в ближайшие 4 года")
    return None


# ─── База данных заданий ─────────────────────────────────────────────────
class SchedulerDB:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self._init_db()

    def _init_db(self):
        storage.executescript(self.db_name, """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                tool_name TEXT NOT NULL,
                tool_args TEXT,
                schedule_type TEXT CHECK(schedule_type IN ('interval', 'cron')) NOT NULL,
                interval_seconds INTEGER,
                cron_expression TEXT,
                enabled INTEGER DEFAULT 1,
                last_run TEXT,
                next_run TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_enabled ON jobs(enabled);
        """)

    def add_job(self, name: str, tool_name: str, tool_args: Dict,
                schedule_type: str, interval_seconds: int = None, cron_expr: str = None) -> int:
        # Идемпотентность: имя задачи уникально. Если задача с таким именем уже
        # есть (например, после перезапуска сервера), обновляем её, а не падаем
        # с UNIQUE constraint failed. next_run сбрасывается — пересчитается в цикле.
        with storage.connection(self.db_name) as conn:
            existing = conn.execute("SELECT id FROM jobs WHERE name = ?", (name,)).fetchone()
            if existing:
                conn.execute("""
                    UPDATE jobs
                       SET tool_name = ?, tool_args = ?, schedule_type = ?,
                           interval_seconds = ?, cron_expression = ?, enabled = 1,
                           next_run = NULL
                     WHERE name = ?
                """, (tool_name, json.dumps(tool_args, default=str), schedule_type,
                      interval_seconds, cron_expr, name))
                return existing[0]
            cur = conn.execute("""
                INSERT INTO jobs (name, tool_name, tool_args, schedule_type, interval_seconds, cron_expression)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (name, tool_name, json.dumps(tool_args, default=str), schedule_type, interval_seconds, cron_expr))
            return cur.lastrowid

    def get_enabled_jobs(self) -> List[Dict]:
        rows = storage.query_all(self.db_name, "SELECT * FROM jobs WHERE enabled = 1", row=True)
        return [dict(row) for row in rows]

    def get_job_by_id(self, job_id: int) -> Optional[Dict]:
        row = storage.query_one(self.db_name, "SELECT * FROM jobs WHERE id = ?", (job_id,), row=True)
        return dict(row) if row else None

    def update_last_run(self, job_id: int, next_run_dt: datetime = None):
        now_iso = datetime.now().isoformat()
        next_iso = next_run_dt.isoformat() if next_run_dt else None
        storage.execute(
            self.db_name,
            "UPDATE jobs SET last_run = ?, next_run = ? WHERE id = ?",
            (now_iso, next_iso, job_id)
        )

    def delete_job(self, job_id: int):
        storage.execute(self.db_name, "DELETE FROM jobs WHERE id = ?", (job_id,))

    def list_jobs(self) -> List[Dict]:
        rows = storage.query_all(self.db_name, "SELECT * FROM jobs ORDER BY id", row=True)
        return [dict(row) for row in rows]

    def enable_job(self, job_id: int, enabled: bool):
        storage.execute(
            self.db_name,
            "UPDATE jobs SET enabled = ?, next_run = NULL WHERE id = ?",
            (1 if enabled else 0, job_id)
        )


# Единственный экземпляр БД на модуль (исправление v2.0: раньше каждая
# функция создавала свой SchedulerDB с повторной инициализацией схемы)
_db = SchedulerDB()


# ─── Исполнитель задач ───────────────────────────────────────────────────
class JobExecutor:
    def __init__(self, db: SchedulerDB = None):
        self.db = db or _db
        self._tools_cache = {}

    def _load_tool(self, tool_name: str):
        if tool_name in self._tools_cache:
            return self._tools_cache[tool_name]
        tool_map = {
            "empty_trash": ("mcp_fs_trash", "empty_trash"),
            "sync_directories": ("mcp_fs_sync", "sync_directories"),
            "remind": ("mcp_calendar", "remind"),
            "batch_delete": ("mcp_fs_batch", "batch_delete"),
            "move_to_trash": ("mcp_fs_trash", "move_to_trash"),
            "archive_files": ("mcp_fs_archives", "archive_files"),
            "extract_archive": ("mcp_fs_archives", "extract_archive"),
            "sync_to_cloud": ("mcp_fs_cloud", "sync_to_cloud"),
            "sync_from_cloud": ("mcp_fs_cloud", "sync_from_cloud"),
            "backup_database": ("knowledge_base_server", "backup_database"),
        }
        if tool_name in tool_map:
            module_name, func_name = tool_map[tool_name]
            try:
                mod = __import__(module_name, fromlist=[func_name])
                func = getattr(mod, func_name)
                self._tools_cache[tool_name] = func
                return func
            except Exception as e:
                _log(f"[Scheduler] Failed to load tool {tool_name}: {e}")
                return None
        else:
            _log(f"[Scheduler] Tool {tool_name} not mapped.")
            return None

    def run_job(self, job: Dict) -> Dict:
        name = job["name"]
        tool_name = job["tool_name"]
        args = json.loads(job["tool_args"]) if job["tool_args"] else {}
        _log(f"[Scheduler] Executing job '{name}': {tool_name}({args})")
        try:
            func = self._load_tool(tool_name)
            if not func:
                raise ValueError(f"Tool '{tool_name}' not found or could not be loaded")
            result = func(**args)
            if not isinstance(result, dict):
                result = {"result": str(result)}
            _log(f"[Scheduler] Job '{name}' completed: {str(result)[:200]}")
            conversation_memory.add(
                op="scheduled_job",
                paths={"job": name, "tool": tool_name},
                status="success",
                dialog="scheduler",
                context=f"Scheduled job '{name}' executed, result: {result.get('status', 'ok')}"
            )
            return result
        except Exception as e:
            _log(f"[Scheduler] Job '{name}' failed: {e}")
            conversation_memory.add(
                op="scheduled_job",
                paths={"job": name, "tool": tool_name},
                status="error",
                dialog="scheduler",
                context=f"Job failed: {e}"
            )
            return {"error": str(e), "job": name}


# ─── Планировщик (фоновый поток) ────────────────────────────────────────
class SchedulerThread:
    """Единый цикл на основе БД: каждую секунду сверяет next_run включённых
    задач с текущим временем. Cron-выражения обрабатываются честно
    (исправление v2.0 — раньше любой cron выполнялся раз в минуту)."""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.executor = JobExecutor(_db)
        self.db = _db

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp_scheduler_loop")
        self._thread.start()
        _log("[Scheduler] Background thread started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _log("[Scheduler] Background thread stopped")

    def _run(self):
        while not self._stop_event.wait(CHECK_INTERVAL_SEC):
            try:
                self._check_pending_jobs()
            except Exception as e:
                _log(f"[Scheduler] Loop error: {e}")
        storage.close_thread()

    def _compute_next_run(self, job: Dict, now: datetime) -> Optional[datetime]:
        if job["schedule_type"] == "interval" and job["interval_seconds"]:
            return now + timedelta(seconds=job["interval_seconds"])
        if job["schedule_type"] == "cron" and job["cron_expression"]:
            return cron_next_run(job["cron_expression"], now)
        return None

    def _check_pending_jobs(self):
        jobs = self.db.get_enabled_jobs()
        now = datetime.now()
        for job in jobs:
            if self._stop_event.is_set():
                return
            next_run_str = job.get("next_run")
            next_run = None
            if next_run_str:
                try:
                    next_run = datetime.fromisoformat(next_run_str)
                except Exception:
                    next_run = None

            if next_run is None:
                # Новая/сброшенная задача: интервальная стартует сразу
                # (поведение v1.1 сохранено), cron ждёт первого совпадения.
                if job["schedule_type"] == "cron" and job["cron_expression"]:
                    nxt = cron_next_run(job["cron_expression"], now)
                    if nxt:
                        # Только планируем (last_run не трогаем — задача ещё не выполнялась)
                        storage.execute(
                            self.db.db_name,
                            "UPDATE jobs SET next_run = ? WHERE id = ?",
                            (nxt.isoformat(), job["id"])
                        )
                    continue
                # интервальная — выполняем немедленно
                self.executor.run_job(job)
                self.db.update_last_run(job["id"], self._compute_next_run(job, now))
                continue

            if now >= next_run:
                # Перечитываем задачу из БД: аргументы могли обновиться
                # (исправление устаревшего замыкания из v1.1)
                fresh = self.db.get_job_by_id(job["id"])
                if not fresh or not fresh["enabled"]:
                    continue
                self.executor.run_job(fresh)
                self.db.update_last_run(fresh["id"], self._compute_next_run(fresh, datetime.now()))


# ─── Глобальный экземпляр планировщика ──────────────────────────────────
_scheduler = SchedulerThread()


def scheduler_start() -> Dict:
    _scheduler.start()
    return {"status": "started"}


def scheduler_stop() -> Dict:
    _scheduler.stop()
    return {"status": "stopped"}


def scheduler_add_interval(name: str, tool_name: str, interval_seconds: int,
                           args: Dict = None) -> Dict:
    args = args or {}
    if not isinstance(interval_seconds, int) or interval_seconds <= 0:
        return {"status": "error", "message": "interval_seconds должен быть положительным целым"}
    job_id = _db.add_job(name, tool_name, args, "interval", interval_seconds=interval_seconds)
    return {"status": "created", "job_id": job_id, "name": name}


def scheduler_add_cron(name: str, tool_name: str, cron_expression: str,
                       args: Dict = None) -> Dict:
    args = args or {}
    # Валидация выражения ДО записи в БД (v2.0)
    try:
        parse_cron(cron_expression)
    except (ValueError, TypeError) as e:
        return {"status": "error", "message": f"Некорректное cron-выражение: {e}"}
    job_id = _db.add_job(name, tool_name, args, "cron", cron_expr=cron_expression)
    nxt = cron_next_run(cron_expression, datetime.now())
    return {"status": "created", "job_id": job_id, "name": name,
            "next_run": nxt.isoformat() if nxt else None}


def scheduler_list() -> Dict:
    jobs = _db.list_jobs()
    return {"jobs": jobs, "count": len(jobs)}


def scheduler_delete(job_id: int) -> Dict:
    _db.delete_job(job_id)
    return {"status": "deleted", "job_id": job_id}


def scheduler_enable(job_id: int, enabled: bool) -> Dict:
    _db.enable_job(job_id, enabled)
    return {"status": "updated", "job_id": job_id, "enabled": enabled}


# ─── Graceful Shutdown ───────────────────────────────────────────────────
def _shutdown_scheduler():
    try:
        _scheduler.stop()
    except Exception as e:
        _log(f"[Scheduler] Shutdown error: {e}")


def _signal_handler(signum, frame):
    # Исправление v2.0: раньше обработчик «проглатывал» сигнал и процесс
    # продолжал висеть после Ctrl+C. Теперь останавливаемся и завершаемся.
    _shutdown_scheduler()
    sys.exit(0)


atexit.register(_shutdown_scheduler)
try:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
except Exception:
    pass  # signal доступен только в главном потоке / не во всех окружениях


# ─── Регистрация инструментов MCP ───────────────────────────────────────
def register_tools(server: BaseMCPServer):
    server.register_tool("scheduler_start", {
        "description": "Запустить фоновый планировщик задач",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: scheduler_start())

    server.register_tool("scheduler_stop", {
        "description": "Остановить планировщик задач",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: scheduler_stop())

    server.register_tool("scheduler_add_interval", {
        "description": "Добавить задание с интервалом в секундах",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "tool_name": {"type": "string"},
                "interval_seconds": {"type": "integer"},
                "args": {"type": "object"}
            },
            "required": ["name", "tool_name", "interval_seconds"]
        }
    }, lambda **kw: scheduler_add_interval(
        kw["name"], kw["tool_name"], kw["interval_seconds"], kw.get("args", {})
    ))

    server.register_tool("scheduler_add_cron", {
        "description": "Добавить задание с cron-выражением (5 полей: мин час день месяц день_недели)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "tool_name": {"type": "string"},
                "cron_expression": {"type": "string"},
                "args": {"type": "object"}
            },
            "required": ["name", "tool_name", "cron_expression"]
        }
    }, lambda **kw: scheduler_add_cron(
        kw["name"], kw["tool_name"], kw["cron_expression"], kw.get("args", {})
    ))

    server.register_tool("scheduler_list", {
        "description": "Список всех заданий",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: scheduler_list())

    server.register_tool("scheduler_delete", {
        "description": "Удалить задание по ID",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "integer"}},
            "required": ["job_id"]
        }
    }, lambda **kw: scheduler_delete(kw["job_id"]))

    server.register_tool("scheduler_enable", {
        "description": "Включить или выключить задание",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer"},
                "enabled": {"type": "boolean", "default": True}
            },
            "required": ["job_id"]
        }
    }, lambda **kw: scheduler_enable(kw["job_id"], kw.get("enabled", True)))


__mcp_plugin__ = {
    "name": "scheduler",
    "version": "2.0",
    "description": "Планировщик задач (интервалы и настоящий cron, mcp_storage)",
    "dependencies": [],
    "on_load": lambda: _log("[Scheduler] Plugin v2.0 loaded (mcp_storage, real cron)."),
    "on_unload": lambda: scheduler_stop()
}

if __name__ == "__main__":
    server = BaseMCPServer("scheduler", "2.0")
    register_tools(server)
    server.run()
