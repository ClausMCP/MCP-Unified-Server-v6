#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Watchdog v1.1 – Мониторинг и авто-восстановление серверов MCP
Исправления: добавлен graceful shutdown, исправлена работа с PID-файлом.
"""
import os
import sys
import json
import time
import subprocess
import threading
import urllib.request
import atexit
import signal
from pathlib import Path
from typing import Dict, List, Optional
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Конфигурация ─────────────────────────────────────────────────────────
CHECK_INTERVAL_SEC = int(os.environ.get("MCP_WATCHDOG_INTERVAL", "30"))
RESTART_DELAY_SEC = int(os.environ.get("MCP_WATCHDOG_RESTART_DELAY", "5"))
MAX_RESTART_ATTEMPTS = int(os.environ.get("MCP_WATCHDOG_MAX_ATTEMPTS", "3"))
RESTART_COOLDOWN_SEC = int(os.environ.get("MCP_WATCHDOG_COOLDOWN", "3600"))

SETUP_BAT = Path(__file__).parent / "setup.bat"
MCP_SERVER_SCRIPT = Path(__file__).parent / "mcp_fs_server.py"
VENV_PYTHON = Path(__file__).parent / ".venv" / "Scripts" / "python.exe"

def _send_alert(message: str):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    full_msg = f"[MCP Watchdog] {timestamp}: {message}"
    _log(full_msg)
    conversation_memory.add(
        op="watchdog_alert",
        paths={"component": "watchdog"},
        status="alert",
        dialog="watchdog",
        context=full_msg
    )
    try:
        from mcp_admin_server import send_alert
        send_alert(full_msg)
    except ImportError:
        telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        if telegram_token and telegram_chat:
            try:
                url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
                data = json.dumps({"chat_id": telegram_chat, "text": full_msg}).encode()
                req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                _log(f"Watchdog Telegram alert failed: {e}")
        if slack_webhook:
            try:
                data = json.dumps({"text": full_msg}).encode()
                req = urllib.request.Request(slack_webhook, data=data, headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                _log(f"Watchdog Slack alert failed: {e}")

def _call_get_system_health(timeout: int = 10) -> Optional[Dict]:
    try:
        from mcp_admin_server import get_system_health
        return get_system_health()
    except ImportError:
        _log("[Watchdog] Admin server not available, cannot check health directly.")
        return None
    except Exception as e:
        _log(f"[Watchdog] Health check error: {e}")
        return None

def _restart_server() -> bool:
    _log("[Watchdog] Attempting to restart MCP server...")
    pid_file = Path(__file__).parent / ".venv" / "server.pid"
    if pid_file.exists():
        try:
            with open(pid_file, 'r') as f:
                pid = int(f.read().strip())
            # Проверяем, жив ли процесс
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)
                _log(f"[Watchdog] Killed process {pid}")
            except ProcessLookupError:
                _log(f"[Watchdog] Process {pid} already dead")
            # Удаляем PID-файл
            pid_file.unlink(missing_ok=True)
            time.sleep(2)
        except Exception as e:
            _log(f"[Watchdog] Could not kill process {pid}: {e}")
            
    if VENV_PYTHON.exists() and MCP_SERVER_SCRIPT.exists():
        cmd = [str(VENV_PYTHON), str(MCP_SERVER_SCRIPT)]
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW)
        with open(pid_file, 'w') as f:
            f.write(str(proc.pid))
        _log(f"[Watchdog] Started new server process with PID {proc.pid}")
        return True
    else:
        _log("[Watchdog] Python executable or server script not found")
        return False

class Watchdog:
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None
        self._failure_counts = {}
        self._last_restart_time = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp_watchdog")
        self._thread.start()
        _log("[Watchdog] Started monitoring thread")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _log("[Watchdog] Stopped monitoring thread")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                health = self._check_health()
                if health:
                    status = health.get("status", "unknown")
                    missing = health.get("missing_components", [])
                    if status == "degraded" and missing:
                        _send_alert(f"Server degraded, missing components: {missing}")
                        self._handle_failure("mcp_server", health)
                    elif status == "healthy":
                        self._failure_counts.clear()
                    else:
                        _send_alert("Health check failed — server may be down")
                        self._handle_failure("mcp_server", None)
            except Exception as e:
                _log(f"[Watchdog] Loop error: {e}")
            time.sleep(CHECK_INTERVAL_SEC)

    def _check_health(self) -> Optional[Dict]:
        return _call_get_system_health()

    def _handle_failure(self, server_name: str, health: Optional[Dict]):
        now = time.time()
        count, first_fail = self._failure_counts.get(server_name, (0, now))
        if now - first_fail > RESTART_COOLDOWN_SEC:
            count = 0
            first_fail = now
        count += 1
        self._failure_counts[server_name] = (count, first_fail)
        if count <= MAX_RESTART_ATTEMPTS:
            _log(f"[Watchdog] Failure #{count} for {server_name}, restarting...")
            _send_alert(f"Restarting {server_name} (attempt {count}/{MAX_RESTART_ATTEMPTS})")
            time.sleep(RESTART_DELAY_SEC)
            success = _restart_server()
            if success:
                _log(f"[Watchdog] Restart initiated for {server_name}")
                time.sleep(5)
            else:
                _log(f"[Watchdog] Failed to restart {server_name}")
        else:
            _send_alert(f"Maximum restart attempts reached for {server_name}. Manual intervention required.")
            self._failure_counts[server_name] = (MAX_RESTART_ATTEMPTS + 1, first_fail)

_watchdog = Watchdog()

def watchdog_start() -> Dict:
    _watchdog.start()
    return {"status": "started", "interval_sec": CHECK_INTERVAL_SEC}

def watchdog_stop() -> Dict:
    _watchdog.stop()
    return {"status": "stopped"}

def watchdog_status() -> Dict:
    return {
        "running": _watchdog._thread is not None and _watchdog._thread.is_alive(),
        "interval_sec": CHECK_INTERVAL_SEC,
        "max_attempts": MAX_RESTART_ATTEMPTS,
        "cooldown_sec": RESTART_COOLDOWN_SEC,
        "failure_counts": _watchdog._failure_counts
    }

def watchdog_restart_now() -> Dict:
    success = _restart_server()
    return {"status": "restarted" if success else "failed"}

# ─── Graceful Shutdown ───────────────────────────────────────────────────
def _shutdown_watchdog():
    watchdog_stop()

atexit.register(_shutdown_watchdog)
signal.signal(signal.SIGINT, lambda s, f: _shutdown_watchdog())
signal.signal(signal.SIGTERM, lambda s, f: _shutdown_watchdog())

def register_tools(server: BaseMCPServer):
    server.register_tool("watchdog_start", {
        "description": "Запустить фоновый мониторинг и авто-восстановление серверов",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: watchdog_start())
    
    server.register_tool("watchdog_stop", {
        "description": "Остановить watchdog",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: watchdog_stop())
    
    server.register_tool("watchdog_status", {
        "description": "Статус watchdog",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: watchdog_status())
    
    server.register_tool("watchdog_restart_now", {
        "description": "Принудительно перезапустить MCP сервер",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: watchdog_restart_now())

__mcp_plugin__ = {
    "name": "watchdog",
    "version": "1.1",
    "description": "Мониторинг и авто-восстановление MCP серверов",
    "dependencies": [],
    "on_load": lambda: _log("[Watchdog] Plugin loaded."),
    "on_unload": lambda: watchdog_stop()
}

if __name__ == "__main__":
    server = BaseMCPServer("watchdog", "1.1")
    register_tools(server)
    server.run()