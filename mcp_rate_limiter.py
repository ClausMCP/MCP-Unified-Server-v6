#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Rate Limiter + Circuit Breaker v2.0

Изменения относительно v1.3.2 (API сохранён: rate_limiter, circuit_breaker,
safe_call, cleanup_old_records — все сигнатуры и форматы ответов прежние):
  • Все 7 вызовов sqlite3.connect() заменены на mcp_storage.
  • ИСПРАВЛЕН КРИТИЧЕСКИЙ БАГ: в БД записывался time.monotonic(), который
    бессмыслен между процессами — после перезапуска окна лимитера и таймеры
    восстановления Circuit Breaker вели себя непредсказуемо. Теперь везде
    time.time(). Старые monotonic-значения выглядят «древними» и вычищаются
    первой же очисткой — миграция не нужна.
  • ИСПРАВЛЕН КРИТИЧЕСКИЙ БАГ: cleanup_old_records сравнивал monotonic-значения
    (~тысячи секунд) с cutoff от time.time() (~1.75 млрд) — условие было
    истинно ВСЕГДА, и вся таблица стиралась при каждом запуске процесса.
    «Персистентный» лимитер фактически ничего не сохранял.
  • ИСПРАВЛЕН БАГ: REPLACE INTO (service, timestamps) удалял строку целиком
    и вставлял заново с failures=0 — каждый разрешённый вызов обнулял счётчик
    отказов Circuit Breaker. Теперь UPSERT сохраняет остальные колонки.
  • ИСПРАВЛЕН БАГ: record_success/record_failure были no-op, если строки ещё
    нет (UPDATE без строки). Теперь UPSERT создаёт строку при необходимости.
  • Путь БД был относительным (Path("mcp_rate_limiter.db")) — файл создавался
    в текущем рабочем каталоге процесса. Теперь путь стабилен: рядом с модулем
    (если legacy-файл существует) или в MCP_STORAGE_DIR.
  • Фоновые потоки (health-check и очистка) стали останавливаемыми и
    защищены от дублирования при повторном импорте.
"""

import os
import time
import threading
import json
from pathlib import Path

import mcp_storage as storage
from mcp_shared import _log

# ─── Конфигурация ─────────────────────────────────────────────────────────
# Legacy-кандидаты: рядом с модулем и в текущем каталоге (старое поведение)
_module_dir_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_rate_limiter.db")
_cwd_db = os.path.abspath("mcp_rate_limiter.db")
_legacy = _module_dir_db if os.path.isfile(_module_dir_db) else _cwd_db

DB_NAME = "rate_limiter"
DB_PATH = Path(storage.register(DB_NAME, legacy_path=_legacy))  # имя сохранено для совместимости

HEALTH_CHECK_INTERVAL_SEC = int(os.environ.get("MCP_RL_HEALTH_INTERVAL", "300"))
CLEANUP_INTERVAL_SEC = int(os.environ.get("MCP_RL_CLEANUP_INTERVAL", "21600"))


def init_db():
    storage.executescript(DB_NAME, """
        CREATE TABLE IF NOT EXISTS rate_history (
            service TEXT PRIMARY KEY,
            timestamps TEXT,
            failures INTEGER DEFAULT 0,
            last_failure REAL DEFAULT 0,
            last_success REAL DEFAULT 0
        )
    """)


init_db()


class PersistentRateLimiter:
    def __init__(self, max_calls: int = 25, window_sec: float = 60.0):
        self.max_calls = max_calls
        self.window = window_sec
        self.lock = threading.Lock()

    def allow(self, service: str) -> tuple[bool, float | None]:
        now = time.time()  # v2.0: wall-clock — переживает перезапуск процесса
        with self.lock:
            with storage.connection(DB_NAME) as conn:
                row = conn.execute(
                    "SELECT timestamps FROM rate_history WHERE service = ?", (service,)
                ).fetchone()

                timestamps = json.loads(row[0]) if row and row[0] else []
                timestamps = [ts for ts in timestamps if ts > now - self.window]

                if len(timestamps) >= self.max_calls:
                    retry_after = (timestamps[0] + self.window - now) if timestamps else self.window
                    return False, round(max(0.0, retry_after), 1)

                timestamps.append(now)
                # UPSERT: не трогаем failures/last_failure/last_success
                # (исправление v2.0 — REPLACE обнулял счётчик отказов)
                conn.execute("""
                    INSERT INTO rate_history (service, timestamps) VALUES (?, ?)
                    ON CONFLICT(service) DO UPDATE SET timestamps = excluded.timestamps
                """, (service, json.dumps(timestamps)))
                return True, None

    def reset(self, service: str = None) -> dict:
        """Сбрасывает окно лимитера и счётчик отказов для сервиса (или всех).

        Добавлено в v2.0: инструмент rate_limiter_reset в mcp_fs_server
        вызывал этот метод, но в v1.3.2 он не существовал — вызов падал
        с AttributeError при каждом использовании.
        """
        with self.lock:
            if service:
                deleted = storage.execute(
                    DB_NAME, "DELETE FROM rate_history WHERE service = ?", (service,))
            else:
                deleted = storage.execute(DB_NAME, "DELETE FROM rate_history")
        _log(f"[RateLimiter] Reset: service={service or 'ALL'}, rows={deleted}")
        return {"reset": service or "all", "rows": deleted}


class EnhancedCircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 45.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._health_thread = None
        self._start_health_check()

    def _start_health_check(self):
        if self._health_thread is not None and self._health_thread.is_alive():
            return
        def health_check_loop():
            while not self._stop_event.wait(HEALTH_CHECK_INTERVAL_SEC):
                try:
                    self._perform_health_check()
                except Exception as e:
                    _log(f"[HealthCheck] Ошибка: {e}")
            storage.close_thread()
        self._health_thread = threading.Thread(
            target=health_check_loop, daemon=True, name="rl_health_check")
        self._health_thread.start()

    def stop(self, timeout: float = 5.0):
        """Корректная остановка фонового health-check (v2.0)."""
        self._stop_event.set()
        if self._health_thread is not None:
            self._health_thread.join(timeout=timeout)

    def _perform_health_check(self):
        now = time.time()
        with storage.connection(DB_NAME) as conn:
            rows = conn.execute(
                "SELECT service, failures, last_failure FROM rate_history"
            ).fetchall()
            for service, failures, last_failure in rows:
                if failures >= self.failure_threshold:
                    if now - (last_failure or 0) > self.recovery_timeout * 2:
                        conn.execute(
                            "UPDATE rate_history SET failures = 0 WHERE service = ?",
                            (service,)
                        )
                        _log(f"[HealthCheck] Сервис {service} восстановлен")

    def can_execute(self, service: str) -> bool:
        row = storage.query_one(
            DB_NAME,
            "SELECT failures, last_failure FROM rate_history WHERE service = ?",
            (service,)
        )
        if not row:
            return True
        failures, last_failure = row
        if failures < self.failure_threshold:
            return True
        # Если превышен порог, проверяем, не прошло ли время восстановления
        if time.time() - (last_failure or 0) > self.recovery_timeout:
            return True
        return False

    def record_success(self, service: str):
        # UPSERT: создаёт строку, если сервис ещё не встречался (v2.0)
        storage.execute(DB_NAME, """
            INSERT INTO rate_history (service, failures, last_success) VALUES (?, 0, ?)
            ON CONFLICT(service) DO UPDATE SET failures = 0, last_success = excluded.last_success
        """, (service, time.time()))

    def record_failure(self, service: str):
        storage.execute(DB_NAME, """
            INSERT INTO rate_history (service, failures, last_failure) VALUES (?, 1, ?)
            ON CONFLICT(service) DO UPDATE SET
                failures = rate_history.failures + 1,
                last_failure = excluded.last_failure
        """, (service, time.time()))


# ====================== ГЛОБАЛЬНЫЕ ЭКЗЕМПЛЯРЫ ======================
rate_limiter = PersistentRateLimiter(max_calls=25, window_sec=60)
circuit_breaker = EnhancedCircuitBreaker(failure_threshold=5, recovery_timeout=45)


def safe_call(service: str, func, *args, **kwargs):
    allowed, retry_after = rate_limiter.allow(service)

    if not allowed:
        return {
            "error": f"Rate limit exceeded for {service}",
            "retry_after": retry_after,
            "message": f"Повторите через {retry_after} секунд"
        }

    if not circuit_breaker.can_execute(service):
        return {
            "error": f"Сервис {service} временно отключён (Circuit Breaker)",
            "retry_after": 30
        }

    try:
        result = func(*args, **kwargs)
        circuit_breaker.record_success(service)
        return result
    except Exception as e:
        circuit_breaker.record_failure(service)
        return {"error": str(e), "service": service}


# ====================== Очистка БД ======================
def cleanup_old_records(max_age_hours: int = 24):
    """Очистка старых записей.

    Исправление v2.0: раньше cutoff от time.time() сравнивался с monotonic-
    значениями в БД — условие было истинно всегда, и таблица стиралась целиком
    при каждом запуске. Теперь единицы согласованы (везде time.time()),
    и NULL/0 больше не считаются «старыми», если запись свежая по другой метке.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    try:
        deleted = storage.execute(DB_NAME, """
            DELETE FROM rate_history
            WHERE COALESCE(last_failure, 0) < ?
              AND COALESCE(last_success, 0) < ?
        """, (cutoff, cutoff))
        if deleted > 0:
            _log(f"[DB Cleanup] Удалено {deleted} старых записей rate limiter")
    except Exception as e:
        _log(f"[DB Cleanup] Ошибка очистки: {e}")


# Запуск очистки при старте (вычистит в т.ч. старые monotonic-записи)
cleanup_old_records()

_cleanup_stop = threading.Event()
_cleanup_thread = None
_cleanup_lock = threading.Lock()


def start_cleanup_scheduler():
    """Планировщик очистки (идемпотентный запуск, v2.0)."""
    global _cleanup_thread
    with _cleanup_lock:
        if _cleanup_thread is not None and _cleanup_thread.is_alive():
            return
        _cleanup_stop.clear()
        def loop():
            while not _cleanup_stop.wait(CLEANUP_INTERVAL_SEC):
                cleanup_old_records()
            storage.close_thread()
        _cleanup_thread = threading.Thread(target=loop, daemon=True, name="rl_cleanup")
        _cleanup_thread.start()


def stop_cleanup_scheduler(timeout: float = 5.0):
    """Корректная остановка планировщика очистки (v2.0)."""
    _cleanup_stop.set()
    with _cleanup_lock:
        if _cleanup_thread is not None:
            _cleanup_thread.join(timeout=timeout)


start_cleanup_scheduler()

_log("[RateLimiter] Инициализирован v2.0 (mcp_storage, wall-clock, UPSERT)")
