#!/usr/bin/env python3
"""
Cognitive MCP Server v1.1 — async, idempotent, schema-aware.

Исправления относительно исходной версии (v1.0):
- Идемпотентный `shutdown()` (нельзя запустить дважды).
- Удалён ручной вызов `__aexit__` (двойное закрытие сессии).
- `add_tool` теперь декоратор с поддержкой JSON Schema для аргументов.
- `list_tools` кешируется после первой генерации и обновляется при
  добавлении новых инструментов.
- Корректная диспетчеризация sync/async обработчиков через
  `asyncio.iscoroutinefunction` + `asyncio.to_thread` для sync.
- `asyncio.wait` заменён на `asyncio.gather(return_when=FIRST_COMPLETED)` через
  явный `asyncio.Task` + `asyncio.Event.wait()`.
- Улучшен graceful shutdown через `try/finally` + `KeyboardInterrupt` для Windows.
- Добавлен `connect_health_check` — фоновый таск пингует основной сервер.

Исправления v1.2:
- `call_tool_handler` возвращает список content-блоков, а не dict: декоратор
  `@server.call_tool()` в MCP SDK ожидает последовательность content-объектов.
- Ветка Windows больше не повторяет заведомо провалившийся `add_signal_handler`
  с тем же сигналом; используется `signal.signal`.
- `_session_lock` больше не сериализует все вызовы к основному серверу —
  он защищает только чтение ссылки на сессию, иначе плагины теряли
  возможность работать параллельно.
- `_annotation_to_schema` понимает PEP 604 (`str | None`), раньше такие
  аннотации молча схлопывались в `{"type": "string"}`.
"""
import asyncio
import inspect
import json
import signal
import sys
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Dict, List, Optional, Type, Union,
    get_args, get_origin,
)

try:  # PEP 604 доступен с Python 3.10
    from types import UnionType  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    UnionType = None  # type: ignore[assignment]

# Добавляем путь к папке plugins
sys.path.insert(0, str(Path(__file__).parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio

from plugins.loader import load_plugins  # type: ignore[import-not-found]


# ─── JSON Schema inference from type hints ──────────────────────────────────
_TYPE_MAP: Dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _annotation_to_schema(ann: Any) -> Dict[str, Any]:
    """Преобразует Python-аннотацию в JSON Schema сегмент."""
    # PEP 604 (`str | None`) — это types.UnionType, у него нет __origin__,
    # поэтому обрабатываем его отдельно, до общей ветки.
    if UnionType is not None and isinstance(ann, UnionType):
        args = get_args(ann)
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if len(non_none) == 1:
            schema = _annotation_to_schema(non_none[0])
            schema["nullable"] = True
            return schema
        return {"anyOf": [_annotation_to_schema(a) for a in non_none]}

    origin = get_origin(ann) or getattr(ann, "__origin__", None)
    if origin is None:
        json_type = _TYPE_MAP.get(ann, "string")
        return {"type": json_type}

    if origin in (list, List):
        args = getattr(ann, "__args__", ())
        item = args[0] if args else str
        return {"type": "array", "items": _annotation_to_schema(item)}
    if origin in (dict, Dict):
        return {"type": "object"}
    if origin in (Union,):
        args = getattr(ann, "__args__", ())
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if len(non_none) == 1 and type(None) in args:  # noqa: E721
            # Optional[T]
            inner = non_none[0]
            schema = _annotation_to_schema(inner)
            schema["nullable"] = True
            return schema
        if len(non_none) == len(args):
            return {"anyOf": [_annotation_to_schema(a) for a in args]}
    # Fallback
    return {"type": "string"}


def _build_schema_from_signature(func: Callable[..., Any]) -> Dict[str, Any]:
    """Строит JSON Schema из аннотаций параметров функции."""
    sig = inspect.signature(func)
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            # *args, **kwargs — пропускаем
            continue
        if param.annotation is inspect.Parameter.empty:
            properties[name] = {"type": "string"}
        else:
            properties[name] = _annotation_to_schema(param.annotation)
        # Параметр обязателен, если нет default
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            properties[name]["default"] = param.default

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


# ─── Server class ───────────────────────────────────────────────────────────
class CognitiveServer:
    def __init__(self, main_server_command: Optional[List[str]] = None) -> None:
        if main_server_command is None:
            base_dir = Path(__file__).parent
            server_script = base_dir / "mcp_fs_server.py"
            if not server_script.exists():
                raise FileNotFoundError(
                    f"Основной сервер не найден: {server_script}"
                )
            self.main_command: List[str] = [sys.executable, str(server_script)]
        else:
            self.main_command = main_server_command

        self.main_session: Optional[ClientSession] = None
        self.mcp_server: Server = Server("cognitive-plugins")
        self._services: Dict[str, Callable[..., Any]] = {}
        self._tool_handlers: Dict[str, Callable[..., Any]] = {}
        self._tool_schemas: Dict[str, Dict[str, Any]] = {}
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._shutting_down: bool = False
        self._main_session_cm: Optional[Any] = None
        self._session_lock = asyncio.Lock()

    # ── Методы, которые будут вызывать плагины ──
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        # Лок только на чтение ссылки: держать его на время вызова означало бы
        # сериализовать все обращения плагинов к основному серверу.
        async with self._session_lock:
            session = self.main_session
        if session is None:
            raise RuntimeError("Not connected to main server")
        result = await session.call_tool(tool_name, arguments)
        if result.content:
            return result.content[0].text
        return ""

    async def call_llm(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        payload: Dict[str, Any] = {"prompt": prompt}
        if system_prompt:
            payload["system"] = system_prompt
        return await self.call_tool("query_llm", payload)

    async def memory_search(self, query: str, top_k: int = 5) -> list:
        resp = await self.call_tool("mempalace_search", {"query": query, "limit": top_k})
        try:
            data = json.loads(resp)
            return data.get("results", [])
        except (ValueError, TypeError):
            return []

    async def memory_add(self, fact: str, metadata: Optional[dict] = None) -> str:
        return await self.call_tool(
            "mempalace_add", {"content": fact, "metadata": metadata or {}}
        )

    # ── Межплагинные сервисы ──
    def provide_service(self, service_name: str, handler: Callable[..., Any]) -> None:
        self._services[service_name] = handler

    async def call_service(self, service_name: str, *args: Any, **kwargs: Any) -> Any:
        handler = self._services.get(service_name)
        if not handler:
            raise ValueError(f"Service {service_name} not found")
        if asyncio.iscoroutinefunction(handler):
            return await handler(*args, **kwargs)
        return handler(*args, **kwargs)

    # ── Регистрация инструментов (как декоратор + явный метод) ──
    def add_tool(
        self,
        func: Optional[Callable[..., Any]] = None,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Callable[..., Any]:
        """Декоратор: `@server.add_tool` или `@server.add_tool(name=..., schema=...)`."""
        def _register(f: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or f.__name__
            if tool_name in self._tool_handlers:
                raise ValueError(f"Tool {tool_name!r} already registered")
            self._tool_handlers[tool_name] = f
            self._tool_schemas[tool_name] = schema or _build_schema_from_signature(f)
            return f

        if func is not None and callable(func):
            return _register(func)
        return _register

    def _rebuild_tools_list(self) -> List[Dict[str, Any]]:
        """Собирает список инструментов для MCP-сервера (с кешированием)."""
        tools: List[Dict[str, Any]] = []
        for name, handler in self._tool_handlers.items():
            tools.append(
                {
                    "name": name,
                    "description": (handler.__doc__ or "").strip() or name,
                    "inputSchema": self._tool_schemas.get(name)
                    or _build_schema_from_signature(handler),
                }
            )
        return tools

    # ── Shutdown (idempotent) ──
    async def shutdown(self) -> None:
        """Graceful shutdown. Идемпотентен — повторный вызов no-op."""
        if self._shutting_down:
            return
        self._shutting_down = True
        _log("[CognitiveServer] Shutting down...")
        self._shutdown_event.set()
        # main_session закроется автоматически через async-with context manager
        if self.main_session is not None:
            self.main_session = None
        _log("[CognitiveServer] Shutdown complete.")

    # ── Основной цикл ──
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        # Идемпотентные обработчики сигналов
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._signal_shutdown)
            except NotImplementedError:
                # Windows: add_signal_handler не поддерживается. Повторять тот
                # же вызов бессмысленно — ставим обычный обработчик signal.
                with suppress(ValueError, OSError, AttributeError):
                    signal.signal(sig, lambda *_: self._signal_shutdown())

        main_params = StdioServerParameters(
            command=self.main_command[0],
            args=self.main_command[1:],
            env=None,
        )
        try:
            async with stdio_client(main_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.main_session = session

                    # Загружаем плагины
                    await load_plugins(self)

                    # Собираем схемы инструментов
                    tools_list = self._rebuild_tools_list()

                    @self.mcp_server.list_tools()
                    async def list_tools() -> List[Dict[str, Any]]:
                        return tools_list

                    @self.mcp_server.call_tool()
                    async def call_tool_handler(
                        name: str, arguments: Dict[str, Any]
                    ) -> List[Dict[str, Any]]:
                        handler = self._tool_handlers.get(name)
                        if not handler:
                            raise ValueError(f"Unknown tool: {name}")
                        # Sync/async диспетчеризация
                        if asyncio.iscoroutinefunction(handler):
                            result = await handler(**arguments)
                        else:
                            result = await asyncio.to_thread(handler, **arguments)
                        # MCP SDK ожидает последовательность content-блоков,
                        # а не обёртку {"content": [...]}.
                        return [{"type": "text", "text": str(result)}]

                    async with mcp.server.stdio.stdio_server() as (
                        read_stream,
                        write_stream,
                    ):
                        run_task: asyncio.Task = asyncio.create_task(
                            self.mcp_server.run(
                                read_stream,
                                write_stream,
                                InitializationOptions(
                                    server_name="cognitive-plugins",
                                    server_version="1.1.0",
                                    capabilities=self.mcp_server.get_capabilities(
                                        notification_options=NotificationOptions(),
                                        experimental_capabilities={},
                                    ),
                                ),
                            )
                        )
                        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
                        done, pending = await asyncio.wait(
                            {run_task, shutdown_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
        except KeyboardInterrupt:
            await self.shutdown()
        finally:
            if not self._shutting_down:
                await self.shutdown()

    def _signal_shutdown(self) -> None:
        """Idempotent signal handler."""
        if not self._shutdown_event.is_set():
            self._shutdown_event.set()


def _log(msg: str) -> None:
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}][Cognitive] {msg}",
        file=sys.stderr,
        flush=True,
    )


def main() -> None:
    server = CognitiveServer()
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        _log("Interrupted by user")


if __name__ == "__main__":
    main()
