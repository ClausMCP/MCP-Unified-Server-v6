#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Admin Server v3.1
Centralized monitoring, health checks, Prometheus metrics, and Web UI.
Lazy startup via env flag, dynamic server discovery, context-aware alerts.
"""
import os
import sys
import json
import time
import glob
import threading
import http.server
import socketserver
import urllib.request
from pathlib import Path
from typing import Dict, List
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Configuration ───────────────────────────────────────────────────────────
WEB_PORT = int(os.environ.get("MCP_ADMIN_WEB_PORT", "8080"))
START_WEB_SERVER = os.environ.get("MCP_START_ADMIN_WEB", "0") == "1"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Dynamic server discovery (replaces hardcoded list)
def _discover_servers() -> List[str]:
    base = Path(__file__).parent.resolve()
    pattern = str(base / "mcp_*.py")
    all_files = glob.glob(pattern)
    excluded = {"mcp_shared.py", "mcp_fs_server.py", "mcp_admin_server.py"}
    return [os.path.basename(f) for f in all_files
            if os.path.basename(f) not in excluded and not f.endswith("_test.py")]

SERVERS_TO_MONITOR = _discover_servers()

# ─── Metrics Store (Thread-Safe) ─────────────────────────────────────────────
class MetricsStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters = {}
        self.histograms = {}
        self.start_time = time.time()

    def inc(self, name: str, value: int = 1):
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + value

    def observe(self, name: str, value: float):
        with self._lock:
            if name not in self.histograms:
                self.histograms[name] = []
            self.histograms[name].append(value)
            if len(self.histograms[name]) > 1000:
                self.histograms[name] = self.histograms[name][-1000:]

    def get_prometheus_format(self) -> str:
        with self._lock:
            lines = []
            lines.append(f"# HELP mcp_uptime_seconds System uptime")
            lines.append(f"# TYPE mcp_uptime_seconds counter")
            lines.append(f"mcp_uptime_seconds {time.time() - self.start_time:.2f}")
            for name, value in self.counters.items():
                safe = name.replace(" ", "_").replace(".", "_").lower()
                lines.append(f"# HELP mcp_{safe} Total count")
                lines.append(f"# TYPE mcp_{safe} counter")
                lines.append(f"mcp_{safe} {value}")
            for name, values in self.histograms.items():
                safe = name.replace(" ", "_").replace(".", "_").lower()
                avg = sum(values) / len(values) if values else 0
                lines.append(f"# HELP mcp_{safe}_avg Average value")
                lines.append(f"# TYPE mcp_{safe}_avg gauge")
                lines.append(f"mcp_{safe}_avg {avg:.4f}")
            return "\n".join(lines) + "\n"

metrics = MetricsStore()

# ─── Notification Helpers ────────────────────────────────────────────────────
def send_alert(message: str):
    dialog_id = dialog_ctx.get("admin")
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    full_msg = f"[MCP Alert] {timestamp}: {message}"
    _log(full_msg)
    conversation_memory.add(
        op="system_alert",
        paths={"component": "admin_server"},
        status="alert",
        dialog=dialog_id,
        context=full_msg
    )
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": full_msg}).encode()
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            _log(f"Telegram alert failed: {e}")
    if SLACK_WEBHOOK_URL:
        try:
            data = json.dumps({"text": full_msg}).encode()
            req = urllib.request.Request(SLACK_WEBHOOK_URL, data=data, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            _log(f"Slack alert failed: {e}")

# ─── Helpers ─────────────────────────────────────────────────────────────────
def _get_process_info() -> Dict:
    if not HAS_PSUTIL:
        return {"error": "psutil not installed"}
    try:
        process = psutil.Process(os.getpid())
        mem = process.memory_info()
        return {
            "pid": os.getpid(),
            "cpu_percent": process.cpu_percent(interval=0.1),
            "memory_rss_mb": round(mem.rss / (1024 * 1024), 2),
            "memory_vms_mb": round(mem.vms / (1024 * 1024), 2),
            "threads": process.num_threads(),
            "uptime_sec": round(time.time() - process.create_time(), 2)
        }
    except Exception as e:
        return {"error": str(e)}

def _check_file_status(filepath: str) -> Dict:
    p = Path(filepath)
    if not p.exists():
        return {"status": "missing", "path": filepath}
    st = p.stat()
    return {
        "status": "present", "path": filepath,
        "size_bytes": st.st_size,
        "last_modified": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))
    }

def _get_disk_usage() -> Dict:
    if not HAS_PSUTIL:
        return {"error": "psutil not installed"}
    try:
        root = Path(__file__).anchor if sys.platform == "win32" else "/"
        usage = psutil.disk_usage(root)
        return {
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "percent_used": usage.percent
        }
    except Exception as e:
        return {"error": str(e)}

# ─── Core Operations ─────────────────────────────────────────────────────────
def get_system_health() -> Dict:
    start_time = time.time()
    proc_info = _get_process_info()
    metrics.observe("process_cpu_percent", proc_info.get("cpu_percent", 0))
    metrics.observe("process_memory_mb", proc_info.get("memory_rss_mb", 0))
    disk_info = _get_disk_usage()
    metrics.observe("disk_usage_percent", disk_info.get("percent_used", 0))

    if disk_info.get("percent_used", 0) > 90:
        send_alert(f"Critical Disk Usage: {disk_info['percent_used']}% used")

    server_statuses = {}
    missing_servers = []
    for srv in SERVERS_TO_MONITOR:
        status = _check_file_status(os.path.join(os.path.dirname(__file__), srv))
        server_statuses[srv] = status
        if status["status"] == "missing":
            missing_servers.append(srv)

    if missing_servers:
        send_alert(f"Missing Server Components: {', '.join(missing_servers)}")

    mem_stats = conversation_memory.get_stats()
    elapsed = time.time() - start_time
    result = {
        "status": "healthy",
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "elapsed_ms": round(elapsed * 1000, 2),
        "process": proc_info,
        "disk": disk_info,
        "servers": server_statuses,
        "memory_db": {
            "entries": mem_stats["total"],
            "file_size_kb": round(mem_stats["db_size_bytes"] / 1024, 2)
        }
    }
    if proc_info.get("memory_rss_mb", 0) > 1000:
        result["status"] = "warning"
        result["warning"] = "High memory usage"
    if missing_servers:
        result["status"] = "degraded"
        result["missing_components"] = missing_servers
    return result

def get_active_tasks() -> Dict:
    threads = [{"name": t.name, "daemon": t.daemon, "alive": t.is_alive()} for t in threading.enumerate()]
    return {"active_threads": len(threads), "details": threads}

def clear_cache() -> Dict:
    from mcp_shared import chunk_cache, normalize_path, _ensure_allowed
    normalize_path.cache_clear()
    _ensure_allowed.cache_clear()
    with chunk_cache._lock:
        count = len(chunk_cache._cache)
        chunk_cache._cache.clear()
        chunk_cache._times.clear()
    metrics.inc("cache_clears")
    return {"status": "success", "message": f"Caches cleared. Chunk cache had {count} entries."}

# ─── Web Interface & Metrics Server ──────────────────────────────────────────
class MCPWebHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; version=0.0.4')
            self.end_headers()
            self.wfile.write(metrics.get_prometheus_format().encode())
        elif self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            health = get_system_health()
            self.wfile.write(json.dumps(health, ensure_ascii=False).encode())
        else:
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = """<!DOCTYPE html><html><head><title>MCP Admin Dashboard</title><style>body{font-family:monospace;margin:20px;background:#1e1e1e;color:#d4d4d4}pre{background:#252526;padding:10px;border-radius:5px;overflow:auto}</style></head><body><h1>MCP System Dashboard</h1><div id="health">Loading...</div><script>async function update(){const r=await fetch('/health');document.getElementById('health').innerHTML='<pre>'+JSON.stringify(await r.json(),null,2)+'</pre>'}setInterval(update,5000);update();</script></body></html>"""
            self.wfile.write(html.encode())
        metrics.inc("web_requests")

    def log_message(self, format, *args):
        _log(f"[Web] {args[0]}")

def start_web_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", WEB_PORT), MCPWebHandler) as httpd:
        _log(f"Admin Web Server started on port {WEB_PORT}")
        httpd.serve_forever()

# Lazy startup controlled by environment flag
if START_WEB_SERVER:
    threading.Thread(target=start_web_server, daemon=True, name="mcp_admin_web").start()
else:
    _log("Admin Web Server disabled. Set MCP_START_ADMIN_WEB=1 to enable.")

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("admin-monitor", "3.1")
server.register_tool("get_system_health", {"description": "Get comprehensive health status", "inputSchema": {"type": "object", "properties": {}}}, get_system_health)
server.register_tool("get_active_tasks", {"description": "List active threads and background tasks", "inputSchema": {"type": "object", "properties": {}}}, get_active_tasks)
server.register_tool("clear_cache", {"description": "Clear internal caches to optimize memory usage", "inputSchema": {"type": "object", "properties": {}}}, clear_cache)

if __name__ == "__main__":
    server.run()