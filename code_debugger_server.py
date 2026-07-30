#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Code Debugger v3.2 (Context-Isolated & Cross-Platform Sandbox)
Syntax checking and isolated code execution with strict environment stripping,
Unix resource limits, AST-based safety checks, and persistent hypothesis memory.
Uses contextvars for secure dialog isolation.
"""
import os
import sys
import json
import subprocess
import tempfile
import ast
import time
import uuid
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

# -----------------------------------------------------------------------------
# Dependency check on mcp_shared
# -----------------------------------------------------------------------------
try:
    from mcp_shared import (
        _log, BaseMCPServer, conversation_memory, dialog_ctx
    )
except ImportError as e:
    print(f"FATAL: Missing required module 'mcp_shared': {e}", file=sys.stderr)
    sys.exit(1)

# -----------------------------------------------------------------------------
# Configuration constants
# -----------------------------------------------------------------------------
MAX_CODE_SIZE = 1024 * 100          # 100 KB
MAX_STDOUT_CHARS = 10000            # Increased from 2000
MAX_STDERR_CHARS = 5000             # Increased from 1000
DEFAULT_TIMEOUT = 5
MIN_TIMEOUT = 1
MAX_TIMEOUT = 30

# -----------------------------------------------------------------------------
# Cross-Platform Resource Limits
# -----------------------------------------------------------------------------
HAS_RESOURCE = False
if sys.platform != 'win32':
    try:
        import resource
        HAS_RESOURCE = True
    except ImportError:
        pass

def _set_limits():
    """Apply strict resource limits (Unix only)."""
    if not HAS_RESOURCE:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (20, 20))
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Security & Safety
# -----------------------------------------------------------------------------
_BANNED_MODULES = {
    'os', 'sys', 'subprocess', 'socket', 'urllib', 'http', 'requests',
    'ftplib', 'smtplib', 'telnetlib', 'poplib', 'imaplib', 'shutil',
    'pathlib', 'tempfile', 'multiprocessing', 'threading', 'ctypes',
    'mmap', 'pickle', 'marshal', 'importlib', 'pkgutil', 'io'
}

def _check_code_safety(code: str) -> Tuple[bool, str]:
    """Static AST analysis to block dangerous imports, built-in overrides, and __import__ calls."""
    if len(code) > MAX_CODE_SIZE:
        return False, f"Code size exceeds limit of {MAX_CODE_SIZE} bytes"

    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            # Block imports of banned modules
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] in _BANNED_MODULES:
                        return False, f"Restricted import: '{alias.name}'"
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] in _BANNED_MODULES:
                    return False, f"Restricted import from: '{node.module}'"
            # Block assignment to dangerous built-ins
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ('__builtins__', 'open', 'eval', 'exec'):
                        return False, f"Attempt to override restricted built-in: '{target.id}'"
            # Block direct call to __import__ function
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == '__import__':
                    return False, "Direct call to __import__() is forbidden"
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"
    except Exception as e:
        return False, f"AST analysis failed: {e}"

def _strip_environment() -> dict:
    """
    Return a whitelisted environment to prevent secret/env leaks.
    Can be extended via MCP_ALLOWED_ENV_VARS environment variable (comma-separated).
    """
    allowed = {'PATH', 'PYTHONPATH', 'HOME', 'USERPROFILE', 'TEMP', 'TMP', 'SYSTEMROOT', 'COMSPEC'}
    extra = os.environ.get('MCP_ALLOWED_ENV_VARS', '')
    if extra:
        allowed.update(extra.split(','))
    return {k: v for k, v in os.environ.items() if k in allowed}

def _get_dialog_id(dialog_id: Optional[str]) -> str:
    """Return a valid dialog id, generating a default if None."""
    if dialog_id:
        return dialog_id
    ctx_id = dialog_ctx.get()
    if ctx_id:
        return ctx_id
    return f"auto_{uuid.uuid4().hex[:8]}"

# -----------------------------------------------------------------------------
# Core Operations
# -----------------------------------------------------------------------------
def syntax_check(code: str, language: str = "python", dialog_id: str = None) -> Dict:
    d_id = _get_dialog_id(dialog_id)
    if language.lower() != "python":
        return {"valid": None, "error": f"Language '{language}' not supported for syntax check", "language": language}

    safe, reason = _check_code_safety(code)
    if not safe:
        conversation_memory.add(
            op="syntax_check", paths={"lang": language}, status="blocked",
            dialog=d_id, context=f"Security block: {reason}"
        )
        return {"valid": False, "error": reason, "language": language}

    try:
        compile(code, '<string>', 'exec')
        conversation_memory.add(
            op="syntax_check", paths={"lang": language}, status="valid", dialog=d_id,
            context=f"Syntax check passed for {language}"
        )
        return {"valid": True, "error": None, "language": language}
    except SyntaxError as e:
        return {
            "valid": False, "error": f"Line {e.lineno}, Col {e.offset}: {e.msg}",
            "line": e.lineno, "column": e.offset, "text": e.text, "language": language
        }
    except Exception as e:
        return {"valid": False, "error": f"{type(e).__name__}: {e}", "language": language}

def test_hypothesis(code: str, timeout: int = DEFAULT_TIMEOUT, dialog_id: str = None) -> Dict:
    # Normalize and clamp timeout
    timeout = min(max(timeout, MIN_TIMEOUT), MAX_TIMEOUT)
    d_id = _get_dialog_id(dialog_id)

    # 1. Safety & size check
    safe, reason = _check_code_safety(code)
    if not safe:
        conversation_memory.add(
            op="test_hypothesis", status="blocked", dialog=d_id,
            context=f"Security violation: {reason}"
        )
        return {"error": f"Security violation: {reason}", "blocked": True, "dialog": d_id}

    # 2. Syntax pre-check
    syntax = syntax_check(code, dialog_id=d_id)
    if not syntax["valid"]:
        return {"error": f"Syntax error: {syntax['error']}", "blocked": False, "dialog": d_id}

    tmpname = None
    start = time.time()
    try:
        # Use NamedTemporaryFile with automatic deletion (safer)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', prefix='mcp_exec_', delete=False, encoding='utf-8') as f:
            tmpname = f.name
            f.write("# Safe MCP Sandbox Environment\nimport sys; sys.path = ['.']\n" + code)
            os.chmod(tmpname, 0o600)

        # Prepare execution command
        cmd = [sys.executable, '-u', '-B', tmpname]
        env = _strip_environment()

        # Platform-specific sandboxing
        kwargs = {
            "capture_output": True, "text": True, "timeout": timeout,
            "env": env, "cwd": os.path.dirname(tmpname)
        }
        if sys.platform != 'win32':
            kwargs["preexec_fn"] = _set_limits  # Unix only
        else:
            # Windows: avoid window, breakaway from job
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB

        proc = subprocess.run(cmd, **kwargs)
        elapsed = time.time() - start

        # Truncate output according to config limits
        stdout = proc.stdout[:MAX_STDOUT_CHARS] if proc.stdout else ""
        stderr = proc.stderr[:MAX_STDERR_CHARS] if proc.stderr else ""
        truncated = (proc.stdout and len(proc.stdout) > MAX_STDOUT_CHARS) or (proc.stderr and len(proc.stderr) > MAX_STDERR_CHARS)

        conversation_memory.add(
            op="test_hypothesis", paths={"temp_file": tmpname},
            status="executed" if proc.returncode == 0 else f"exit_{proc.returncode}",
            dialog=d_id, context=f"Executed in {elapsed:.2f}s, exit={proc.returncode}, out={len(stdout)} chars"
        )
        return {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "elapsed_sec": round(elapsed, 2),
            "truncated": truncated,
            "blocked": False,
            "dialog": d_id
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Execution timed out after {timeout}s", "timeout": timeout, "blocked": False, "dialog": d_id}
    except Exception as e:
        return {"error": str(e), "exception": type(e).__name__, "blocked": False, "dialog": d_id}
    finally:
        if tmpname and os.path.exists(tmpname):
            try:
                os.unlink(tmpname)
            except Exception:
                pass  # Best-effort cleanup

# -----------------------------------------------------------------------------
# Server Setup
# -----------------------------------------------------------------------------
server = BaseMCPServer("code-debugger", "3.2")
server.register_tool("syntax_check", {
    "description": "Check Python code syntax and block restricted imports",
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "maxLength": MAX_CODE_SIZE},
            "language": {"type": "string", "default": "python"},
            "dialog_id": {"type": "string"}
        },
        "required": ["code"]
    }
}, lambda **kw: syntax_check(kw["code"], kw.get("language", "python"), kw.get("dialog_id")))

server.register_tool("test_hypothesis", {
    "description": "Run Python code in isolated sandbox with strict env stripping and timeout (1-30 sec)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "maxLength": MAX_CODE_SIZE},
            "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT, "minimum": MIN_TIMEOUT, "maximum": MAX_TIMEOUT},
            "dialog_id": {"type": "string"}
        },
        "required": ["code"]
    }
}, lambda **kw: test_hypothesis(kw["code"], kw.get("timeout", DEFAULT_TIMEOUT), kw.get("dialog_id")))

if __name__ == "__main__":
    _log("Starting Code Debugger Server v3.2")
    server.run()