#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Code Exec v1.0 — выполнение Python-кода (офлайн).

Запускает Python-код в ОТДЕЛЬНОМ подпроцессе того же venv (со всеми
установленными пакетами: numpy, pandas, math, statistics и т.д.), с таймаутом
и захватом stdout/stderr. Подходит для вычислений, формул, обработки данных и
скриптов. Интернет не требуется.

Инструменты: run_python, run_python_file, calc, list_packages.

ВНИМАНИЕ: выполняет реальный код на машине (как и run_shell). Рабочая папка по
умолчанию — MCP_CODE_EXEC_DIR или <data>/workspace.
"""
import os
import sys
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Any

from mcp_shared import BaseMCPServer, _log, conversation_memory, normalize_path

_MAX_OUTPUT = 50000          # ограничение размера вывода
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 300


def _python_exe() -> str:
    """Интерпретатор для запуска (тот же venv, что и у сервера)."""
    return os.environ.get("MCP_PYTHON_EXE") or sys.executable


def _workspace() -> str:
    d = os.environ.get("MCP_CODE_EXEC_DIR")
    if not d:
        base = os.path.dirname(os.path.abspath(getattr(conversation_memory, "db_path", "."))) or "."
        d = os.path.join(base, "workspace")
    os.makedirs(d, exist_ok=True)
    return d


def _clip(s: str) -> str:
    if s and len(s) > _MAX_OUTPUT:
        return s[:_MAX_OUTPUT] + f"\n...[обрезано, всего {len(s)} символов]"
    return s or ""


def _run(args: List[str], cwd: str, timeout: int, stdin: str = "") -> Dict[str, Any]:
    timeout = max(1, min(int(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
    # Принудительный UTF-8: на Windows подпроцесс иначе использует cp1251 и
    # кириллица в исходнике/выводе превращается в мусор. PYTHONUTF8/IOENCODING
    # + encoding='utf-8' гарантируют корректную кириллицу.
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            input=stdin if stdin else None,
            encoding="utf-8", errors="replace", env=env,
        )
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "stdout": _clip(proc.stdout),
            "stderr": _clip(proc.stderr),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        def _dec(v):
            if isinstance(v, bytes):
                return v.decode("utf-8", "replace")
            return v or ""
        return {
            "status": "timeout",
            "returncode": None,
            "stdout": _clip(_dec(e.stdout)),
            "stderr": _clip(_dec(e.stderr)),
            "timed_out": True,
            "timeout_sec": timeout,
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def run_python(code: str, timeout: int = _DEFAULT_TIMEOUT, stdin: str = "",
               workdir: Optional[str] = None) -> Dict[str, Any]:
    """Выполнить переданный Python-код и вернуть stdout/stderr/код возврата."""
    if not code or not code.strip():
        return {"status": "error", "error": "пустой код"}
    cwd = normalize_path(workdir) if workdir else _workspace()
    os.makedirs(cwd, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=cwd, encoding="utf-8")
    try:
        tmp.write(code)
        tmp.close()
        result = _run([_python_exe(), tmp.name], cwd=cwd, timeout=timeout, stdin=stdin)
    finally:
        try:
            os.remove(tmp.name)
        except Exception:
            pass
    result["workdir"] = cwd
    return result


def run_python_file(path: str, args: Optional[List[str]] = None,
                    timeout: int = _DEFAULT_TIMEOUT, stdin: str = "") -> Dict[str, Any]:
    """Запустить существующий .py файл с аргументами."""
    p = Path(normalize_path(path))
    if not p.exists():
        return {"status": "error", "error": f"файл не найден: {p}"}
    cmd = [_python_exe(), str(p)] + [str(a) for a in (args or [])]
    res = _run(cmd, cwd=str(p.parent), timeout=timeout, stdin=stdin)
    res["file"] = str(p)
    return res


def calc(expression: str) -> Dict[str, Any]:
    """Безопасно вычислить математическое выражение (модуль math доступен)."""
    import math
    allowed = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    allowed.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum,
                    "len": len, "pow": pow})
    try:
        # запрет доступа к builtins (никаких импортов/функций кроме math)
        value = eval(expression, {"__builtins__": {}}, allowed)  # noqa: S307 (выражение, sandbox)
        return {"status": "success", "expression": expression, "result": value}
    except Exception as e:
        return {"status": "error", "expression": expression, "error": f"{type(e).__name__}: {e}"}


def list_packages() -> Dict[str, Any]:
    """Список установленных пакетов (что доступно офлайн для кода)."""
    res = _run([_python_exe(), "-m", "pip", "list", "--format=freeze"], cwd=_workspace(), timeout=30)
    pkgs = [line for line in (res.get("stdout") or "").splitlines() if line]
    return {"status": "success", "count": len(pkgs), "packages": pkgs}


def register_tools(server: BaseMCPServer):
    server.register_tool("run_python", {
        "description": "Execute Python code offline in an isolated subprocess (same venv: numpy, pandas, "
                       "math, etc.). Returns stdout/stderr/returncode. For computation, formulas, data "
                       "processing, scripting. Runs REAL code on the machine.",
        "inputSchema": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python source to execute"},
            "timeout": {"type": "integer", "description": f"Seconds (default {_DEFAULT_TIMEOUT}, max {_MAX_TIMEOUT})"},
            "stdin": {"type": "string", "description": "Optional stdin"},
            "workdir": {"type": "string", "description": "Working directory (default workspace)"}
        }, "required": ["code"]}
    }, lambda **kw: run_python(kw["code"], kw.get("timeout", _DEFAULT_TIMEOUT), kw.get("stdin", ""), kw.get("workdir")))

    server.register_tool("run_python_file", {
        "description": "Run an existing .py file with arguments (offline).",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "timeout": {"type": "integer"},
            "stdin": {"type": "string"}
        }, "required": ["path"]}
    }, lambda **kw: run_python_file(kw["path"], kw.get("args"), kw.get("timeout", _DEFAULT_TIMEOUT), kw.get("stdin", "")))

    server.register_tool("calc", {
        "description": "Safely evaluate a math expression (math module functions available, no imports). "
                       "Example: calc('sqrt(2) * pi').",
        "inputSchema": {"type": "object", "properties": {
            "expression": {"type": "string"}
        }, "required": ["expression"]}
    }, lambda **kw: calc(kw["expression"]))

    server.register_tool("list_packages", {
        "description": "List installed Python packages available offline for run_python.",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: list_packages())


__mcp_plugin__ = {
    "name": "code-exec",
    "version": "1.0.0",
    "description": "Offline Python execution: run_python, run_python_file, calc, list_packages",
    "dependencies": [],
    "on_load": lambda: _log("[code-exec] v1.0 loaded — tools: run_python, run_python_file, calc, list_packages"),
}

if __name__ == "__main__":
    print(json.dumps(run_python("print(2**10)"), indent=2, ensure_ascii=False))
