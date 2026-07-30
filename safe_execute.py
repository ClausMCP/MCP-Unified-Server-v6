#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Safe Execute v1.0 — глобальный декоратор обработки ошибок (Фаза 0).

Возможности:
  * перехват исключений → структурированный {"status": "error", ...}
    вместо падения MCP-инструмента;
  * retry с exponential backoff для временных ошибок (опционально);
  * fallback-значение или fallback-функция;
  * логирование в _log и (опционально) в conversation_memory;
  * таймаут на выполнение (для блокирующих операций ФС).

Использование:
    from safe_execute import safe_execute

    @safe_execute(retries=2, log_memory=True)
    def read_big_file(path): ...

    @safe_execute(fallback={"status": "degraded", "items": []})
    def search_index(q): ...
"""
import time
import threading
import traceback
import functools
from typing import Callable, Any, Optional, Tuple
from datetime import datetime

try:
    from mcp_shared import _log, conversation_memory, dialog_ctx
    _HAS_SHARED = True
except ImportError:
    import sys
    _HAS_SHARED = False
    def _log(msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}][SafeExec] {msg}",
              file=sys.stderr, flush=True)

# Ошибки, которые имеет смысл ретраить (временные)
TRANSIENT_ERRORS: Tuple[type, ...] = (
    TimeoutError, ConnectionError, OSError, IOError,
)

class SafeExecuteTimeout(TimeoutError):
    pass


def _run_with_timeout(fn: Callable, args, kwargs, timeout: float):
    """Выполнить fn в потоке с таймаутом. Поток-демон: не блокирует выход."""
    result: dict = {}
    def target():
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as e:  # перехватываем всё, отдаём наружу
            result["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise SafeExecuteTimeout(
            f"{getattr(fn, '__name__', 'fn')} exceeded {timeout}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def safe_execute(fn: Callable = None, *,
                 retries: int = 0,
                 backoff: float = 0.5,
                 retry_on: Tuple[type, ...] = TRANSIENT_ERRORS,
                 timeout: Optional[float] = None,
                 fallback: Any = None,
                 fallback_fn: Optional[Callable] = None,
                 reraise: bool = False,
                 log_memory: bool = False):
    """
    Декоратор. Параметры:
      retries     — число повторов при retry_on-ошибках (0 = без повторов)
      backoff     — базовая пауза, растёт как backoff * 2**attempt
      timeout     — сек; если задан, выполнение в потоке с таймаутом
      fallback    — значение, возвращаемое при окончательной ошибке
      fallback_fn — функция(*args, **kwargs) вместо fallback-значения
      reraise     — пробросить исключение вместо возврата error-дикта
      log_memory  — записать ошибку в conversation_memory (если доступна)
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            name = getattr(func, "__name__", "fn")
            last_exc: Optional[BaseException] = None

            for attempt in range(retries + 1):
                try:
                    if timeout is not None:
                        return _run_with_timeout(func, args, kwargs, timeout)
                    return func(*args, **kwargs)
                except retry_on as e:
                    last_exc = e
                    if attempt < retries:
                        delay = backoff * (2 ** attempt)
                        _log(f"[{name}] transient error "
                             f"({type(e).__name__}: {e}), "
                             f"retry {attempt + 1}/{retries} in {delay:.1f}s")
                        time.sleep(delay)
                        continue
                    break
                except Exception as e:
                    last_exc = e
                    break  # не-временная ошибка — без retry

            # окончательная ошибка
            err_type = type(last_exc).__name__
            err_msg = str(last_exc)
            _log(f"[{name}] FAILED: {err_type}: {err_msg}")

            if log_memory and _HAS_SHARED:
                try:
                    conversation_memory.add(
                        op=f"error:{name}", paths={},
                        status="error", dialog=dialog_ctx.get(),
                        context=f"{err_type}: {err_msg}",
                        meta={"traceback": traceback.format_exc()[-2000:]},
                        category="error",
                    )
                except Exception as mem_err:
                    _log(f"[{name}] memory logging failed: {mem_err}")

            if fallback_fn is not None:
                try:
                    return fallback_fn(*args, **kwargs)
                except Exception as fb_err:
                    _log(f"[{name}] fallback_fn failed too: {fb_err}")

            if reraise:
                raise last_exc

            if fallback is not None:
                return fallback

            return {
                "status": "error",
                "error_type": err_type,
                "message": err_msg,
                "function": name,
                "retries_attempted": retries,
            }
        return wrapper

    # поддержка @safe_execute без скобок
    if fn is not None and callable(fn):
        return decorator(fn)
    return decorator
