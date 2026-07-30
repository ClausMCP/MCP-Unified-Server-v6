# plugins/world_model/plugin.py
"""
Плагин World Model — обёртка над mcp_world_model.

ИСПРАВЛЕНО: импортировались несуществующие имена
(world_run_forward_chaining, world_query_facts, world_get_state,
world_predict_effects), из-за чего весь импорт падал в ImportError и плагин
работал только заглушками. Теперь импортируются реальные async-функции движка.
"""
import json
from typing import List, Dict
from plugins.base_plugin import MCPPlugin

try:
    from mcp_world_model import (
        world_add_fact, world_add_rule, world_list_rules, world_delete_rule,
        world_run_inference, world_get_predictions, world_add_action_effect,
        world_simulate_action, world_backward_chain,
    )
    WORLD_MODEL_AVAILABLE = True
except ImportError:
    WORLD_MODEL_AVAILABLE = False
    async def world_add_fact(*a, **kw): return {"error": "world_model not available"}
    async def world_add_rule(*a, **kw): return {"error": "world_model not available"}
    async def world_list_rules(*a, **kw): return {"error": "world_model not available"}
    async def world_delete_rule(*a, **kw): return {"error": "world_model not available"}
    async def world_run_inference(*a, **kw): return {"error": "world_model not available"}
    async def world_get_predictions(*a, **kw): return {"error": "world_model not available"}
    async def world_add_action_effect(*a, **kw): return {"error": "world_model not available"}
    async def world_simulate_action(*a, **kw): return {"error": "world_model not available"}
    async def world_backward_chain(*a, **kw): return {"error": "world_model not available"}


class WorldModelPlugin(MCPPlugin):
    @property
    def name(self) -> str:
        return "World Model"

    @property
    def depends_on(self) -> List[str]:
        return []

    async def register_tools(self):
        self.server.add_tool(self.add_fact)
        self.server.add_tool(self.add_rule)
        self.server.add_tool(self.list_rules)
        self.server.add_tool(self.delete_rule)
        self.server.add_tool(self.run_inference)
        self.server.add_tool(self.get_predictions)
        self.server.add_tool(self.simulate_action)
        self.server.add_tool(self.backward_chain)
        self.server.add_tool(self.add_action_effect)

    async def register_services(self):
        self.provide_service("world_model.add_fact", self.add_fact)
        self.provide_service("world_model.add_rule", self.add_rule)
        self.provide_service("world_model.run_inference", self.run_inference)
        self.provide_service("world_model.predict", self.simulate_action)

    async def add_fact(self, statement: str, confidence: float = 0.9, source_tool: str = "plugin") -> str:
        """Добавить факт в модель мира."""
        result = await self._acall(world_add_fact, statement, confidence, source_tool)
        await self.memory_add(f"Факт: {statement} (уверенность {confidence})",
                              metadata={"type": "fact", "confidence": confidence})
        return json.dumps(result, default=str)

    async def add_rule(self, condition, conclusion: str, confidence: float = 0.8) -> str:
        """Добавить правило if-then. condition может быть строкой или Dict."""
        if isinstance(condition, str):
            condition = {"type": "fact", "statement": condition}
        result = await self._acall(world_add_rule, condition, conclusion, confidence)
        await self.memory_add(f"Правило: если {condition}, то {conclusion}",
                              metadata={"type": "rule"})
        return json.dumps(result, default=str)

    async def list_rules(self) -> str:
        """Список всех правил."""
        result = await self._acall(world_list_rules)
        return json.dumps(result, indent=2, default=str)

    async def delete_rule(self, rule_id: str) -> str:
        """Удалить правило по ID."""
        result = await self._acall(world_delete_rule, rule_id)
        return json.dumps(result, default=str)

    async def run_inference(self) -> str:
        """Прямой вывод новых фактов на основе правил (forward chaining)."""
        result = await self._acall(world_run_inference)
        return json.dumps(result, default=str)

    async def get_predictions(self, limit: int = 50) -> str:
        """Получить сделанные предсказания."""
        result = await self._acall(world_get_predictions, limit)
        return json.dumps(result, indent=2, default=str)

    async def simulate_action(self, tool_name: str, args: Dict = None) -> str:
        """Предсказать последствия действия (инструмента)."""
        result = await self._acall(world_simulate_action, tool_name, args or {})
        return json.dumps(result, indent=2, default=str)

    async def backward_chain(self, goal: str, max_depth: int = 5) -> str:
        """Обратный вывод: найти цепочку, доказывающую цель."""
        result = await self._acall(world_backward_chain, goal, max_depth)
        return json.dumps(result, indent=2, default=str)

    async def add_action_effect(self, tool_name: str, args_pattern: Dict,
                                effect_type: str, effect_target: Dict,
                                confidence: float = 0.5) -> str:
        """Описать эффект действия (для предсказаний)."""
        result = await self._acall(world_add_action_effect, tool_name, args_pattern,
                                   effect_type, effect_target, confidence)
        return json.dumps(result, default=str)
