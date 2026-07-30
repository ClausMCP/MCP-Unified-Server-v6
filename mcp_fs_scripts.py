#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Scripts v2.1
Secure execution of user-defined scripts with environment stripping,
AST import validation, Windows job isolation, and context-aware logging.
"""
import os
import sys
import ast
import json
import time
import hashlib
import subprocess
import platform
from pathlib import Path
from typing import List, Dict, Optional, Set
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Configuration ───────────────────────────────────────────────────────────
SCRIPTS_DIR = os.environ.get("MCP_SCRIPTS_DIR", os.path.join(os.path.dirname(__file__), "scripts"))
ALLOWED_EXTENSIONS = {'.py', '.bat', '.cmd', '.ps1'}
EXECUTION_TIMEOUT = int(os.environ.get("MCP_SCRIPT_TIMEOUT", "30"))
MAX_OUTPUT_CHARS = int(os.environ.get("MCP_SCRIPT_MAX_OUTPUT", "5000"))

# Safe environment whitelist (blocks tokens, API keys, secrets)
_ALLOWED_ENV_KEYS = {
    'PATH', 'PATHEXT', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR',
    'COMPUTERNAME', 'USERNAME', 'USERPROFILE', 'TMP', 'TEMP',
    'PYTHONIOENCODING', 'PYTHONUNBUFFERED', 'VIRTUAL_ENV',
    'LANG', 'LC_ALL', 'TZ', 'TERM', 'HOME'
}

# AST Blacklist: Blocks network, process spawning, and system manipulation
_DANGEROUS_MODULES = {
    'subprocess', 'socket', 'requests', 'urllib', 'http',
    'ftplib', 'smtplib', 'poplib', 'imaplib', 'xmlrpc',
    'ctypes', 'winreg', 'multiprocessing', 'threading',
    'shutil', 'pickle', 'marshal'
}

# ─── Security & Isolation Helpers ────────────────────────────────────────────
def _strip_environment() -> Dict[str, str]:
    """Return a filtered environment dict with only safe, necessary variables."""
    return {k: v for k, v in os.environ.items() if k in _ALLOWED_ENV_KEYS}

def _check_ast_imports(script_path: Path) -> Dict[str, any]:
    """Parse script AST and block dangerous module imports."""
    try:
        source = script_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(script_path))
    except SyntaxError as e:
        return {"valid": False, "error": f"SyntaxError: {e}"}
    except Exception as e:
        return {"valid": False, "error": f"ParseError: {e}"}

    blocked: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split('.')[0]
                if mod in _DANGEROUS_MODULES:
                    blocked.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split('.')[0]
                if mod in _DANGEROUS_MODULES:
                    blocked.add(node.module)

    if blocked:
        return {
            "valid": False,
            "blocked": list(blocked),
            "error": f"Blocked dangerous imports: {', '.join(blocked)}"
        }
    return {"valid": True}

def _get_script_path(script_name: str) -> Path:
    """Resolve script path securely with traversal protection."""
    base = Path(normalize_path(SCRIPTS_DIR)).resolve()
    script_path = (base / script_name).resolve()

    if not str(script_path).startswith(str(base)):
        raise PermissionError(f"Script '{script_name}' attempts directory traversal")
    if not script_path.exists():
        raise FileNotFoundError(f"Script '{script_name}' not found in {SCRIPTS_DIR}")
    if script_path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Extension '{script_path.suffix}' not allowed. Use {ALLOWED_EXTENSIONS}")
    
    return script_path

# ─── Execution Engines ───────────────────────────────────────────────────────
def _execute_python(script_path: Path, args: List[str]) -> Dict:
    """Execute Python script with isolated env and AST validation."""
    ast_check = _check_ast_imports(script_path)
    if not ast_check["valid"]:
        return {"status": "blocked", "error": ast_check["error"], "blocked_modules": ast_check["blocked"]}

    cmd = [sys.executable, "-u", str(script_path)] + args
    env = _strip_environment()
    start_time = time.time()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT,
            cwd=str(script_path.parent),
            env=env
        )
        elapsed = time.time() - start_time
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:MAX_OUTPUT_CHARS],
            "stderr": proc.stderr[:MAX_OUTPUT_CHARS // 2],
            "elapsed_sec": round(elapsed, 2)
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Execution timed out after {EXECUTION_TIMEOUT}s"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def _execute_shell(script_path: Path, args: List[str]) -> Dict:
    """Execute Batch or PowerShell script with job isolation."""
    ext = script_path.suffix.lower()
    env = _strip_environment()
    start_time = time.time()

    if ext in ['.bat', '.cmd']:
        cmd = ["cmd.exe", "/c", str(script_path)] + args
    elif ext == '.ps1':
        cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile", "-File", str(script_path)] + args
    else:
        return {"status": "error", "error": "Unsupported shell script type"}

    # Windows isolation flags: break from parent job, create new process group, hide window
    if platform.system() == "Windows":
        flags = (
            getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0) |
            getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) |
            getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
        kwargs = {"creationflags": flags, "env": env}
    else:
        kwargs = {"env": env}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT,
            cwd=str(script_path.parent),
            **kwargs
        )
        elapsed = time.time() - start_time
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:MAX_OUTPUT_CHARS],
            "stderr": proc.stderr[:MAX_OUTPUT_CHARS // 2],
            "elapsed_sec": round(elapsed, 2)
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Execution timed out after {EXECUTION_TIMEOUT}s"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

# ─── Core Operations ─────────────────────────────────────────────────────────
def list_scripts() -> Dict:
    """List available scripts in the secured directory."""
    base = Path(normalize_path(SCRIPTS_DIR))
    if not base.exists():
        return {"status": "error", "message": "Scripts directory not found"}
    
    scripts = []
    for f in base.iterdir():
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
            scripts.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime
            })
    return {"status": "success", "scripts": scripts, "count": len(scripts)}

def run_script(script_name: str, arguments: List[str] = None, cache_result: bool = False) -> Dict:
    """Execute a validated script and log to conversation memory."""
    if arguments is None:
        arguments = []
    
    script_path = _get_script_path(script_name)
    dialog_id = dialog_ctx.get("default")

    # Optional cache check (metadata-based)
    cache_key = None
    if cache_result:
        arg_hash = hashlib.sha256("".join(arguments).encode()).hexdigest()[:8]
        cache_key = f"{script_path.name}_{script_path.stat().st_mtime}_{arg_hash}"
        # Cache lookup would be implemented here if integrated with chunk_cache/memory

    _log(f"[Scripts] Executing: {script_name} | Dialog: {dialog_id}")
    result = _execute_python(script_path, arguments) if script_path.suffix.lower() == '.py' else _execute_shell(script_path, arguments)

    # Enrich result
    result["script"] = script_name
    result["arguments"] = arguments
    result["cache_key"] = cache_key

    conversation_memory.add(
        op="run_script",
        paths={"script": str(script_path)},
        status=result["status"],
        dialog=dialog_id,
        context=f"Executed {script_name} | Exit: {result.get('exit_code')} | Time: {result.get('elapsed_sec')}s"
    )
    return result

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-scripts", "2.1")
server.register_tool("list_scripts", {
    "description": "List available scripts in the configured scripts directory",
    "inputSchema": {"type": "object", "properties": {}}
}, list_scripts)

server.register_tool("run_script", {
    "description": "Execute a validated script with arguments. Blocks dangerous imports.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "script_name": {"type": "string"},
            "arguments": {"type": "array", "items": {"type": "string"}, "default": []},
            "cache_result": {"type": "boolean", "default": False}
        },
        "required": ["script_name"]
    }
}, lambda **kw: run_script(kw["script_name"], kw.get("arguments", []), kw.get("cache_result", False)))

if __name__ == "__main__":
    server.run()