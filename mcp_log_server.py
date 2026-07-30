#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Log Server v1.0 – Централизованное логирование с ротацией
Принимает логи по UDP (порт 9010), пишет в файлы с ротацией по размеру,
поддерживает фильтрацию по уровню и модулю, поиск по логам.
Может также принимать HTTP POST на /log для интеграции.
"""
import os
import sys
import json
import time
import re
import socket
import threading
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Конфигурация ─────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("MCP_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))
UDP_PORT = int(os.environ.get("MCP_LOG_UDP_PORT", "9010"))
HTTP_PORT = int(os.environ.get("MCP_LOG_HTTP_PORT", "9011"))
MAX_LOG_SIZE_MB = int(os.environ.get("MCP_LOG_MAX_SIZE_MB", "10"))
BACKUP_COUNT = int(os.environ.get("MCP_LOG_BACKUP_COUNT", "5"))
LOG_LEVEL = os.environ.get("MCP_LOG_LEVEL", "INFO").upper()

# Уровни логирования, которые будут записываться
LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL
}
DEFAULT_LEVEL = LEVELS.get(LOG_LEVEL, logging.INFO)

# ─── Инициализация директории для логов ──────────────────────────────────
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

# ─── Настройка ротации логов (RotatingFileHandler) ───────────────────────
log_formatter = logging.Formatter(
    fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Основной лог-файл: mcp.log
main_log_path = os.path.join(LOG_DIR, "mcp.log")
main_handler = logging.handlers.RotatingFileHandler(
    main_log_path, maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024, backupCount=BACKUP_COUNT, encoding='utf-8'
)
main_handler.setFormatter(log_formatter)
main_handler.setLevel(DEFAULT_LEVEL)

# Лог ошибок: ошибки дублируются в mcp_error.log (только ERROR и выше)
error_log_path = os.path.join(LOG_DIR, "mcp_error.log")
error_handler = logging.handlers.RotatingFileHandler(
    error_log_path, maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024, backupCount=BACKUP_COUNT, encoding='utf-8'
)
error_handler.setFormatter(log_formatter)
error_handler.setLevel(logging.ERROR)

# Корневой логгер
root_logger = logging.getLogger()
root_logger.setLevel(DEFAULT_LEVEL)
root_logger.addHandler(main_handler)
root_logger.addHandler(error_handler)

# Отдельный логгер для MCP (для удобства)
mcp_logger = logging.getLogger("MCP")
mcp_logger.setLevel(DEFAULT_LEVEL)

# ─── UDP сервер для приёма логов ─────────────────────────────────────────
class UDPLogServer:
    def __init__(self, host='0.0.0.0', port=UDP_PORT):
        self.host = host
        self.port = port
        self.socket = None
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.port))
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="udp_log_server")
        self.thread.start()
        _log(f"[LogServer] UDP log receiver started on port {self.port}")

    def stop(self):
        self.running = False
        if self.socket:
            self.socket.close()
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self):
        while self.running:
            try:
                data, addr = self.socket.recvfrom(65535)
                self._handle_log(data, addr)
            except OSError:
                break
            except Exception as e:
                _log(f"[LogServer] UDP error: {e}")

    def _handle_log(self, data: bytes, addr):
        try:
            payload = json.loads(data.decode('utf-8'))
            # Ожидаемый формат: {"level": "INFO", "module": "mcp_web_reader", "message": "...", "dialog": "..."}
            level_name = payload.get("level", "INFO").upper()
            level = LEVELS.get(level_name, logging.INFO)
            module = payload.get("module", "unknown")
            message = payload.get("message", "")
            dialog = payload.get("dialog", "")
            extra = payload.get("extra", {})

            # Формируем запись в логгере
            log_record = logging.LogRecord(
                name=f"MCP.{module}",
                level=level,
                pathname="",
                lineno=0,
                msg=message,
                args=(),
                exc_info=None
            )
            log_record.__dict__["dialog_id"] = dialog
            log_record.__dict__["extra"] = extra
            root_logger.handle(log_record)
        except json.JSONDecodeError:
            # Если не JSON, пробуем как plain text
            line = data.decode('utf-8', errors='replace').strip()
            if line:
                mcp_logger.info(f"[UDP] {line}")
        except Exception as e:
            mcp_logger.error(f"Failed to process UDP log: {e}")

# ─── HTTP сервер для приёма логов (альтернативный) ───────────────────────
class HTTPLogHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/log':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
                level = data.get("level", "INFO").upper()
                module = data.get("module", "http")
                message = data.get("message", "")
                dialog = data.get("dialog", "")
                level_int = LEVELS.get(level, logging.INFO)
                log_record = logging.LogRecord(
                    name=f"MCP.{module}",
                    level=level_int,
                    pathname="",
                    lineno=0,
                    msg=message,
                    args=(),
                    exc_info=None
                )
                log_record.__dict__["dialog_id"] = dialog
                root_logger.handle(log_record)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f'{{"error":"{e}"}}'.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Переопределяем, чтобы логи HTTP сервера не спамили
        pass

def start_http_server(port=HTTP_PORT):
    httpd = HTTPServer(('0.0.0.0', port), HTTPLogHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="http_log_server")
    thread.start()
    _log(f"[LogServer] HTTP log receiver started on port {port}")
    return httpd

# ─── Глобальные экземпляры ───────────────────────────────────────────────
_udp_server = UDPLogServer()
_httpd = None

def start_log_server() -> Dict:
    """Запустить UDP и HTTP серверы приёма логов."""
    global _httpd
    _udp_server.start()
    if _httpd is None:
        _httpd = start_http_server()
    return {"status": "started", "udp_port": UDP_PORT, "http_port": HTTP_PORT}

def stop_log_server() -> Dict:
    """Остановить серверы логирования."""
    _udp_server.stop()
    global _httpd
    if _httpd:
        _httpd.shutdown()
        _httpd = None
    return {"status": "stopped"}

# ─── Инструменты для поиска и анализа логов ──────────────────────────────
def search_logs(query: str, level: Optional[str] = None, module: Optional[str] = None,
                limit: int = 100, offset: int = 0) -> Dict:
    """
    Поиск по лог-файлам (поддерживает регулярные выражения).
    Возвращает строки с логами, удовлетворяющие фильтрам.
    """
    log_files = [main_log_path]
    if level and LEVELS.get(level.upper(), 0) >= logging.ERROR:
        log_files.append(error_log_path)
    else:
        log_files = list(Path(LOG_DIR).glob("mcp.log*"))  # включая ротированные
        log_files.sort(reverse=True)  # свежие первыми

    results = []
    try:
        regex = re.compile(query, re.IGNORECASE)
    except re.error:
        return {"error": f"Invalid regex: {query}"}

    # Фильтр по уровню (если указан)
    level_filter = LEVELS.get(level.upper()) if level else None
    module_lower = module.lower() if module else None

    for log_file in log_files:
        if not log_file.exists():
            continue
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n')
                # Парсим уровень и модуль из формата: "2025-05-25 10:00:00 | INFO     | MCP.web_reader | message"
                parts = line.split('|')
                if len(parts) >= 3:
                    try:
                        line_level = parts[1].strip()
                        line_module = parts[2].strip()
                    except Exception:
                        line_level = ""
                        line_module = ""
                else:
                    line_level = ""
                    line_module = ""

                # Фильтр по уровню
                if level_filter:
                    line_level_int = LEVELS.get(line_level, 0)
                    if line_level_int < level_filter:
                        continue
                # Фильтр по модулю
                if module_lower and module_lower not in line_module.lower():
                    continue
                # Поиск по regex
                if regex.search(line):
                    results.append(line)
                    if len(results) >= offset + limit:
                        break
        if len(results) >= offset + limit:
            break

    total = len(results)
    paginated = results[offset:offset+limit]
    return {
        "query": query,
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": paginated,
        "has_more": offset + limit < total
    }

def get_log_stats() -> Dict:
    """Статистика по лог-файлам."""
    stats = {}
    total_size = 0
    for f in Path(LOG_DIR).glob("mcp.log*"):
        size = f.stat().st_size
        total_size += size
        stats[f.name] = {"size_bytes": size, "size_mb": round(size / (1024*1024), 2)}
    return {
        "log_dir": LOG_DIR,
        "files": stats,
        "total_size_mb": round(total_size / (1024*1024), 2),
        "udp_port": UDP_PORT,
        "http_port": HTTP_PORT,
        "current_log_level": LOG_LEVEL
    }

def rotate_logs() -> Dict:
    """Принудительная ротация логов."""
    main_handler.doRollover()
    error_handler.doRollover()
    return {"status": "rotated"}

# ─── Интеграция с mcp_shared: переопределение _log для отправки на сервер ──
# В mcp_shared.py нужно будет изменить функцию _log, чтобы она отправляла UDP.
# Для совместимости здесь не трогаем mcp_shared, а предоставляем отдельную функцию.
def send_log(level: str, message: str, module: str = "unknown", dialog: str = None):
    """Отправить лог на центральный сервер (UDP)."""
    if not dialog:
        dialog = dialog_ctx.get("default")
    payload = {
        "level": level,
        "module": module,
        "message": message,
        "dialog": dialog,
        "timestamp": datetime.now().isoformat()
    }
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(json.dumps(payload).encode('utf-8'), ('127.0.0.1', UDP_PORT))
        sock.close()
    except Exception:
        pass  # не ломаем основное приложение

# ─── Регистрация инструментов MCP ────────────────────────────────────────
def register_tools(server: BaseMCPServer):
    server.register_tool("log_server_start", {
        "description": "Запустить централизованный сервер логирования (UDP+HTTP)",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: start_log_server())

    server.register_tool("log_server_stop", {
        "description": "Остановить сервер логирования",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: stop_log_server())

    server.register_tool("log_search", {
        "description": "Поиск по лог-файлам с поддержкой regex и фильтров",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Регулярное выражение или текст"},
                "level": {"type": "string", "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]},
                "module": {"type": "string", "description": "Имя модуля (часть имени)"},
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0}
            },
            "required": ["query"]
        }
    }, lambda **kw: search_logs(
        kw["query"], kw.get("level"), kw.get("module"),
        kw.get("limit", 100), kw.get("offset", 0)
    ))

    server.register_tool("log_stats", {
        "description": "Статистика лог-файлов",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: get_log_stats())

    server.register_tool("log_rotate", {
        "description": "Принудительная ротация логов",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: rotate_logs())

    server.register_tool("log_send", {
        "description": "Отправить лог на сервер (для тестирования)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["DEBUG", "INFO", "WARNING", "ERROR"]},
                "message": {"type": "string"},
                "module": {"type": "string", "default": "manual"}
            },
            "required": ["level", "message"]
        }
    }, lambda **kw: send_log(kw["level"], kw["message"], kw.get("module", "manual")))

__mcp_plugin__ = {
    "name": "log-server",
    "version": "1.0",
    "description": "Централизованное логирование с ротацией и поиском",
    "dependencies": [],
    "on_load": lambda: _log("[LogServer] Plugin loaded. Use log_server_start() to start UDP/HTTP receivers."),
    "on_unload": lambda: stop_log_server()
}

if __name__ == "__main__":
    # Тестовый запуск
    server = BaseMCPServer("log-server", "1.0")
    register_tools(server)
    server.run()