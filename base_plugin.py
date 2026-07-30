# plugins/base_plugin.py
import json
import inspect
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class MCPPlugin(ABC):
    def __init__(self):
        self.server = None

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    def depends_on(self) -> List[str]:
        """Список имён (name) плагинов, от которых зависит данный плагин."""
        return []

    async def register(self, server):
        self.server = server
        required = ['call_tool', 'call_llm', 'memory_search', 'memory_add']
        for m in required:
            if not hasattr(server, m):
                raise RuntimeError(f"Server must implement {m}()")
        await self.register_tools()
        await self.register_services()
        await self.subscribe_events()

    async def register_tools(self):
        pass

    async def register_services(self):
        pass

    async def subscribe_events(self):
        pass

    # ── Универсальный вызов движка ───────────────────────────────────────────
    # Движки проекта неоднородны: world_model экспортирует async-функции,
    # а planning/hypothesis — синхронные. Этот помощник делает вызов корректным
    # в обоих случаях (и для заглушек, объявленных как async def).
    @staticmethod
    async def _acall(fn, *args, **kwargs):
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def call_llm(self, prompt: str, system_prompt: str = None) -> str:
        return await self.server.call_llm(prompt, system_prompt)

    async def memory_search(self, query: str, top_k: int = 5) -> List[Dict]:
        return await self.server.memory_search(query, top_k)

    async def memory_add(self, fact: str, metadata: Dict = None) -> str:
        return await self.server.memory_add(fact, metadata)

    async def call_service(self, service_name: str, *args, **kwargs) -> Any:
        return await self.server.call_service(service_name, *args, **kwargs)

    def provide_service(self, service_name: str, handler):
        self.server.provide_service(service_name, handler)
