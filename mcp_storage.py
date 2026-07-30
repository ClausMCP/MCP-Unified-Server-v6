#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Storage v1.0 — единый ConnectionManager для SQLite
═══════════════════════════════════════════════════════

Заменяет ~100 разрозненных вызовов sqlite3.connect() во всех модулях проекта.

Что даёт:
  • Единые PRAGMA (WAL, busy_timeout, synchronous=NORMAL, foreign_keys)
    для ВСЕХ баз — раньше каждый модуль настраивал (или забывал настроить) сам.
  •持 Постоянные thread-local соединения вместо открытия/закрытия на каждый
    запрос — устраняет накладные расходы и гонки при 38 фоновых потоках.
  • Автоматический retry при «database is locked» с экспоненциальным backoff.
  • Реестр логических имён БД: модуль пишет storage.connection("dialogs"),
    а не хардкодит путь. Консолидация файлов в storage/ происходит
    прозрачно, БЕЗ потери legacy-данных (если старый файл существует —
    используется он).
  • Корректное закрытие всех соединений при завершении (atexit) и
    ручное закрытие для рабочих потоков (close_thread()).

Зависимости: только стандартная библиотека. НЕ импортирует mcp_shared
(модуль-фундамент, его импортируют все остальные).

Использование:
    import mcp_storage as storage

    # 1. Регистрация БД (один раз при старте модуля):
    storage.register("dialogs", legacy_path=os.environ.get("MCP_DIALOG_DB"))

    # 2. Транзакция (commit при успехе, rollback при исключении):
    with storage.connection("dialogs") as conn:
        conn.execute("INSERT INTO t VALUES (?)", (x,))

    # 3. Быстрые хелперы:
    row  = storage.query_one("dialogs", "SELECT name FROM dialogs WHERE id=?", (i,))
    rows = storage.query_all("dialogs", "SELECT * FROM dialogs", row=True)
    storage.execute("dialogs", "DELETE FROM dialogs WHERE deleted=1")

Переменные окружения:
    MCP_STORAGE_DIR          — каталог для новых БД (по умолчанию ./storage)
    MCP_DB_BUSY_TIMEOUT_MS   — busy_timeout, мс (по умолчанию 5000)
    MCP_DB_LOCK_RETRIES      — число повторов при блокировке (по умолчанию 5)
    MCP_DB_CACHE_KB          — cache_size на соединение, КБ (по умолчанию 64000)
"""

import os
import atexit
import sqlite3
import threading
import time
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("mcp.storage")

# ─── Настройки ────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.environ.get("MCP_STORAGE_DIR", os.path.join(_BASE_DIR, "storage"))
BUSY_TIMEOUT_MS = int(os.environ.get("MCP_DB_BUSY_TIMEOUT_MS", "5000"))
LOCK_RETRIES = int(os.environ.get("MCP_DB_LOCK_RETRIES", "5"))
CACHE_KB = int(os.environ.get("MCP_DB_CACHE_KB", "64000"))

# ─── Внутреннее состояние ─────────────────────────────────────────────────
_registry: Dict[str, str] = {}          # логическое имя -> абсолютный путь
_registry_lock = threading.Lock()

_local = threading.local()              # ._conns: Dict[path, sqlite3.Connection]

_all_conns: List[sqlite3.Connection] = []   # для close_all() при завершении
_all_conns_lock = threading.Lock()

_closed = False


# ─── Реестр баз данных ────────────────────────────────────────────────────
def register(name: str, legacy_path: Optional[str] = None) -> str:
    """
    Регистрирует логическое имя БД и возвращает разрешённый путь.

    Приоритет выбора пути:
      1. legacy_path, если он задан и файл УЖЕ существует
         (сохранение накопленных данных — никакой миграции не требуется);
      2. legacy_path, если он задан явно через переменную окружения
         (пользователь сам выбрал место — уважаем);
      3. MCP_STORAGE_DIR/<name>.sqlite — новое единое хранилище.

    Повторная регистрация того же имени безопасна (идемпотентна),
    но попытка перерегистрировать на ДРУГОЙ путь логируется как warning
    и игнорируется — побеждает первый зарегистрированный путь.
    """
    with _registry_lock:
        if legacy_path and os.path.isfile(legacy_path):
            resolved = os.path.abspath(legacy_path)
        elif legacy_path and os.environ.get(_env_hint(name)):
            resolved = os.path.abspath(legacy_path)
        else:
            resolved = os.path.join(STORAGE_DIR, f"{name}.sqlite")

        existing = _registry.get(name)
        if existing and os.path.normcase(existing) != os.path.normcase(resolved):
            log.warning("storage.register(%r): уже зарегистрирована как %s, "
                        "новый путь %s игнорируется", name, existing, resolved)
            return existing

        _registry[name] = resolved
        return resolved


def _env_hint(name: str) -> str:
    """Имя переменной окружения-подсказки для БД (для register)."""
    return f"MCP_{name.upper()}_DB"


def resolve(name_or_path: str) -> str:
    """
    Преобразует логическое имя или путь в абсолютный путь к файлу БД.

    Правила:
      • ":memory:" — возвращается как есть;
      • содержит разделитель пути или заканчивается на .db/.sqlite/.sqlite3 —
        трактуется как путь (обратная совместимость со старым кодом);
      • иначе — логическое имя: ищется в реестре, при отсутствии
        регистрируется автоматически в STORAGE_DIR.
    """
    if name_or_path == ":memory:":
        return name_or_path
    looks_like_path = (
        os.sep in name_or_path
        or (os.altsep and os.altsep in name_or_path)
        or name_or_path.lower().endswith((".db", ".sqlite", ".sqlite3"))
    )
    if looks_like_path:
        return os.path.abspath(name_or_path)
    with _registry_lock:
        path = _registry.get(name_or_path)
    if path:
        return path
    return register(name_or_path)


# ─── Управление соединениями ──────────────────────────────────────────────
def _apply_pragmas(conn: sqlite3.Connection, path: str) -> None:
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(f"PRAGMA cache_size=-{CACHE_KB};")
    conn.execute("PRAGMA foreign_keys=ON;")


def get(name_or_path: str) -> sqlite3.Connection:
    """
    Возвращает ПОСТОЯННОЕ thread-local соединение с БД.

    Соединение создаётся один раз на поток и переиспользуется.
    Не закрывайте его вручную — используйте close_thread() при
    завершении рабочего потока или положитесь на atexit.

    ВАЖНО: row_factory у соединения общий для всего потока.
    Если нужен sqlite3.Row — используйте connection(..., row=True)
    или query_one/query_all(..., row=True), которые аккуратно
    выставляют и восстанавливают row_factory.
    """
    if _closed:
        raise RuntimeError("mcp_storage уже закрыт (close_all вызван)")

    path = resolve(name_or_path)
    conns: Dict[str, sqlite3.Connection] = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns

    conn = conns.get(path)
    if conn is not None:
        return conn

    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    conn = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        check_same_thread=False,  # закрытие из close_all при завершении
    )
    _apply_pragmas(conn, path)
    conns[path] = conn
    with _all_conns_lock:
        _all_conns.append(conn)
    log.debug("storage: открыто соединение %s (поток %s)",
              path, threading.current_thread().name)
    return conn


def _is_locked_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


@contextmanager
def connection(name_or_path: str, row: bool = False):
    """
    Контекст-менеджер транзакции. Прямая замена паттерна:

        with sqlite3.connect(path) as conn:      # старый код
        with storage.connection("name") as conn:  # новый код

    Поведение:
      • commit при успешном выходе, rollback при исключении
        (как у нативного контекст-менеджера sqlite3, но соединение
        НЕ закрывается — оно постоянное);
      • row=True временно включает sqlite3.Row и восстанавливает
        прежний row_factory на выходе;
      • при «database is locked» commit повторяется до LOCK_RETRIES раз
        с экспоненциальным backoff.
    """
    conn = get(name_or_path)
    prev_factory = conn.row_factory
    if row:
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        _commit_with_retry(conn)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.row_factory = prev_factory


def _commit_with_retry(conn: sqlite3.Connection) -> None:
    delay = 0.05
    for attempt in range(LOCK_RETRIES + 1):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if not _is_locked_error(e) or attempt == LOCK_RETRIES:
                raise
            log.debug("storage: БД заблокирована, повтор commit через %.2fs "
                      "(попытка %d/%d)", delay, attempt + 1, LOCK_RETRIES)
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


# ─── Хелперы для типовых операций ─────────────────────────────────────────
def execute(name_or_path: str, sql: str,
            params: Iterable[Any] = ()) -> int:
    """Одиночная запись в транзакции. Возвращает rowcount."""
    with connection(name_or_path) as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.rowcount


def executemany(name_or_path: str, sql: str,
                seq_of_params: Iterable[Iterable[Any]]) -> int:
    """Пакетная запись в одной транзакции. Возвращает rowcount."""
    with connection(name_or_path) as conn:
        cur = conn.executemany(sql, [tuple(p) for p in seq_of_params])
        return cur.rowcount


def executescript(name_or_path: str, script: str) -> None:
    """Выполнить SQL-скрипт (например, создание схемы)."""
    conn = get(name_or_path)
    conn.executescript(script)
    _commit_with_retry(conn)


def query_one(name_or_path: str, sql: str, params: Iterable[Any] = (),
              row: bool = False) -> Optional[Any]:
    """SELECT одной строки. row=True -> sqlite3.Row (доступ по имени)."""
    conn = get(name_or_path)
    prev = conn.row_factory
    if row:
        conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, tuple(params)).fetchone()
    finally:
        conn.row_factory = prev


def query_all(name_or_path: str, sql: str, params: Iterable[Any] = (),
              row: bool = False) -> List[Any]:
    """SELECT всех строк. row=True -> список sqlite3.Row."""
    conn = get(name_or_path)
    prev = conn.row_factory
    if row:
        conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.row_factory = prev


# ─── Обслуживание ─────────────────────────────────────────────────────────
def close_thread() -> None:
    """
    Закрывает соединения ТЕКУЩЕГО потока.
    Вызывайте в конце работы долгоживущих рабочих потоков,
    чтобы не копить открытые дескрипторы.
    """
    conns: Dict[str, sqlite3.Connection] = getattr(_local, "conns", None)
    if not conns:
        return
    for path, conn in list(conns.items()):
        try:
            conn.close()
        except Exception:
            pass
        with _all_conns_lock:
            try:
                _all_conns.remove(conn)
            except ValueError:
                pass
    _local.conns = {}


def close_all() -> None:
    """Закрывает ВСЕ соединения (вызывается автоматически через atexit)."""
    global _closed
    _closed = True
    with _all_conns_lock:
        conns, _all_conns[:] = list(_all_conns), []
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass
    log.debug("storage: закрыто %d соединений", len(conns))


def stats() -> Dict[str, Any]:
    """Диагностика: зарегистрированные БД, размеры файлов, соединения потока."""
    with _registry_lock:
        reg = dict(_registry)
    out: Dict[str, Any] = {"storage_dir": STORAGE_DIR, "databases": {}}
    for name, path in reg.items():
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        out["databases"][name] = {"path": path, "size_bytes": size}
    conns = getattr(_local, "conns", {}) or {}
    out["thread_connections"] = list(conns.keys())
    with _all_conns_lock:
        out["total_open_connections"] = len(_all_conns)
    return out


atexit.register(close_all)
