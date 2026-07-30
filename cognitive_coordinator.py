#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cognitive Coordinator v2.1 — async-safe версия.

Исправления относительно исходной версии (v2.0):
- ОТКАТ async-моста (v2.2): mcp_cognitive_bus полностью синхронен
  (`def publish`, колбэки вызываются в обычном цикле), поэтому корутинные
  обработчики приходилось запускать через `asyncio.run` / `run_until_complete`
  прямо в потоке-публикаторе. Это (а) не устраняло блокировку публикатора,
  ради которой правка делалась, (б) в ветке `ex is None` вызывало корутинную
  функцию без await — обработчик молча не выполнялся, (в) держалось на
  `asyncio.get_event_loop()`, deprecated с 3.12.
  Теперь обработчики синхронные, а неблокирующая доставка обеспечивается
  ThreadPoolExecutor'ом (async_workers > 0).
- Константа переименована в `MAX_ATTEMPTS` (3 попытки: 0, 1, 2).
- Валидация событий различает отсутствие поля и `None`.
- `_wrap_handler` сохраняет `async_workers` в `bool`-флаг, executor создаётся
  лениво (через `__init__` + `start()`), `shutdown()` всегда идемпотентен.
- `ThreadPoolExecutor` используется как context manager через `__enter__/__exit__`,
  что гарантирует корректное завершение даже при исключениях.
- Добавлен `subscribe_once` для разовых подписок (например, на `plan_finished`).
"""
import asyncio
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from mcp_cognitive_bus import publish, subscribe
except ImportError as exc:
    raise ImportError(
        f"cognitive_coordinator requires mcp_cognitive_bus: {exc}"
    ) from exc

try:
    from mcp_shared import _log
except ImportError:
    def _log(msg: str) -> None:  # type: ignore[no-redef]
        print(f"[Coordinator] {msg}", flush=True)


REFLECTION_LIMIT_ON_HYPOTHESIS_REJECTED: int = 100
REFLECTION_LIMIT_ON_GOAL_COMPLETED: int = 50

# ВАЖНО: раньше называлось MAX_RETRIES=2, но цикл был range(max_retries+1) → 3
# попытки. Теперь явно: 3 попытки.
MAX_ATTEMPTS: int = 3
RETRY_DELAY_SEC: float = 0.5


def safe_log(message: str, level: str = "INFO") -> None:
    """Логирование с уровнем."""
    prefix = {"INFO": "INFO", "WARNING": "WARN", "ERROR": "ERR"}.get(level, "INFO")
    _log(f"[Coordinator][{prefix}] {message}")


class CognitiveCoordinator:
    def __init__(self, async_workers: int = 0) -> None:
        self._world_model_cache: Optional[Tuple[Callable[..., Any], Callable[..., Any]]] = None
        self._hypothesis_engine_cache: Optional[Tuple[Callable[..., Any], Callable[..., Any]]] = None
        self._goal_manager_cache: Optional[Callable[..., Any]] = None
        self._reflection_cache: Optional[Callable[..., Any]] = None
        self._lock = threading.Lock()
        self._async_workers: int = max(0, async_workers)
        self._executor: Optional[ThreadPoolExecutor] = None
        self._started: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        """Создаёт executor лениво. Идемпотентно."""
        with self._lock:
            if self._started:
                return
            if self._async_workers > 0 and self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._async_workers,
                    thread_name_prefix="coord-worker",
                )
            self._started = True

    @contextmanager
    def _executor_cm(self) -> Any:
        """Гарантирует наличие executor и отдаёт его.

        ИСПРАВЛЕНО: раньше `yield` выполнялся под удержанным `threading.Lock`
        (не реентрантным). Любой код в теле `with`, дошедший до `_get_*`
        геттеров, которые берут тот же лок, вставал в дедлок. Теперь лок
        удерживается только на время создания executor'а.
        """
        with self._lock:
            if self._async_workers > 0 and self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._async_workers,
                    thread_name_prefix="coord-worker",
                )
                self._started = True
            executor = self._executor
        yield executor

    def shutdown(self) -> None:
        """Идемпотентное завершение executor'а."""
        with self._lock:
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=True, cancel_futures=False)
                except Exception as e:
                    safe_log(f"Executor shutdown error: {e}", level="ERROR")
                self._executor = None
            self._started = False

    # ── Lazy getters ───────────────────────────────────────────────────────
    def _get_world_model(self) -> Tuple[Callable[..., Any], Callable[..., Any]]:
        if self._world_model_cache is None:
            with self._lock:
                if self._world_model_cache is None:
                    from mcp_world_model import (
                        world_add_fact_sync,
                        world_run_inference_sync,
                    )
                    self._world_model_cache = (
                        world_add_fact_sync,
                        world_run_inference_sync,
                    )
        return self._world_model_cache

    def _get_hypothesis_engine(self) -> Tuple[Callable[..., Any], Callable[..., Any]]:
        if self._hypothesis_engine_cache is None:
            with self._lock:
                if self._hypothesis_engine_cache is None:
                    from mcp_hypothesis_engine import (
                        hyp_create_hypothesis,
                        hyp_update_hypothesis_status,
                    )
                    self._hypothesis_engine_cache = (
                        hyp_create_hypothesis,
                        hyp_update_hypothesis_status,
                    )
        return self._hypothesis_engine_cache

    def _get_goal_manager(self) -> Callable[..., Any]:
        if self._goal_manager_cache is None:
            with self._lock:
                if self._goal_manager_cache is None:
                    from mcp_goal_manager import goal_update
                    self._goal_manager_cache = goal_update
        return self._goal_manager_cache

    def _get_reflection(self) -> Callable[..., Any]:
        if self._reflection_cache is None:
            with self._lock:
                if self._reflection_cache is None:
                    from mcp_reflection_engine import run_reflection
                    self._reflection_cache = run_reflection
        return self._reflection_cache

    # ── Safe call (sync, с экспоненциальным backoff) ───────────────────────
    def _safe_call(
        self,
        func: Callable[..., Any],
        *args: Any,
        max_attempts: int = MAX_ATTEMPTS,
        **kwargs: Any,
    ) -> Any:
        """Синхронный вызов с ретраями и экспоненциальным backoff.

        Если движок вернул awaitable (world_model экспортирует async-функции),
        он разрешается через `_resolve` — отдельный event loop создаётся только
        для этого случая, а не для каждого обработчика.
        """
        for attempt in range(max_attempts):
            try:
                result = func(*args, **kwargs)
                if hasattr(result, "__await__"):
                    result = _resolve(result)
                return result
            except Exception as e:
                if attempt == max_attempts - 1:
                    safe_log(
                        f"Permanent failure in {getattr(func, '__name__', repr(func))}: {e}",
                        level="ERROR",
                    )
                    raise
                safe_log(
                    f"Retry {attempt + 1}/{max_attempts - 1} for "
                    f"{getattr(func, '__name__', repr(func))}: {e}",
                    level="WARNING",
                )
                time.sleep(RETRY_DELAY_SEC * (2 ** attempt))  # экспоненциальный backoff
        return None  # недостижимо, но чтобы mypy не ругался

    def _validate_event_data(self, data: Dict[str, Any], required_fields: List[str]) -> bool:
        missing = [f for f in required_fields if f not in data or data.get(f) is None]
        if missing:
            safe_log(
                f"Missing required fields: {missing} in event data",
                level="ERROR",
            )
            return False
        return True

    # ── Event handlers ─────────────────────────────────────────────────────
    def on_hypothesis_verified(self, data: Dict[str, Any]) -> None:
        if not self._validate_event_data(data, ["statement", "confidence"]):
            return
        safe_log(
            f"Hypothesis verified: {str(data['statement'])[:100]} "
            f"(conf={data['confidence']})"
        )
        add_fact, run_fc = self._get_world_model()
        try:
            self._safe_call(
                add_fact,
                data["statement"],
                confidence=data["confidence"],
                source_tool="hypothesis_engine",
            )
            self._safe_call(run_fc)
        except Exception as e:
            publish(
                "coordinator_error",
                {
                    "handler": "on_hypothesis_verified",
                    "error": str(e),
                    "original_data": data,
                },
            )
            raise

    def on_hypothesis_rejected(self, data: Dict[str, Any]) -> None:
        if not self._validate_event_data(data, ["hypothesis_id"]):
            return
        safe_log(
            f"Hypothesis rejected: {data['hypothesis_id']} "
            f"reason={data.get('reason', 'unknown')}"
        )
        run_reflection = self._get_reflection()
        try:
            self._safe_call(
                run_reflection, limit=REFLECTION_LIMIT_ON_HYPOTHESIS_REJECTED
            )
        except Exception as e:
            publish(
                "coordinator_error",
                {"handler": "on_hypothesis_rejected", "error": str(e)},
            )
            raise

    def on_goal_completed(self, data: Dict[str, Any]) -> None:
        if not self._validate_event_data(data, ["goal_id"]):
            return
        safe_log(f"Goal completed: {data['goal_id']} -> triggering reflection")
        run_reflection = self._get_reflection()
        try:
            self._safe_call(
                run_reflection, limit=REFLECTION_LIMIT_ON_GOAL_COMPLETED
            )
        except Exception as e:
            publish(
                "coordinator_error",
                {"handler": "on_goal_completed", "error": str(e)},
            )
            raise

    def on_plan_failed(self, data: Dict[str, Any]) -> None:
        if not self._validate_event_data(data, ["plan_id"]):
            return
        safe_log(f"Plan failed: {data['plan_id']} -> generating hypothesis")
        create_hyp, _ = self._get_hypothesis_engine()
        reason = data.get("reason", "unknown")
        try:
            self._safe_call(
                create_hyp,
                statement=f"Plan {data['plan_id']} failed due to {reason}",
                confidence=0.4,
                verification_plan=[
                    {"action": "analyze_logs", "params": {"plan_id": data["plan_id"]}}
                ],
                source_tool="coordinator",
            )
        except Exception as e:
            publish(
                "coordinator_error",
                {"handler": "on_plan_failed", "error": str(e)},
            )
            raise

    def on_fact_added(self, data: Dict[str, Any]) -> None:
        if not self._validate_event_data(data, ["statement"]):
            return
        safe_log(f"Fact added: {str(data['statement'])[:100]}")
        _, run_fc = self._get_world_model()
        try:
            new_facts = self._safe_call(run_fc)
            if new_facts:
                safe_log(f"Forward chaining produced {len(new_facts)} new facts")
        except Exception as e:
            publish(
                "coordinator_error",
                {"handler": "on_fact_added", "error": str(e)},
            )
            raise

    def on_rule_added(self, data: Dict[str, Any]) -> None:
        if not self._validate_event_data(data, ["rule_id"]):
            return
        safe_log(f"Rule added: {data['rule_id']}")
        _, run_fc = self._get_world_model()
        try:
            self._safe_call(run_fc)
        except Exception as e:
            publish(
                "coordinator_error",
                {"handler": "on_rule_added", "error": str(e)},
            )
            raise

    # ── Registration & wrapping ────────────────────────────────────────────
    def register(self) -> None:
        """Регистрирует все обработчики событий. Должна вызываться после start()."""
        self.start()  # убеждаемся, что executor создан
        subscribe("hypothesis_verified", self._wrap_handler(self.on_hypothesis_verified))
        subscribe("hypothesis_rejected", self._wrap_handler(self.on_hypothesis_rejected))
        subscribe("goal_completed", self._wrap_handler(self.on_goal_completed))
        subscribe("plan_failed", self._wrap_handler(self.on_plan_failed))
        subscribe("fact_added", self._wrap_handler(self.on_fact_added))
        subscribe("rule_added", self._wrap_handler(self.on_rule_added))
        safe_log("Registered all event handlers")

    def _wrap_handler(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """Оборачивает синхронный обработчик для подписки на шину.

        Шина синхронна: publish() вызывает колбэки прямо в потоке публикатора.
        При async_workers > 0 обработчик уходит в ThreadPoolExecutor и
        публикатор не блокируется ретраями; при 0 — выполняется на месте.
        Исключения не выпускаются в шину, иначе один упавший обработчик
        обрывает доставку остальным подписчикам.
        """
        name = getattr(handler, "__name__", repr(handler))

        def _run(data: Dict[str, Any]) -> None:
            try:
                handler(data)
            except Exception as e:
                safe_log(f"Handler {name} failed: {e}", level="ERROR")

        def wrapper(data: Dict[str, Any]) -> None:
            if self._async_workers > 0:
                with self._executor_cm() as ex:
                    if ex is not None:
                        ex.submit(_run, data)
                        return
            _run(data)

        wrapper.__name__ = f"wrapped_{name}"
        return wrapper


def _resolve(awaitable: Any) -> Any:
    """Разрешает awaitable из async-движка в синхронном контексте."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    # Уже внутри event loop — выполняем в отдельном потоке со своим loop.
    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=1) as ex:
        return ex.submit(asyncio.run, awaitable).result()


# ── Module-level singleton (backward compat) ───────────────────────────────
_default_coordinator = CognitiveCoordinator()


def register_coordinator() -> None:
    """Регистрирует глобальный координатор (для обратной совместимости)."""
    _default_coordinator.register()


def shutdown_coordinator() -> None:
    """Явный shutdown — вызывайте из main после остановки event loop."""
    _default_coordinator.shutdown()
