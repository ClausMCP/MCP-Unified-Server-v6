# plugins/base_plugin.py
"""
Базовый класс плагина для MCP-сервера.

Исправления относительно исходной версии:
- Удалён неиспользуемый импорт `json`.
- Контракт сервера проверяется через `callable(...)`, а не только `hasattr`.
- Добавлена защита от повторной регистрации плагина.
- Унифицирована обработка ошибок в `_acall` (корректная работа с async/sync движками).
"""
import inspect
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, List, Optional


class MCPPlugin(ABC):
    # Методы, которые сервер обязан предоставлять плагину.
    REQUIRED_SERVER_METHODS: tuple = (
        "call_tool",
        "call_llm",
        "memory_search",
        "memory_add",
    )

    def __init__(self) -> None:
        self.server: Optional[Any] = None
        self._registered: bool = False

    @property
    @abstractmethod
    def name(self) -> str:
        """Уникальное имя плагина (используется в зависимостях)."""
        pass

    @property
    def depends_on(self) -> List[str]:
        """Список имён плагинов, от которых зависит данный плагин."""
        return []

    async def register(self, server: Any) -> None:
        """
        Регистрирует плагин в сервере. Идемпотентно — повторный вызов
        бросает RuntimeError, а не молча перерегистрирует обработчики.
        """
        if self._registered:
            raise RuntimeError(f"Plugin {self.name!r} already registered")
        if server is None:
            raise RuntimeError("Server is None")

        for method_name in self.REQUIRED_SERVER_METHODS:
            attr = getattr(server, method_name, None)
            if not callable(attr):
                raise RuntimeError(
                    f"Server must implement {method_name}() as a callable"
                )

        self.server = server
        await self.register_tools()
        await self.register_services()
        await self.subscribe_events()
        self._registered = True

    async def register_tools(self) -> None:
        """Переопределяется в наследнике для регистрации MCP-инструментов."""

    async def register_services(self) -> None:
        """Переопределяется в наследнике для регистрации межплагинных сервисов."""

    async def subscribe_events(self) -> None:
        """Переопределяется в наследнике для подписки на события Cognitive Bus."""

    # ── Универсальный вызов движка ──────────────────────────────────────────
    # Движки проекта неоднородны: world_model экспортирует async-функции,
    # а planning/hypothesis — синхронные. Этот помощник корректен в обоих
    # случаях (и для заглушек, объявленных как `async def`).
    @staticmethod
    async def _acall(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result  # type: ignore[return-value]
        return result

    # ── Безопасные обёртки над методами сервера ─────────────────────────────
    async def call_llm(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        if self.server is None:
            raise RuntimeError(f"Plugin {self.name!r} is not registered")
        return await self.server.call_llm(prompt, system_prompt)

    async def memory_search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        if self.server is None:
            raise RuntimeError(f"Plugin {self.name!r} is not registered")
        return await self.server.memory_search(query, top_k)

    async def memory_add(self, fact: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        if self.server is None:
            raise RuntimeError(f"Plugin {self.name!r} is not registered")
        return await self.server.memory_add(fact, metadata or {})

    async def call_service(self, service_name: str, *args: Any, **kwargs: Any) -> Any:
        if self.server is None:
            raise RuntimeError(f"Plugin {self.name!r} is not registered")
        return await self.server.call_service(service_name, *args, **kwargs)

    def provide_service(self, service_name: str, handler: Callable[..., Any]) -> None:
        if self.server is None:
            raise RuntimeError(f"Plugin {self.name!r} is not registered")
        self.server.provide_service(service_name, handler)
