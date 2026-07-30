# plugins/world_model/plugin.py
"""
Плагин World Model – обёртка над mcp_world_model.
"""
import json
from typing import List, Dict
from plugins.base_plugin import MCPPlugin

# Импортируем реальные функции из модуля world_model
try:
    from mcp_world_model import (
        world_add_fact, world_add_rule, world_run_forward_chaining,
        world_query_facts, world_get_state, world_predict_effects
    )
    WORLD_MODEL_AVAILABLE = True
except ImportError:
    WORLD_MODEL_AVAILABLE = False
    # Заглушки
    async def world_add_fact(*a, **kw): return {"error": "world_model not available"}
    async def world_add_rule(*a, **kw): return {"error": "world_model not available"}
    async def world_run_forward_chaining(*a, **kw): return {"error": "world_model not available"}
    async def world_query_facts(*a, **kw): return {"error": "world_model not available"}
    async def world_get_state(*a, **kw): return {"error": "world_model not available"}
    async def world_predict_effects(*a, **kw): return {"error": "world_model not available"}

class WorldModelPlugin(MCPPlugin):
    @property
    def name(self) -> str:
        return "World Model"

    @property
    def depends_on(self) -> List[str]:
        return []

    async def register_tools(self):
        # Регистрируем инструменты
        self.server.add_tool(self.add_fact)
        self.server.add_tool(self.add_rule)
        self.server.add_tool(self.run_forward_chaining)
        self.server.add_tool(self.query_facts)
        self.server.add_tool(self.get_state)
        self.server.add_tool(self.predict_effects)

    async def register_services(self):
        self.provide_service("world_model.add_fact", self.add_fact)
        self.provide_service("world_model.add_rule", self.add_rule)
        self.provide_service("world_model.query", self.query_facts)
        self.provide_service("world_model.predict", self.predict_effects)

    async def add_fact(self, statement: str, confidence: float = 0.7, source_tool: str = "plugin") -> str:
        """Добавить факт в модель мира."""
        result = await world_add_fact(statement, confidence, source_tool)
        await self.memory_add(f"Факт: {statement} (уверенность {confidence})",
                              metadata={"type": "fact", "confidence": confidence})
        return json.dumps(result)

    async def add_rule(self, condition: str, prediction: str, confidence: float = 0.8) -> str:
        """Добавить правило if-then."""
        result = await world_add_rule(condition, prediction, confidence)
        await self.memory_add(f"Правило: если {condition}, то {prediction}",
                              metadata={"type": "rule"})
        return json.dumps(result)

    async def run_forward_chaining(self) -> str:
        """Запустить вывод новых фактов на основе правил."""
        result = await world_run_forward_chaining()
        return json.dumps(result)

    async def query_facts(self, pattern: str = "", limit: int = 20) -> str:
        """Поиск фактов по шаблону."""
        facts = await world_query_facts(pattern, limit)
        return json.dumps(facts, indent=2, default=str)

    async def get_state(self, query: str = "") -> str:
        """Получить связное описание текущего состояния."""
        state = await world_get_state(query)
        return state

    async def predict_effects(self, action: str, args: Dict = None, context: Dict = None) -> str:
        """Предсказать последствия действия."""
        preds = await world_predict_effects(action, args or {}, context or {})
        return json.dumps(preds, indent=2, default=str)