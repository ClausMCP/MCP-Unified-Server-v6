#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Connectivity v1.1 (автозагрузка unified-сервером) — единая точка знания о доступности внешних ресурсов.

Решает задачу «иногда есть интернет, иногда только локальная память/книги»:
  * is_online()          — дешёвая кэшируемая проверка интернета (TCP-пробник)
  * is_service_up(name)  — состояние конкретного сервиса (llm, web, ...)
  * CircuitBreaker       — только для сетевых сервисов (LLM API, web_reader).
                           Для локальной ФС НЕ используется (там таймауты+retry).
  * Реестр деградаций    — register_fallback("rag", keyword_search):
                           degrade("rag") вернёт рабочую функцию-замену.

Зависимости: только stdlib. Интеграция:
    from mcp_connectivity import circuit, is_online, register_fallback, degrade

    @circuit("llm")
    def call_llm(...): ...
"""
import os
import time
import json
import socket
import threading
import urllib.parse
from enum import Enum
from typing import Callable, Dict, Optional, Any, Tuple
from datetime import datetime

try:
    from mcp_shared import _log
except ImportError:  # автономный запуск / тесты
    import sys
    def _log(msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}][Connectivity] {msg}",
              file=sys.stderr, flush=True)

# ─── Конфигурация ────────────────────────────────────────────────────────────
# Пробники интернета: пары host:port через запятую (TCP connect, без HTTP)
PROBE_TARGETS = [
    t.strip() for t in os.environ.get(
        "MCP_NET_PROBES", "1.1.1.1:443,8.8.8.8:53"
    ).split(",") if t.strip()
]
PROBE_TIMEOUT_SEC = float(os.environ.get("MCP_NET_PROBE_TIMEOUT", "1.5"))
ONLINE_CACHE_TTL_SEC = float(os.environ.get("MCP_NET_CACHE_TTL", "45"))

# Локальный LLM (LM Studio) — отдельный пробник, это НЕ интернет
LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "http://localhost:1234/v1/chat/completions")

# Circuit Breaker
CB_FAILURE_THRESHOLD = int(os.environ.get("MCP_CB_FAILURES", "5"))
CB_RECOVERY_TIMEOUT_SEC = float(os.environ.get("MCP_CB_RECOVERY", "60"))
CB_HALF_OPEN_MAX_CALLS = int(os.environ.get("MCP_CB_HALF_OPEN_CALLS", "1"))


# ─── Circuit Breaker ─────────────────────────────────────────────────────────
class CircuitState(Enum):
    CLOSED = "closed"        # всё хорошо, вызовы проходят
    OPEN = "open"            # сервис лежит, вызовы блокируются сразу
    HALF_OPEN = "half_open"  # пробуем один вызов после паузы


class CircuitOpenError(RuntimeError):
    """Сервис недоступен — circuit открыт. Используйте degrade(service)."""
    def __init__(self, service: str, retry_after: float):
        self.service = service
        self.retry_after = retry_after
        super().__init__(
            f"Circuit '{service}' is OPEN, retry after {retry_after:.0f}s. "
            f"Use degrade('{service}') for a fallback."
        )


class CircuitBreaker:
    """
    Классический CB: CLOSED → (N ошибок) → OPEN → (таймаут) → HALF_OPEN →
    успех → CLOSED / ошибка → OPEN. Потокобезопасен.
    """
    def __init__(self, name: str,
                 failure_threshold: int = CB_FAILURE_THRESHOLD,
                 recovery_timeout: float = CB_RECOVERY_TIMEOUT_SEC):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0
        # статистика
        self._total_calls = 0
        self._total_failures = 0
        self._last_error: Optional[str] = None

    # -- состояние --
    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self):
        if (self._state is CircuitState.OPEN and
                time.monotonic() - self._opened_at >= self.recovery_timeout):
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0
            _log(f"[CB:{self.name}] OPEN → HALF_OPEN (probe allowed)")

    def allow_call(self) -> bool:
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_calls < CB_HALF_OPEN_MAX_CALLS:
                    self._half_open_calls += 1
                    return True
                return False
            return False  # OPEN

    def record_success(self):
        with self._lock:
            self._total_calls += 1
            if self._state is not CircuitState.CLOSED:
                _log(f"[CB:{self.name}] recovered → CLOSED")
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._last_error = None

    def record_failure(self, error: Exception = None):
        with self._lock:
            self._total_calls += 1
            self._total_failures += 1
            self._failures += 1
            if error is not None:
                self._last_error = f"{type(error).__name__}: {error}"
            if self._state is CircuitState.HALF_OPEN:
                self._trip()
            elif (self._state is CircuitState.CLOSED and
                  self._failures >= self.failure_threshold):
                self._trip()

    def _trip(self):
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        _log(f"[CB:{self.name}] tripped → OPEN "
             f"({self._failures} failures, last: {self._last_error})")

    def reset(self):
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._half_open_calls = 0

    def retry_after(self) -> float:
        with self._lock:
            if self._state is not CircuitState.OPEN:
                return 0.0
            return max(0.0, self.recovery_timeout -
                       (time.monotonic() - self._opened_at))

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state.value,
                "consecutive_failures": self._failures,
                "total_calls": self._total_calls,
                "total_failures": self._total_failures,
                "last_error": self._last_error,
                "retry_after_sec": round(self.retry_after(), 1),
            }


# ─── ConnectivityManager ─────────────────────────────────────────────────────
class ConnectivityManager:
    """
    Центральный реестр: пробники, circuit breakers по сервисам, деградации.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._fallbacks: Dict[str, Callable] = {}
        # кэш пробников: name -> (timestamp_monotonic, result)
        self._probe_cache: Dict[str, Tuple[float, bool]] = {}

    # -- breakers --
    def breaker(self, service: str) -> CircuitBreaker:
        with self._lock:
            if service not in self._breakers:
                self._breakers[service] = CircuitBreaker(service)
            return self._breakers[service]

    # -- пробники --
    def _tcp_probe(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SEC):
                return True
        except OSError:
            return False

    def _cached(self, key: str, fn: Callable[[], bool],
                ttl: float = ONLINE_CACHE_TTL_SEC, force: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            if not force and key in self._probe_cache:
                ts, result = self._probe_cache[key]
                if now - ts < ttl:
                    return result
        result = fn()
        with self._lock:
            self._probe_cache[key] = (now, result)
        return result

    def is_online(self, force: bool = False) -> bool:
        """Есть ли интернет. Кэш на ONLINE_CACHE_TTL_SEC."""
        def probe() -> bool:
            for target in PROBE_TARGETS:
                host, _, port = target.partition(":")
                if self._tcp_probe(host, int(port or 443)):
                    return True
            return False
        result = self._cached("internet", probe, force=force)
        # открытый circuit "web" — дополнительный сигнал офлайна
        web_cb = self._breakers.get("web")
        if web_cb is not None and web_cb.state is CircuitState.OPEN:
            return False
        return result

    def is_llm_up(self, force: bool = False) -> bool:
        """Доступен ли локальный LLM (LM Studio). Это не интернет!"""
        parsed = urllib.parse.urlparse(LLM_ENDPOINT)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        result = self._cached("llm", lambda: self._tcp_probe(host, port),
                              ttl=15.0, force=force)
        llm_cb = self._breakers.get("llm")
        if llm_cb is not None and llm_cb.state is CircuitState.OPEN:
            return False
        return result

    def is_service_up(self, service: str) -> bool:
        if service == "llm":
            return self.is_llm_up()
        if service in ("web", "internet"):
            return self.is_online()
        cb = self._breakers.get(service)
        return cb is None or cb.state is not CircuitState.OPEN

    # -- деградации --
    def register_fallback(self, service: str, fallback: Callable):
        """Например: register_fallback('rag', keyword_search)."""
        with self._lock:
            self._fallbacks[service] = fallback
        _log(f"Fallback registered for '{service}': "
             f"{getattr(fallback, '__name__', repr(fallback))}")

    def degrade(self, service: str) -> Optional[Callable]:
        """Вернуть функцию-замену для недоступного сервиса (или None)."""
        with self._lock:
            return self._fallbacks.get(service)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "online": self.is_online(),
                "llm_up": self.is_llm_up(),
                "mode": self._mode(),
                "breakers": {n: b.stats() for n, b in self._breakers.items()},
                "fallbacks": list(self._fallbacks.keys()),
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }

    def _mode(self) -> str:
        online, llm = self.is_online(), self.is_llm_up()
        if online and llm:
            return "full"          # интернет + LLM
        if llm:
            return "local"         # только локальный LLM + книги/память
        if online:
            return "web_only"      # интернет есть, LLM лежит
        return "offline"           # только память и файлы


# ─── Глобальный экземпляр и удобные обёртки ──────────────────────────────────
_manager = ConnectivityManager()

def is_online(force: bool = False) -> bool:
    return _manager.is_online(force)

def is_llm_up(force: bool = False) -> bool:
    return _manager.is_llm_up(force)

def is_service_up(service: str) -> bool:
    return _manager.is_service_up(service)

def register_fallback(service: str, fallback: Callable):
    _manager.register_fallback(service, fallback)

def degrade(service: str) -> Optional[Callable]:
    return _manager.degrade(service)

def connectivity_status() -> Dict[str, Any]:
    return _manager.status()

def get_mode() -> str:
    """full | local | web_only | offline"""
    return _manager._mode()


def circuit(service: str, fallback: Callable = None,
            exceptions: tuple = (Exception,)):
    """
    Декоратор Circuit Breaker для сетевых функций (llm, web, email, ...).

    @circuit("llm")
    def ask_llm(prompt): ...

    При открытом circuit:
      * если задан fallback (или зарегистрирован через register_fallback) —
        вызывается он с теми же аргументами;
      * иначе — CircuitOpenError.
    """
    def decorator(fn: Callable) -> Callable:
        cb = _manager.breaker(service)

        def wrapper(*args, **kwargs):
            if not cb.allow_call():
                fb = fallback or _manager.degrade(service)
                if fb is not None:
                    _log(f"[CB:{service}] open → fallback "
                         f"{getattr(fb, '__name__', repr(fb))}")
                    return fb(*args, **kwargs)
                raise CircuitOpenError(service, cb.retry_after())
            try:
                result = fn(*args, **kwargs)
            except exceptions as e:
                cb.record_failure(e)
                raise
            cb.record_success()
            return result

        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        wrapper.__doc__ = fn.__doc__
        wrapper.circuit_breaker = cb  # доступ к статистике
        return wrapper
    return decorator


# ─── Регистрация MCP-инструментов (опционально) ──────────────────────────────
def register_tools(server):
    """Подключение к BaseMCPServer: server-side статус доступности."""
    server.register_tool("connectivity_status", {
        "description": "Статус доступности: интернет, LLM, circuit breakers, режим (full/local/web_only/offline)",
        "inputSchema": {"type": "object", "properties": {
            "force": {"type": "boolean", "description": "Игнорировать кэш пробников", "default": False}
        }}
    }, lambda **kw: (_manager.is_online(force=kw.get("force", False)),
                     _manager.is_llm_up(force=kw.get("force", False)),
                     _manager.status())[-1])

    server.register_tool("connectivity_reset_breaker", {
        "description": "Сбросить circuit breaker сервиса вручную",
        "inputSchema": {"type": "object", "properties": {
            "service": {"type": "string"}
        }, "required": ["service"]}
    }, lambda **kw: (_manager.breaker(kw["service"]).reset(),
                     {"status": "reset", "service": kw["service"]})[-1])


__mcp_plugin__ = {
    "name": "connectivity",
    "version": "1.1",
    "description": "Доступность интернета/LLM, circuit breakers, режимы full/local/web_only/offline",
    "dependencies": [],
    "on_load": lambda: _log("[Connectivity] Plugin loaded. Mode: " + get_mode()),
}

if __name__ == "__main__":
    print(json.dumps(connectivity_status(), ensure_ascii=False, indent=2))
