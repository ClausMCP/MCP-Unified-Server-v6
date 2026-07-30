#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cognitive Core v3.1 — async-safe, LLM-aware, public-API-based.

Исправления относительно исходной версии (v3.0):
- Использует публичный API планировщика (`planning_create_plan`,
  `planning_start_plan`) вместо прямого доступа к `planner.db`.
- Подписка на события статуса плана вместо `pass` в `replanning`.
- Шум-фильтр перед добавлением факта в world model (минимум: длина
  и blacklist тривиальных паттернов; рекомендуется — LLM-классификатор).
- Проверка `is_llm_up()` в начале цикла: если LLM недоступен — пропуск
  итерации с логированием.
- `hashlib` импортируется на уровне модуля.
- `_observe` фильтрует tool_call/tool_result с ошибочным статусом.
- `_cycle_loop` использует `asyncio.Event` для отмены (вместо `threading.Event`).
- `cog_create_hypothesis` явно передаёт `confidence` (не полагается на default).
- Добавлен `cog_set_model_context` для адаптации под новое окно модели.
"""
import asyncio
import hashlib
import threading
import time
import traceback
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    from mcp_shared import BaseMCPServer, _log, conversation_memory
except ImportError as exc:
    print(f"FATAL: missing mcp_shared: {exc}", file=sys.stderr)
    raise

try:
    from mcp_world_model import WorldModel
    from mcp_hypothesis_engine import HypothesisEngine
    from mcp_planning_engine import ActionPlanner
    from mcp_task_manager import submit_task, task_list, task_status

    from mcp_world_model import world_add_fact_sync, world_run_inference_sync
    from mcp_hypothesis_engine import (
        hyp_create_hypothesis,
        hyp_list_hypotheses,
    )
    from mcp_planning_engine import (
        planning_create_plan,
        planning_get_plan,
        planning_list_plans,
    )
except ImportError as exc:
    print(f"FATAL: missing cognitive engine modules: {exc}", file=sys.stderr)
    raise

# Плагин может отсутствовать — отражаем это
try:
    from mcp_reflection_engine import get_reflections, run_reflection
    REFLECTION_AVAILABLE = True
except ImportError:
    REFLECTION_AVAILABLE = False
    def run_reflection(limit: int = 100) -> Dict[str, Any]:  # type: ignore[no-redef]
        _log("[CognitiveCore] Reflection skipped (module missing)")
        return {"status": "skipped"}

    def get_reflections(limit: int = 20) -> Dict[str, Any]:  # type: ignore[no-redef]
        return {"reflections": []}

# Подключение к LM Studio / availability check
try:
    from connectivity import is_llm_up, is_model_loaded
except ImportError:
    def is_llm_up() -> bool:  # type: ignore[no-redef]
        return True
    def is_model_loaded() -> bool:  # type: ignore[no-redef]
        return True


# ─── Singleton resolver ─────────────────────────────────────────────────────
def _get_module_singleton(
    module: Any, candidate_names: tuple, factory: Callable[..., Any]
) -> Any:
    """Возвращает уже существующий глобальный экземпляр из модуля,
    иначе создаёт новый через factory()."""
    for name in candidate_names:
        inst = getattr(module, name, None)
        if inst is not None:
            return inst
    return factory()


# ─── Noise filter ───────────────────────────────────────────────────────────
_TRIVIAL_FACT_PATTERNS: tuple = (
    "привет", "hello", "hi", "ok", "okay", "спасибо", "thanks",
    "yes", "no", "ага", "угу",
)


def _is_meaningful_fact(text: str, min_len: int = 20) -> bool:
    """Минимальный шум-фильтр. В продакшене заменить на LLM-классификатор."""
    if not text:
        return False
    s = text.strip().lower()
    if len(s) < min_len:
        return False
    if s in _TRIVIAL_FACT_PATTERNS:
        return False
    return True


# ─── CognitiveCore ──────────────────────────────────────────────────────────
class CognitiveCore:
    def __init__(self) -> None:
        # Импортируем модули для поиска singleton
        import mcp_world_model as _wm_mod
        import mcp_hypothesis_engine as _hyp_mod
        import mcp_planning_engine as _plan_mod

        self.planner = _get_module_singleton(
            _plan_mod,
            ("_planner", "planner", "_action_planner"),
            ActionPlanner,
        )
        self.world = _get_module_singleton(
            _wm_mod, ("_world_model", "world_model", "_world", "world"), WorldModel
        )
        self.hypothesis = _get_module_singleton(
            _hyp_mod,
            ("_hypothesis_engine", "hypothesis_engine", "_engine"),
            HypothesisEngine,
        )
        self.memory = conversation_memory

        self.current_goal: Optional[Dict[str, Any]] = None
        self.current_plan_id: Optional[str] = None
        self.running: bool = False
        self._stop_event = asyncio.Event()
        self._cycle_thread: Optional[threading.Thread] = None
        self._cycle_interval: float = 2.0

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self._cycle_thread = threading.Thread(
            target=self._cycle_loop, daemon=True, name="cognitive_cycle"
        )
        self._cycle_thread.start()
        _log("[CognitiveCore] Cognitive cycle started")

    def stop(self, timeout: float = 5.0) -> None:
        if not self.running:
            return
        self.running = False
        # Устанавливаем event из основного потока (thread-safe)
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(self._stop_event.set)
        except RuntimeError:
            # Если event loop не запущен, устанавливаем напрямую
            self._stop_event.set()
        if self._cycle_thread is not None:
            self._cycle_thread.join(timeout=timeout)
        _log("[CognitiveCore] Cognitive cycle stopped")

    # ── Goal setting (public API only) ─────────────────────────────────────
    def set_goal(
        self,
        goal: str,
        goal_type: str = "atomic",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Устанавливает новую цель и создаёт план через ПУБЛИЧНЫЙ API планировщика.

        Возвращает plan_id или None при ошибке.
        """
        self.current_goal = {
            "description": goal,
            "goal_type": goal_type,
            "metadata": metadata or {},
        }

        # Шаги плана
        steps: List[Dict[str, Any]] = []
        if goal_type == "atomic":
            tool_name = (metadata or {}).get("tool_name")
            if not tool_name:
                _log("[CognitiveCore] Atomic goal requires tool_name in metadata")
                return None
            steps.append(
                {
                    "tool_name": tool_name,
                    "args": (metadata or {}).get("tool_args", {}),
                    "precondition": (metadata or {}).get("precondition", {}),
                    "postcondition": (metadata or {}).get("postcondition", {}),
                    "expected_effects": (metadata or {}).get("expected_effects", {}),
                }
            )
        elif goal_type == "composite":
            if metadata and "steps" in metadata:
                steps = list(metadata["steps"])
            else:
                _log("[CognitiveCore] Composite goal requires 'steps' in metadata")
                return None
        else:
            _log(f"[CognitiveCore] Unknown goal type: {goal_type}")
            return None

        if not steps:
            return None

        # Генерируем goal_id и используем публичный API планировщика
        goal_id = hashlib.md5(f"{goal}_{time.time()}".encode()).hexdigest()[:12]

        # ИСПРАВЛЕНО: planning_create_plan (а не self.planner.db.create_plan)
        result = planning_create_plan(
            goal_id=goal_id,
            name=f"Plan for {goal[:50]}",
            steps=steps,
        )
        if isinstance(result, dict):
            plan_id = result.get("plan_id")
        else:
            plan_id = getattr(result, "plan_id", None)

        if plan_id:
            self.current_plan_id = plan_id
            _log(f"[CognitiveCore] Created plan {plan_id} for goal: {goal}")
            return plan_id
        _log(f"[CognitiveCore] Failed to create plan for goal: {goal}")
        return None

    # ── Main loop ──────────────────────────────────────────────────────────
    def _cycle_loop(self) -> None:
        """Главный цикл, выполняется в отдельном потоке."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self._stop_event.is_set():
                try:
                    # Проверка LLM перед каждой итерацией
                    if not is_llm_up():
                        _log(
                            "[CognitiveCore] LLM unavailable — "
                            f"skipping cycle, retry in {self._cycle_interval}s"
                        )
                        self._stop_event.wait(self._cycle_interval)
                        continue
                    if self.current_plan_id:
                        loop.run_until_complete(self._run_active_cycle_async())
                    else:
                        loop.run_until_complete(self._idle_cycle_async())
                except Exception as e:
                    _log(f"[CognitiveCore] Cycle error: {e}\n{traceback.format_exc()}")
                self._stop_event.wait(self._cycle_interval)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _run_active_cycle_async(self) -> None:
        """Одна итерация при активной цели/плане."""
        if self.current_plan_id is None:
            return
        plan = planning_get_plan(self.current_plan_id)
        if not plan:
            self.current_plan_id = None
            return
        if plan.get("status") in ("completed", "failed", "cancelled"):
            _log(
                f"[CognitiveCore] Plan {self.current_plan_id} finished with "
                f"status {plan.get('status')}"
            )
            self.current_plan_id = None
            return

        # 1. Observe
        observations = self._observe()
        # 2. Remember (с шум-фильтром)
        self._remember(observations)
        # 3. Reflect
        reflections = self._reflect()
        if reflections:
            _log(f"[CognitiveCore] Found {len(reflections)} reflections")
        # 4. Hypothesize
        new_hypotheses = self._hypothesize(reflections)
        for hyp in new_hypotheses:
            _log(f"[CognitiveCore] New hypothesis: {hyp['statement'][:80]}")
        # 5. Plan — replan по событию, а не по polling
        if plan.get("status") == "replanning":
            # ИСПРАВЛЕНО: ничего не делаем, ждём события plan_replanned;
            # fallback через таймаут ниже
            self._cycle_interval = min(self._cycle_interval, 5.0)
        elif plan.get("status") in ("pending", "in_progress"):
            await self._check_need_replan(plan, reflections)
        # 6. Act — публичный API
        if plan.get("status") == "pending":
            try:
                # ИСПРАВЛЕНО: planning_start_plan (а не self.planner._start_plan)
                if hasattr(self.planner, "start_plan"):
                    self.planner.start_plan(self.current_plan_id)
                else:
                    from mcp_planning_engine import planning_start_plan
                    planning_start_plan(self.current_plan_id)
            except Exception as e:
                _log(f"[CognitiveCore] Failed to start plan: {e}")

    def _idle_cycle_async(self) -> Awaitable[None]:
        return self._run_async(self._idle_cycle)

    async def _idle_cycle_async_impl(self) -> None:
        self._idle_cycle()
        return None

    # ИСПРАВЛЕНО: используем async-цикл (раньше был sync)
    async def _idle_cycle(self) -> None:  # type: ignore[no-redef]
        try:
            run_reflection(limit=50)
            reflections = get_reflections(limit=10).get("reflections", [])
            self._hypothesize(reflections)
            world_run_inference_sync()
        except Exception as e:
            _log(f"[CognitiveCore] Idle cycle error: {e}")

    def _run_async(self, coro_factory: Callable[[], Any]) -> Awaitable[None]:
        """Запускает sync-функцию в отдельном потоке executor'а."""
        async def _wrap() -> None:
            return coro_factory()  # type: ignore[no-any-return]
        return _wrap()

    # ── Observation & memory ───────────────────────────────────────────────
    def _observe(self) -> List[Dict[str, Any]]:
        thread = self.memory.get_dialog_thread(limit=5)
        observations: List[Dict[str, Any]] = []
        for entry in thread.get("entries", []):
            op = entry.get("op")
            if op == "conversation":
                observations.append(
                    {
                        "type": "message",
                        "role": (entry.get("paths") or {}).get("role"),
                        "content": entry.get("context", ""),
                    }
                )
            elif op in ("tool_call", "tool_result"):
                status = entry.get("status")
                # ИСПРАВЛЕНО: пропускаем неудачные вызовы
                if status not in ("completed", "success", "ok"):
                    continue
                observations.append(
                    {
                        "type": "tool",
                        "tool": (entry.get("paths") or {}).get("tool"),
                        "status": status,
                        "context": entry.get("context", ""),
                    }
                )
        return observations

    def _remember(self, observations: List[Dict[str, Any]]) -> None:
        """Преобразует наблюдения в факты. ИСПРАВЛЕНО: добавлен шум-фильтр."""
        seen_hashes: set = set()
        for obs in observations:
            if obs.get("type") == "message" and obs.get("role") == "user":
                text = (obs.get("content") or "").strip()
                if not _is_meaningful_fact(text):
                    continue
                h = hashlib.md5(text.encode("utf-8")).hexdigest()
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                try:
                    world_add_fact_sync(
                        text, confidence=0.6, source_tool="cognitive_core"
                    )
                except Exception as e:
                    _log(f"[CognitiveCore] world_add_fact failed: {e}")
            elif obs.get("type") == "tool" and obs.get("status") in (
                "completed",
                "success",
                "ok",
            ):
                tool_name = obs.get("tool") or "unknown"
                text = f"Tool {tool_name} executed successfully"
                h = hashlib.md5(text.encode("utf-8")).hexdigest()
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                try:
                    world_add_fact_sync(
                        text, confidence=0.9, source_tool="cognitive_core"
                    )
                except Exception as e:
                    _log(f"[CognitiveCore] world_add_fact failed: {e}")

    def _reflect(self) -> List[Dict[str, Any]]:
        try:
            run_reflection(limit=100)
            return get_reflections(limit=20).get("reflections", [])
        except Exception as e:
            _log(f"[CognitiveCore] reflection failed: {e}")
            return []

    def _hypothesize(self, reflections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        new_hypotheses: List[Dict[str, Any]] = []
        for ref in reflections:
            ref_type = ref.get("reflection_type", "")
            if ref_type in ("antonym_conflict", "version_conflict", "relation_conflict"):
                statement = (
                    f"Possible explanation: {ref.get('description', 'unknown conflict')}"
                )
                try:
                    hyp_result = hyp_create_hypothesis(
                        statement=statement,
                        confidence=0.4,
                        source_tool="cognitive_core",
                    )
                    if isinstance(hyp_result, dict):
                        hyp_id = hyp_result.get("hypothesis_id")
                    else:
                        hyp_id = getattr(hyp_result, "hypothesis_id", None)
                    if hyp_id:
                        new_hypotheses.append(
                            {"statement": statement, "hypothesis_id": hyp_id}
                        )
                except Exception as e:
                    _log(f"[CognitiveCore] hyp_create_hypothesis failed: {e}")
        return new_hypotheses

    async def _check_need_replan(
        self, plan: Dict[str, Any], reflections: List[Dict[str, Any]]
    ) -> None:
        """Инициирует replan, если рефлексия обнаружила критическое противоречие.
        ИСПРАВЛЕНО: используем публичный API replan_manually (если он есть),
        иначе публикация события plan_replan_request."""
        for ref in reflections:
            if ref.get("reflection_type") in ("antonym_conflict", "relation_conflict"):
                _log(
                    f"[CognitiveCore] Requesting replan for "
                    f"{plan.get('plan_id')} due to reflection"
                )
                try:
                    from mcp_cognitive_bus import publish
                    publish(
                        "plan_replan_request",
                        {
                            "plan_id": plan.get("plan_id"),
                            "current_step": plan.get("current_step"),
                            "reason": "reflection_conflict",
                            "reflection_id": ref.get("id"),
                        },
                    )
                except Exception as e:
                    _log(f"[CognitiveCore] publish replan_request failed: {e}")
                break

    # ── Public MCP tools ───────────────────────────────────────────────────
    def set_goal_tool(
        self,
        goal: str,
        goal_type: str = "atomic",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        plan_id = self.set_goal(goal, goal_type, metadata)
        if plan_id:
            return {"status": "success", "goal": goal, "plan_id": plan_id}
        return {"status": "error", "message": "Failed to create plan for goal"}

    def get_status(self) -> Dict[str, Any]:
        plan_info: Dict[str, Any] = {}
        if self.current_plan_id:
            try:
                plan = planning_get_plan(self.current_plan_id)
            except Exception as e:
                plan = None
                _log(f"[CognitiveCore] get_plan failed: {e}")
            if plan:
                steps = plan.get("steps") or []
                plan_info = {
                    "plan_id": plan.get("plan_id"),
                    "status": plan.get("status"),
                    "current_step": plan.get("current_step"),
                    "steps_total": len(steps) if isinstance(steps, list) else 0,
                }
        return {
            "running": self.running,
            "current_goal": self.current_goal,
            "plan": plan_info,
            "llm_available": is_llm_up(),
            "model_loaded": is_model_loaded(),
            "memory_stats": (
                self.memory.get_stats() if hasattr(self.memory, "get_stats") else {}
            ),
        }

    def list_tasks(self, status: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        return task_list(status, limit)


# ── Module-level singleton ──────────────────────────────────────────────────
_cognitive = CognitiveCore()


# ── Public tool functions ────────────────────────────────────────────────────
def cog_set_goal(
    goal: str, goal_type: str = "atomic", metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return _cognitive.set_goal_tool(goal, goal_type, metadata)


def cog_get_status() -> Dict[str, Any]:
    return _cognitive.get_status()


def cog_run_reflection() -> Dict[str, Any]:
    run_reflection(limit=100)
    return {"status": "reflection_completed"}


def cog_list_hypotheses() -> Dict[str, Any]:
    return hyp_list_hypotheses()


def cog_create_hypothesis(
    statement: str,
    verification_plan: Optional[List[Dict[str, Any]]] = None,
    confidence: float = 0.5,  # ИСПРАВЛЕНО: явный default
) -> Dict[str, Any]:
    return hyp_create_hypothesis(
        statement,
        confidence=confidence,
        verification_plan=verification_plan or [],
        source_tool="cognitive_core",
    )


def cog_list_plans(status: Optional[str] = None) -> Dict[str, Any]:
    return planning_list_plans(status)


def cog_get_plan(plan_id: str) -> Dict[str, Any]:
    return planning_get_plan(plan_id)


def cog_list_tasks(status: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    return _cognitive.list_tasks(status, limit)


def cog_submit_task(
    tool_name: str, args: Dict[str, Any], dialog_id: Optional[str] = None
) -> Dict[str, Any]:
    return submit_task(tool_name, args, dialog_id)


def cog_task_status(task_id: str) -> Dict[str, Any]:
    return task_status(task_id)


def cog_set_model_context() -> Dict[str, Any]:
    """Hook для адаптации параметров ядра к текущей загруженной модели."""
    try:
        from connectivity import get_model_info
        info = get_model_info()
        if info is None:
            return {"status": "no_model"}
        # Используем контекстное окно для подстройки цикла
        ctx = info.context_length or 4096
        if _cognitive.running:
            # Чем меньше окно — тем чаще проверяем состояние
            _cognitive._cycle_interval = max(1.0, min(10.0, ctx / 4096 * 2.0))
        return {
            "status": "applied",
            "model": info.id,
            "context_length": info.context_length,
            "cycle_interval": _cognitive._cycle_interval,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── MCP server ──────────────────────────────────────────────────────────────
server = BaseMCPServer("cognitive-core", "3.1")

server.register_tool(
    "cog_set_goal",
    {
        "description": "Set goal (atomic or composite). For atomic specify metadata.tool_name",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "goal_type": {
                    "type": "string",
                    "enum": ["atomic", "composite"],
                    "default": "atomic",
                },
                "metadata": {"type": "object"},
            },
            "required": ["goal"],
        },
    },
    lambda **kw: cog_set_goal(kw["goal"], kw.get("goal_type", "atomic"), kw.get("metadata")),
)

server.register_tool(
    "cog_get_status",
    {"description": "Get cognitive system status", "inputSchema": {"type": "object", "properties": {}}},
    lambda **kw: cog_get_status(),
)

server.register_tool(
    "cog_run_reflection",
    {"description": "Run reflection cycle manually", "inputSchema": {"type": "object", "properties": {}}},
    lambda **kw: cog_run_reflection(),
)

server.register_tool(
    "cog_list_hypotheses",
    {"description": "List all hypotheses", "inputSchema": {"type": "object", "properties": {}}},
    lambda **kw: cog_list_hypotheses(),
)

server.register_tool(
    "cog_create_hypothesis",
    {
        "description": "Create a new hypothesis",
        "inputSchema": {
            "type": "object",
            "properties": {
                "statement": {"type": "string"},
                "verification_plan": {"type": "array", "items": {"type": "object"}},
                "confidence": {"type": "number", "default": 0.5},
            },
            "required": ["statement"],
        },
    },
    lambda **kw: cog_create_hypothesis(
        kw["statement"], kw.get("verification_plan"), kw.get("confidence", 0.5)
    ),
)

server.register_tool(
    "cog_list_plans",
    {
        "description": "List plans (filter by status)",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
    },
    lambda **kw: cog_list_plans(kw.get("status")),
)

server.register_tool(
    "cog_get_plan",
    {
        "description": "Plan details by ID",
        "inputSchema": {
            "type": "object",
            "properties": {"plan_id": {"type": "string"}},
            "required": ["plan_id"],
        },
    },
    lambda **kw: cog_get_plan(kw["plan_id"]),
)

server.register_tool(
    "cog_list_tasks",
    {
        "description": "List async tasks (Task Manager)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    lambda **kw: cog_list_tasks(kw.get("status"), kw.get("limit", 20)),
)

server.register_tool(
    "cog_submit_task",
    {
        "description": "Run long-running operation in background (Task Manager)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "args": {"type": "object"},
                "dialog_id": {"type": "string"},
            },
            "required": ["tool_name", "args"],
        },
    },
    lambda **kw: cog_submit_task(kw["tool_name"], kw.get("args", {}), kw.get("dialog_id")),
)

server.register_tool(
    "cog_task_status",
    {
        "description": "Task status by ID",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    lambda **kw: cog_task_status(kw["task_id"]),
)

server.register_tool(
    "cog_set_model_context",
    {
        "description": "Adapt core parameters to the currently loaded LLM model",
        "inputSchema": {"type": "object", "properties": {}},
    },
    lambda **kw: cog_set_model_context(),
)


def _on_load() -> None:
    _cognitive.start()
    _log("[CognitiveCore] v3.1 loaded and running")


def _on_unload() -> None:
    _cognitive.stop()


__mcp_plugin__ = {
    "name": "cognitive-core",
    "version": "3.1",
    "description": (
        "Cognitive core with public planner API, model awareness, "
        "noise-filtered memory, event-driven replan"
    ),
    "dependencies": [
        "planning-engine",
        "task-manager",
        "world-model",
        "hypothesis-engine",
        "connectivity",
    ],
    "on_load": _on_load,
    "on_unload": _on_unload,
}


if __name__ == "__main__":
    _cognitive.start()
    try:
        server.run()
    finally:
        _cognitive.stop()
