#!/usr/bin/env python3
"""
Cognitive MCP Server – предоставляет инструменты планирования, гипотез и модели мира.
Запускается отдельно и использует основной сервер через MCP-клиент.
"""
import asyncio
import json
import sys
import os
import signal
from pathlib import Path

# Добавляем путь к папке plugins
sys.path.insert(0, str(Path(__file__).parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio

from plugins.loader import load_plugins

class CognitiveServer:
    """
    Асинхронный MCP-сервер, который:
    - Загружает ваши плагины (Planning, Hypothesis, WorldModel)
    - Предоставляет плагинам методы call_tool, call_llm, memory_* через клиент к основному серверу
    - Регистрирует инструменты плагинов как свои
    """
    def __init__(self, main_server_command: list = None):
        if main_server_command is None:
            # Автоматически определяем путь к mcp_fs_server.py
            base_dir = Path(__file__).parent
            server_script = base_dir / "mcp_fs_server.py"
            if not server_script.exists():
                raise FileNotFoundError(f"Не найден основной сервер: {server_script}")
            self.main_command = [sys.executable, str(server_script)]
        else:
            self.main_command = main_server_command
        self.main_session = None
        self.mcp_server = Server("cognitive-plugins")
        self._services = {}          # для межплагинных вызовов
        self._tool_handlers = {}     # инструменты, зарегистрированные плагинами
        self._shutdown_event = asyncio.Event()

    # ----- Методы, которые будут вызывать плагины -----
    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Вызвать инструмент на основном сервере."""
        if not self.main_session:
            raise RuntimeError("Not connected to main server")
        result = await self.main_session.call_tool(tool_name, arguments)
        if result.content:
            return result.content[0].text
        return ""

    async def call_llm(self, prompt: str, system_prompt: str = None) -> str:
        """Вызвать LLM через основной сервер (предполагаем, что там есть инструмент query_llm)."""
        payload = {"prompt": prompt}
        if system_prompt:
            payload["system"] = system_prompt
        return await self.call_tool("query_llm", payload)

    async def memory_search(self, query: str, top_k: int = 5) -> list:
        """Поиск через mempalace_search."""
        resp = await self.call_tool("mempalace_search", {"query": query, "limit": top_k})
        data = json.loads(resp)
        return data.get("results", [])

    async def memory_add(self, fact: str, metadata: dict = None) -> str:
        """Добавить факт в память."""
        resp = await self.call_tool("mempalace_add", {"content": fact, "metadata": metadata or {}})
        return resp

    # ----- Методы для межплагинных вызовов -----
    def provide_service(self, service_name: str, handler):
        self._services[service_name] = handler

    async def call_service(self, service_name: str, *args, **kwargs):
        handler = self._services.get(service_name)
        if not handler:
            raise ValueError(f"Service {service_name} not found")
        return await handler(*args, **kwargs)

    # ----- Регистрация инструментов, добавляемых плагинами -----
    def add_tool(self, func):
        """Обёртка для добавления инструмента в MCP-сервер."""
        tool_name = func.__name__
        self._tool_handlers[tool_name] = func
        return func

    async def shutdown(self):
        """Graceful shutdown: закрываем соединение с основным сервером."""
        _log("[CognitiveServer] Shutting down...")
        self._shutdown_event.set()
        if self.main_session:
            try:
                await self.main_session.__aexit__(None, None, None)
            except Exception as e:
                _log(f"[CognitiveServer] Error closing session: {e}")
        _log("[CognitiveServer] Shutdown complete.")

    # ----- Основной цикл -----
    async def run(self):
        # Настройка обработчиков сигналов для graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
            except NotImplementedError:
                # Windows не поддерживает add_signal_handler для SIGTERM
                pass

        # 1. Подключаемся к основному серверу
        main_params = StdioServerParameters(
            command=self.main_command[0],
            args=self.main_command[1:],
            env=None
        )
        async with stdio_client(main_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self.main_session = session

                # 2. Загружаем плагины (они будут вызывать self.add_tool, self.provide_service)
                await load_plugins(self)

                # 3. Регистрируем инструменты в mcp.Server
                tools_list = []
                for name, handler in self._tool_handlers.items():
                    from inspect import signature
                    sig = signature(handler)
                    properties = {}
                    required = []
                    for param in sig.parameters.values():
                        if param.name in ('self', 'cls'):
                            continue
                        properties[param.name] = {"type": "string", "description": param.name}
                        required.append(param.name)
                    tools_list.append({
                        "name": name,
                        "description": handler.__doc__ or "",
                        "inputSchema": {
                            "type": "object",
                            "properties": properties,
                            "required": required
                        }
                    })

                @self.mcp_server.list_tools()
                async def list_tools():
                    return tools_list

                @self.mcp_server.call_tool()
                async def call_tool(name: str, arguments: dict):
                    handler = self._tool_handlers.get(name)
                    if not handler:
                        raise ValueError(f"Unknown tool: {name}")
                    result = await handler(**arguments)
                    return {"content": [{"type": "text", "text": str(result)}]}

                # 4. Запускаем сервер и ждём сигнала завершения
                async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                    run_task = asyncio.create_task(
                        self.mcp_server.run(
                            read_stream,
                            write_stream,
                            InitializationOptions(
                                server_name="cognitive-plugins",
                                server_version="1.0.0",
                                capabilities=self.mcp_server.get_capabilities(
                                    notification_options=NotificationOptions(),
                                    experimental_capabilities={},
                                ),
                            ),
                        )
                    )
                    # Ожидаем либо завершения сервера, либо сигнала shutdown
                    await asyncio.wait([run_task, self._shutdown_event.wait()], return_when=asyncio.FIRST_COMPLETED)
                    run_task.cancel()

def _log(msg: str):
    """Простое логирование в stderr."""
    import sys
    from datetime import datetime
    print(f"[{datetime.now().strftime('%H:%M:%S')}][Cognitive] {msg}", file=sys.stderr, flush=True)

def main():
    # Путь к основному серверу определяется автоматически в __init__
    server = CognitiveServer()
    asyncio.run(server.run())

if __name__ == "__main__":
    main()