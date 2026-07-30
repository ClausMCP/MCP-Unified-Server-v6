#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Connectivity v1.1 — единая точка знания о доступности внешних ресурсов.

Исправления относительно исходной версии (v1.0):
- `is_online()` теперь возвращает результат ТОЛЬКО TCP-пробы; circuit breaker
  на web-сервис больше не «фальшивит» сетевой статус.
- `is_llm_up()` выполняет двухуровневую проверку: TCP-проба + GET /v1/models
  (важно для LM Studio: порт открыт, но модель может быть не загружена).
- URL LLM парсится один раз при импорте.
- `register_tools` использует обычные функции (без лямбда-трюка).
- Кэш LLM_MODEL_INFO обновляется при смене модели.
- LLM_MODE по умолчанию 'local' (а не 'full' с предположением об облаке).
- Добавлен `is_model_loaded()` — отдельная проверка наличия загруженной модели.
- В `status()` режим кешируется, чтобы не дёргать пробу при каждом status().
"""
import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from mcp_shared import _log
except ImportError:  # автономный запуск / тесты
    import sys

    def _log(msg: str) -> None:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}][Connectivity] {msg}",
            file=sys.stderr,
            flush=True,
        )


# ─── Configuration ──────────────────────────────────────────────────────────
PROBE_TARGETS: list = [
    t.strip()
    for t in os.environ.get("MCP_NET_PROBES", "1.1.1.1:443,8.8.8.8:53").split(",")
    if t.strip()
]
PROBE_TIMEOUT_SEC: float = float(os.environ.get("MCP_NET_PROBE_TIMEOUT", "1.5"))
ONLINE_CACHE_TTL_SEC: float = float(os.environ.get("MCP_NET_CACHE_TTL", "45"))

# Локальный LLM (LM Studio). Парсим один раз.
LLM_ENDPOINT: str = os.environ.get(
    "LLM_ENDPOINT", "http://localhost:1234/v1/chat/completions"
)
_LLM_PARSED = urllib.parse.urlparse(LLM_ENDPOINT)
_LLM_HOST: str = _LLM_PARSED.hostname or "localhost"
_LLM_PORT: int = _LLM_PARSED.port or (443 if _LLM_PARSED.scheme == "https" else 80)
_LLM_BASE: str = f"{_LLM_PARSED.scheme}://{_LLM_HOST}:{_LLM_PORT}"
_LLM_MODELS_URL: str = f"{_LLM_BASE}/v1/models"

LLM_CACHE_TTL_SEC: float = float(os.environ.get("MCP_LLM_CACHE_TTL", "15"))
LLM_MODEL_POLL_SEC: float = float(os.environ.get("MCP_LLM_MODEL_POLL", "60"))

# Circuit Breaker
CB_FAILURE_THRESHOLD: int = int(os.environ.get("MCP_CB_FAILURES", "5"))
CB_RECOVERY_TIMEOUT_SEC: float = float(os.environ.get("MCP_CB_RECOVERY", "60"))
CB_HALF_OPEN_MAX_CALLS: int = int(os.environ.get("MCP_CB_HALF_OPEN_CALLS", "1"))


# ─── Circuit Breaker ─────────────────────────────────────────────────────────
class CircuitState(Enum):
    CLOSED = "closed"        # всё хорошо, вызовы проходят
    OPEN = "open"            # сервис лежит, вызовы блокируются сразу
    HALF_OPEN = "half_open"  # пробуем один вызов после паузы


class CircuitOpenError(RuntimeError):
    """Сервис недоступен — circuit открыт. Используйте degrade(service)."""

    def __init__(self, service: str, retry_after: float) -> None:
        self.service = service
        self.retry_after = retry_after
        super().__init__(
            f"Circuit '{service}' is OPEN, retry after {retry_after:.0f}s. "
            f"Use degrade('{service}') for a fallback."
        )


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = CB_FAILURE_THRESHOLD,
        recovery_timeout: float = CB_RECOVERY_TIMEOUT_SEC,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0
        self._total_calls = 0
        self._total_failures = 0
        self._last_error: Optional[str] = None

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and time.monotonic() - self._opened_at >= self.recovery_timeout
        ):
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

    def record_success(self) -> None:
        with self._lock:
            self._total_calls += 1
            if self._state is not CircuitState.CLOSED:
                _log(f"[CB:{self.name}] recovered → CLOSED")
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._last_error = None

    def record_failure(self, error: Optional[BaseException] = None) -> None:
        with self._lock:
            self._total_calls += 1
            self._total_failures += 1
            self._failures += 1
            if error is not None:
                self._last_error = f"{type(error).__name__}: {error}"
            if self._state is CircuitState.HALF_OPEN:
                self._trip()
            elif (
                self._state is CircuitState.CLOSED
                and self._failures >= self.failure_threshold
            ):
                self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        _log(
            f"[CB:{self.name}] tripped → OPEN "
            f"({self._failures} failures, last: {self._last_error})"
        )

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._half_open_calls = 0

    def retry_after(self) -> float:
        with self._lock:
            if self._state is not CircuitState.OPEN:
                return 0.0
            return max(0.0, self.recovery_timeout - (time.monotonic() - self._opened_at))

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


# ─── Model info ──────────────────────────────────────────────────────────────
@dataclass
class LLMModelInfo:
    id: str
    context_length: Optional[int] = None
    quantization: Optional[str] = None
    fetched_at: float = 0.0


# ─── ConnectivityManager ─────────────────────────────────────────────────────
class ConnectivityManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._fallbacks: Dict[str, Callable[..., Any]] = {}
        self._probe_cache: Dict[str, Tuple[float, bool]] = {}
        self._model_info: Optional[LLMModelInfo] = None
        self._model_loaded: bool = False

    # -- breakers --
    def breaker(self, service: str) -> CircuitBreaker:
        with self._lock:
            if service not in self._breakers:
                self._breakers[service] = CircuitBreaker(service)
            return self._breakers[service]

    # -- probers --
    @staticmethod
    def _tcp_probe(host: str, port: int, timeout: float = PROBE_TIMEOUT_SEC) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _cached(
        self,
        key: str,
        fn: Callable[[], bool],
        ttl: float = ONLINE_CACHE_TTL_SEC,
        force: bool = False,
    ) -> bool:
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
        """Только сетевая проба. НЕ зависит от circuit breaker.

        Исправление: исходный код смешивал эту функцию с CB, что вызывало
        ложные срабатывания и путало семантику.
        """
        def probe() -> bool:
            for target in PROBE_TARGETS:
                host, _, port = target.partition(":")
                try:
                    if self._tcp_probe(host, int(port or 443)):
                        return True
                except ValueError:
                    continue
            return False

        return self._cached("internet", probe, force=force)

    def _llm_http_check(self) -> bool:
        """GET /v1/models — определяет, загружена ли модель в LM Studio."""
        try:
            req = urllib.request.Request(_LLM_MODELS_URL, method="GET")
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SEC) as resp:
                if resp.status != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                models = data.get("data") or []
                with self._lock:
                    self._model_loaded = bool(models)
                    if models:
                        first = models[0]
                        self._model_info = LLMModelInfo(
                            id=str(first.get("id", "")),
                            context_length=_extract_context_length(first),
                            quantization=_extract_quantization(first),
                            fetched_at=time.monotonic(),
                        )
                    else:
                        self._model_info = None
                return self._model_loaded
        except (OSError, ValueError, KeyError):
            with self._lock:
                self._model_loaded = False
                self._model_info = None
            return False

    def is_llm_up(self, force: bool = False) -> bool:
        """Двухуровневая проверка: TCP + наличие загруженной модели."""
        tcp_ok = self._cached(
            "llm_tcp",
            lambda: self._tcp_probe(_LLM_HOST, _LLM_PORT, PROBE_TIMEOUT_SEC),
            ttl=LLM_CACHE_TTL_SEC,
            force=force,
        )
        if not tcp_ok:
            with self._lock:
                self._model_loaded = False
                self._model_info = None
            return False
        # Проверка загруженной модели — кешируется отдельно
        return self._cached(
            "llm_models",
            self._llm_http_check,
            ttl=LLM_MODEL_POLL_SEC,
            force=force,
        )

    def is_model_loaded(self, force: bool = False) -> bool:
        with self._lock:
            if force:
                self._llm_http_check()
            return self._model_loaded

    def get_model_info(self, force: bool = False) -> Optional[LLMModelInfo]:
        with self._lock:
            if force or self._model_info is None:
                self._llm_http_check()
            return self._model_info

    def is_service_up(self, service: str) -> bool:
        if service == "llm":
            return self.is_llm_up()
        if service in ("web", "internet"):
            return self.is_online()
        cb = self._breakers.get(service)
        return cb is None or cb.state is not CircuitState.OPEN

    # -- fallbacks --
    def register_fallback(self, service: str, fallback: Callable[..., Any]) -> None:
        with self._lock:
            self._fallbacks[service] = fallback
        _log(
            f"Fallback registered for '{service}': "
            f"{getattr(fallback, '__name__', repr(fallback))}"
        )

    def degrade(self, service: str) -> Optional[Callable[..., Any]]:
        with self._lock:
            return self._fallbacks.get(service)

    def _mode(self) -> str:
        online = self.is_online()
        llm = self.is_llm_up()
        if llm and online:
            return "full"      # интернет + LLM
        if llm:
            return "local"     # только локальный LLM + книги/память (ОСНОВНОЙ РЕЖИМ)
        if online:
            return "web_only"  # интернет есть, LLM лежит
        return "offline"       # ничего

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "online": self.is_online(),
                "llm_up": self.is_llm_up(),
                "model_loaded": self._model_loaded,
                "model_info": (
                    {
                        "id": self._model_info.id,
                        "context_length": self._model_info.context_length,
                        "quantization": self._model_info.quantization,
                    }
                    if self._model_info
                    else None
                ),
                "mode": self._mode(),
                "breakers": {n: b.stats() for n, b in self._breakers.items()},
                "fallbacks": list(self._fallbacks.keys()),
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }


def _extract_context_length(model_meta: Dict[str, Any]) -> Optional[int]:
    meta = model_meta.get("meta") or {}
    for key in ("n_ctx_train", "n_ctx", "context_length", "max_context_length"):
        if key in meta:
            try:
                return int(meta[key])
            except (TypeError, ValueError):
                return None
    return None


def _extract_quantization(model_meta: Dict[str, Any]) -> Optional[str]:
    meta = model_meta.get("meta") or {}
    for key in ("quantization", "quant", "precision"):
        if key in meta:
            return str(meta[key])
    return None


# ─── Global instance & convenience wrappers ──────────────────────────────────
_manager = ConnectivityManager()


def is_online(force: bool = False) -> bool:
    return _manager.is_online(force)


def is_llm_up(force: bool = False) -> bool:
    return _manager.is_llm_up(force)


def is_model_loaded(force: bool = False) -> bool:
    return _manager.is_model_loaded(force)


def get_model_info(force: bool = False) -> Optional[LLMModelInfo]:
    return _manager.get_model_info(force)


def is_service_up(service: str) -> bool:
    return _manager.is_service_up(service)


def register_fallback(service: str, fallback: Callable[..., Any]) -> None:
    _manager.register_fallback(service, fallback)


def degrade(service: str) -> Optional[Callable[..., Any]]:
    return _manager.degrade(service)


def connectivity_status() -> Dict[str, Any]:
    return _manager.status()


def get_mode() -> str:
    """full | local | web_only | offline. По умолчанию 'local' (LM Studio)."""
    return _manager._mode()


# ─── Circuit Breaker decorator ───────────────────────────────────────────────
def circuit(
    service: str,
    fallback: Optional[Callable[..., Any]] = None,
    exceptions: tuple = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Декоратор Circuit Breaker для сетевых функций (llm, web, email, ...)."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        cb = _manager.breaker(service)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not cb.allow_call():
                fb = fallback or _manager.degrade(service)
                if fb is not None:
                    _log(
                        f"[CB:{service}] open → fallback "
                        f"{getattr(fb, '__name__', repr(fb))}"
                    )
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
        wrapper.circuit_breaker = cb  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ─── MCP-инструменты (опционально) ───────────────────────────────────────────
def _tool_status(**_kw: Any) -> Dict[str, Any]:
    """Регистрируется как MCP-tool: возвращает полный статус подключения."""
    return _manager.status()


def _tool_reset_breaker(**kw: Any) -> Dict[str, Any]:
    service = kw.get("service")
    if not service:
        return {"status": "error", "error": "service is required"}
    _manager.breaker(service).reset()
    return {"status": "reset", "service": service}


def _tool_model_info(**_kw: Any) -> Dict[str, Any]:
    """Регистрируется как MCP-tool: возвращает информацию о текущей модели."""
    info = _manager.get_model_info(force=True)
    if info is None:
        return {"loaded": False, "info": None}
    return {
        "loaded": True,
        "info": {
            "id": info.id,
            "context_length": info.context_length,
            "quantization": info.quantization,
        },
    }


def register_tools(server: Any) -> None:
    """Подключение к BaseMCPServer."""
    server.register_tool(
        "connectivity_status",
        {
            "description": (
                "Статус доступности: интернет, LLM, circuit breakers, "
                "режим (full/local/web_only/offline), информация о модели"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "force": {
                        "type": "boolean",
                        "description": "Игнорировать кэш пробников",
                        "default": False,
                    }
                },
            },
        },
        _tool_status,
    )

    server.register_tool(
        "connectivity_reset_breaker",
        {
            "description": "Сбросить circuit breaker сервиса вручную",
            "inputSchema": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
        _tool_reset_breaker,
    )

    server.register_tool(
        "connectivity_model_info",
        {
            "description": (
                "Информация о текущей загруженной модели LLM (LM Studio)"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        _tool_model_info,
    )


# ─── CLI self-test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(json.dumps(connectivity_status(), ensure_ascii=False, indent=2))
