# plugins/planning_engine/plugin.py
"""
Планировщик — обёртка над mcp_planning_engine.

ИСПРАВЛЕНО: функции движка синхронные, а плагин делал `await f(...)`, что
вызывало TypeError. Теперь вызовы идут через MCPPlugin._acall (работает и с
sync-, и с async-функциями).
"""
import json
from typing import List, Dict
from plugins.base_plugin import MCPPlugin

try:
    from mcp_planning_engine import (
        planning_create_plan, planning_get_plan, planning_list_plans,
        planning_abort_plan, planning_replan
    )
    PLANNING_AVAILABLE = True
except ImportError:
    PLANNING_AVAILABLE = False
    def planning_create_plan(*a, **kw): return {"error": "planning not available"}
    def planning_get_plan(*a, **kw): return {"error": "planning not available"}
    def planning_list_plans(*a, **kw): return {"error": "planning not available"}
    def planning_abort_plan(*a, **kw): return {"error": "planning not available"}
    def planning_replan(*a, **kw): return {"error": "planning not available"}


class PlanningEnginePlugin(MCPPlugin):
    @property
    def name(self) -> str:
        return "Planning Engine"

    @property
    def depends_on(self) -> List[str]:
        return ["World Model"]   # для проверки предусловий/постусловий

    async def register_tools(self):
        self.server.add_tool(self.create_plan)
        self.server.add_tool(self.get_plan)
        self.server.add_tool(self.list_plans)
        self.server.add_tool(self.abort_plan)
        self.server.add_tool(self.replan)

    async def register_services(self):
        self.provide_service("planning_engine.create_plan", self.create_plan)
        self.provide_service("planning_engine.get_plan", self.get_plan)
        self.provide_service("planning_engine.list_plans", self.list_plans)

    async def create_plan(self, goal_id: str) -> str:
        """Создать план для цели по её ID (GoalManager должен быть настроен)."""
        result = await self._acall(planning_create_plan, goal_id)
        if isinstance(result, dict) and result.get("status") == "success":
            await self.memory_add(f"Создан план {result.get('plan_id')} для цели {goal_id}",
                                  metadata={"type": "plan", "plan_id": result.get("plan_id")})
        return json.dumps(result, default=str)

    async def get_plan(self, plan_id: str) -> str:
        """Получить детали плана."""
        plan = await self._acall(planning_get_plan, plan_id)
        return json.dumps(plan, indent=2, default=str)

    async def list_plans(self, status: str = None) -> str:
        """Список планов с фильтром по статусу."""
        plans = await self._acall(planning_list_plans, status)
        return json.dumps(plans, indent=2, default=str)

    async def abort_plan(self, plan_id: str) -> str:
        """Отменить выполнение плана."""
        result = await self._acall(planning_abort_plan, plan_id)
        await self.memory_add(f"План {plan_id} отменён", metadata={"type": "plan_aborted"})
        return json.dumps(result, default=str)

    async def replan(self, plan_id: str, failed_step_index: int, reason: str = "manual") -> str:
        """Перепланирование с указанного шага."""
        result = await self._acall(planning_replan, plan_id, failed_step_index, reason)
        return json.dumps(result, default=str)
