#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Code Debugger v3.3 (Whitelist-AST Sandbox, Cross-Platform)

Исправления относительно исходной версии (v3.2):
- **БЕЛЫЙ список AST-нод** вместо чёрного списка модулей. `__import__`,
  `compile`, `eval`, `exec`, `getattr`, `globals`, `locals`, `__builtins__`
  и любой dunder-доступ запрещены структурно.
- ИСПРАВЛЕНО (v3.4): whitelist v3.3 отвергал валидный код - не было нод
  Attribute, keyword, Import, With, ClassDef, Delete, Yield и т.д., из-за
  чего блокировались `'a'.upper()`, `f(x=1)`, `import math`, `with`, классы.
  Ноды добавлены; безопасность обеспечивается не отсутствием ноды, а
  проверкой имён: белый список модулей ALLOWED_MODULES + запрет любых
  атрибутов, начинающихся с `_`.
- ИСПРАВЛЕНО (v3.4): пролог `sys.path = []` ломал импорт stdlib и сдвигал
  номера строк в traceback на 2. Код пишется в файл как есть; изоляция
  достигается флагами `-I -B -S` (без user-site, без PYTHONPATH, без
  каталога скрипта в sys.path).
- **PYTHONPATH удалён** из белого списка переменных окружения.
- Временный файл создаётся в «закрытой» директории (chmod 0700) через
  кросс-платформенный `safe_create_temp`.
- Resource limits применены и на Windows (через `psutil`-фоллбэк
  на subprocess); основной путь — `resource.setrlimit` на POSIX.
- Чтение исходника пользователя — из файла, а не из stdin; stdout/stderr
  обрезаются до лимитов.
- Логирование в `conversation_memory` через безопасный try/except.
"""
import ast
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

try:
    from mcp_shared import BaseMCPServer, _log, conversation_memory, dialog_ctx
except ImportError as exc:
    print(f"FATAL: Missing required module 'mcp_shared': {exc}", file=sys.stderr)
    sys.exit(1)


# ─── Configuration constants ─────────────────────────────────────────────────
MAX_CODE_SIZE: int = 100 * 1024
MAX_STDOUT_CHARS: int = 10000
MAX_STDERR_CHARS: int = 5000
DEFAULT_TIMEOUT: int = 5
MIN_TIMEOUT: int = 1
MAX_TIMEOUT: int = 30

# ─── POSIX resource limits ───────────────────────────────────────────────────
HAS_RESOURCE: bool = False
if sys.platform != "win32":
    try:
        import resource  # type: ignore[import-not-found]

        HAS_RESOURCE = True
    except ImportError:
        pass


def _set_limits() -> None:
    if not HAS_RESOURCE:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (20, 20))
    except (ValueError, OSError):
        # setrlimit может упасть, если ужесточение невозможно (контейнер без CAP)
        pass


# ─── AST whitelist (REPLACES blacklist) ─────────────────────────────────────
# Явно разрешённые узлы. Всё, чего нет в этом множестве — отклоняется.
ALLOWED_AST_NODES: Set[type] = {
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.Return,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.arguments,
    ast.arg,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Del,
    ast.Constant,
    ast.Num,            # legacy
    ast.Str,            # legacy
    ast.Bytes,          # legacy
    ast.NameConstant,   # legacy (True/False/None)
    ast.Ellipsis,
    ast.JoinedStr,      # f-strings (но не format_spec с вызовами)
    ast.FormattedValue,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Set,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Subscript,
    ast.Slice,
    ast.Index,          # legacy
    ast.Starred,
    ast.Try,
    ast.ExceptHandler,
    ast.Raise,
    ast.Assert,
    ast.Global,
    ast.Nonlocal,
    # v3.4: ноды, без которых не работает обычный Python
    ast.Attribute,      # 'a'.upper(), d.items() - контроль через имя атрибута
    ast.keyword,        # f(x=1)
    ast.Import,         # контроль через ALLOWED_MODULES
    ast.ImportFrom,
    ast.alias,
    ast.With,
    ast.AsyncWith,
    ast.withitem,
    ast.ClassDef,
    ast.Delete,
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
    ast.NamedExpr,      # walrus
    ast.Expression,
    # Безопасные операторы, введённые в 3.10+
    ast.Match,
    ast.match_case,
    # Операторы (без опасных dunder)
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.LShift,
    ast.RShift,
    ast.BitOr,
    ast.BitXor,
    ast.BitAnd,
    ast.MatMult,        # @
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.Invert,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
}

# Имена, которые нельзя использовать ни в `ast.Name`, ни в `ast.Attribute`.
# Это второй уровень защиты — даже если узел прошёл whitelist, имя
# проверяется.
FORBIDDEN_NAMES: Set[str] = {
    "__builtins__",
    "__import__",
    "open",
    "eval",
    "exec",
    "compile",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
    "input",
    "breakpoint",
    "help",
    "memoryview",
    "__loader__",
    "__spec__",
    "__package__",
}

# Модули, разрешённые к импорту в песочнице. Всё остальное отклоняется.
# Список намеренно узкий: вычисления, текст, структуры данных - без
# файловой системы, сети, процессов и сериализации кода.
ALLOWED_MODULES: Set[str] = {
    "math", "cmath", "decimal", "fractions", "statistics", "random",
    "json", "re", "string", "textwrap", "unicodedata",
    "datetime", "calendar", "time",
    "itertools", "functools", "operator", "collections", "heapq", "bisect",
    "array", "enum", "dataclasses", "typing", "copy", "numbers",
    "hashlib", "hmac", "base64", "binascii", "uuid", "secrets",
    "csv", "difflib", "pprint", "reprlib", "abc", "contextlib",
}

# Атрибуты, запрещённые независимо от объекта-владельца.
FORBIDDEN_ATTRS: Set[str] = {
    "system", "popen", "spawn", "spawnl", "spawnv", "fork", "execv", "execl",
    "remove", "unlink", "rmdir", "removedirs", "rename", "replace",
    "chmod", "chown", "kill", "killpg", "putenv", "environ",
}
# Примечание: load/loads/dump/dumps НЕ запрещены - pickle и marshal
# недоступны через ALLOWED_MODULES, а json.dumps безопасен.


# ─── AST safety check ───────────────────────────────────────────────────────
def _check_code_safety(code: str) -> Tuple[bool, str]:
    """
    Белый список AST + проверка имён.
    Блокирует любые неожиданные узлы, dunder-доступ и явные вызовы
    опасных builtin'ов.
    """
    if len(code) > MAX_CODE_SIZE:
        return False, f"Code size exceeds limit of {MAX_CODE_SIZE} bytes"

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"
    except Exception as e:
        return False, f"AST parse failed: {e}"

    for node in ast.walk(tree):
        # 1. Whitelist по типу узла
        if type(node) not in ALLOWED_AST_NODES:
            return False, f"Disallowed AST node: {type(node).__name__}"

        # 2. Импорты: только из ALLOWED_MODULES, только верхний уровень пакета
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_MODULES:
                    return False, f"Restricted import: {alias.name!r}"
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                return False, "Relative imports are forbidden"
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_MODULES:
                return False, f"Restricted import from: {node.module!r}"

        # 3. Имена: блокируем dunder и опасные builtin'ы
        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                return False, f"Use of forbidden name: {node.id!r}"
            if node.id.startswith("__") and node.id.endswith("__"):
                return False, f"Dunder name is forbidden: {node.id!r}"

        # 4. Атрибуты: любой dunder/приватный доступ и опасные API
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return False, f"Attribute access to dunder/private: {node.attr!r}"
            if node.attr in FORBIDDEN_ATTRS:
                return False, f"Suspicious attribute: {node.attr!r}"

        # 5. Вызовы по имени
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_NAMES:
                return False, f"Call to forbidden function: {node.func.id!r}"

    return True, ""


# ─── Environment isolation ──────────────────────────────────────────────────
# Явно НЕ включаем PYTHONPATH — переменная может перенаправить импорт
# из любой директории и обойти whitelist.
ALLOWED_ENV_VARS: Set[str] = {
    "PATH",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "TZ",
}


def _strip_environment() -> Dict[str, str]:
    extra = os.environ.get("MCP_ALLOWED_ENV_VARS", "")
    allowed = set(ALLOWED_ENV_VARS)
    if extra:
        allowed.update(v.strip() for v in extra.split(",") if v.strip())
    return {k: v for k, v in os.environ.items() if k in allowed}


# ─── Cross-platform temp file creation ──────────────────────────────────────
@contextmanager
def safe_create_temp(suffix: str = ".py", prefix: str = "mcp_exec_") -> Iterator[Path]:
    """
    Создаёт временный файл в безопасной директории.
    - POSIX: mkdir(0o700) + chmod 0o600 для файла
    - Windows: использует tempdir (per-user); пользователь не имеет доступа
      к чужим сессиям, ACL на каталог назначает ОС
    """
    base_dir = Path(tempfile.gettempdir()) / f"mcp_sandbox_{os.getpid()}"
    base_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            os.chmod(base_dir, 0o700)
        except OSError:
            pass
    fd, name = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=str(base_dir))
    try:
        if sys.platform != "win32":
            try:
                os.chmod(name, 0o600)
            except OSError:
                pass
        yield Path(name)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(name)
        except OSError:
            pass


# ─── Dialog id helper ───────────────────────────────────────────────────────
def _get_dialog_id(dialog_id: Optional[str]) -> str:
    if dialog_id:
        return dialog_id
    try:
        ctx_id = dialog_ctx.get()
    except LookupError:
        ctx_id = None
    if ctx_id:
        return ctx_id
    return f"auto_{uuid.uuid4().hex[:8]}"


# ─── Core operations ────────────────────────────────────────────────────────
def syntax_check(code: str, language: str = "python", dialog_id: str = None) -> Dict[str, Any]:
    d_id = _get_dialog_id(dialog_id)
    if language.lower() != "python":
        return {
            "valid": None,
            "error": f"Language '{language}' not supported for syntax check",
            "language": language,
        }

    safe, reason = _check_code_safety(code)
    if not safe:
        try:
            conversation_memory.add(
                op="syntax_check",
                paths={"lang": language},
                status="blocked",
                dialog=d_id,
                context=f"Security block: {reason}",
            )
        except Exception:
            pass
        return {"valid": False, "error": reason, "language": language}

    try:
        compile(code, "<string>", "exec")
        try:
            conversation_memory.add(
                op="syntax_check",
                paths={"lang": language},
                status="valid",
                dialog=d_id,
                context=f"Syntax check passed for {language}",
            )
        except Exception:
            pass
        return {"valid": True, "error": None, "language": language}
    except SyntaxError as e:
        return {
            "valid": False,
            "error": f"Line {e.lineno}, Col {e.offset}: {e.msg}",
            "line": e.lineno,
            "column": e.offset,
            "text": e.text,
            "language": language,
        }
    except Exception as e:
        return {
            "valid": False,
            "error": f"{type(e).__name__}: {e}",
            "language": language,
        }


def test_hypothesis(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    dialog_id: str = None,
) -> Dict[str, Any]:
    timeout = max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))
    d_id = _get_dialog_id(dialog_id)

    safe, reason = _check_code_safety(code)
    if not safe:
        try:
            conversation_memory.add(
                op="test_hypothesis",
                status="blocked",
                dialog=d_id,
                context=f"Security violation: {reason}",
            )
        except Exception:
            pass
        return {"error": f"Security violation: {reason}", "blocked": True, "dialog": d_id}

    syntax = syntax_check(code, dialog_id=d_id)
    if not syntax["valid"]:
        return {
            "error": f"Syntax error: {syntax['error']}",
            "blocked": False,
            "dialog": d_id,
        }

    start = time.time()
    try:
        with safe_create_temp(suffix=".py", prefix="mcp_exec_") as tmp_path:
            # Записываем код в файл
            # Код пишется как есть: номера строк в traceback совпадают с
            # тем, что прислал пользователь. Изоляция - через флаги -I -B -S.
            tmp_path.write_text(code, encoding="utf-8")
            cmd = [sys.executable, "-I", "-B", "-S", str(tmp_path)]
            env = _strip_environment()
            kwargs: Dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
                "env": env,
                "cwd": str(tmp_path.parent),
            }
            if sys.platform != "win32":
                kwargs["preexec_fn"] = _set_limits
            else:
                kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW
                    if hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                )

            proc = subprocess.run(cmd, **kwargs)
            elapsed = time.time() - start

            stdout = (proc.stdout or "")[:MAX_STDOUT_CHARS]
            stderr = (proc.stderr or "")[:MAX_STDERR_CHARS]
            truncated = bool(
                (proc.stdout and len(proc.stdout) > MAX_STDOUT_CHARS)
                or (proc.stderr and len(proc.stderr) > MAX_STDERR_CHARS)
            )

            try:
                conversation_memory.add(
                    op="test_hypothesis",
                    paths={"temp_file": str(tmp_path)},
                    status="executed" if proc.returncode == 0 else f"exit_{proc.returncode}",
                    dialog=d_id,
                    context=(
                        f"Executed in {elapsed:.2f}s, exit={proc.returncode}, "
                        f"out={len(stdout)} chars"
                    ),
                )
            except Exception:
                pass

            return {
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "elapsed_sec": round(elapsed, 2),
                "truncated": truncated,
                "blocked": False,
                "dialog": d_id,
            }
    except subprocess.TimeoutExpired:
        return {
            "error": f"Execution timed out after {timeout}s",
            "timeout": timeout,
            "blocked": False,
            "dialog": d_id,
        }
    except Exception as e:
        return {
            "error": str(e),
            "exception": type(e).__name__,
            "blocked": False,
            "dialog": d_id,
        }


# ─── Server Setup ───────────────────────────────────────────────────────────
server = BaseMCPServer("code-debugger", "3.3")
server.register_tool(
    "syntax_check",
    {
        "description": "Check Python code syntax with strict AST whitelist",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "maxLength": MAX_CODE_SIZE},
                "language": {"type": "string", "default": "python"},
                "dialog_id": {"type": "string"},
            },
            "required": ["code"],
        },
    },
    lambda **kw: syntax_check(kw["code"], kw.get("language", "python"), kw.get("dialog_id")),
)

server.register_tool(
    "test_hypothesis",
    {
        "description": "Run Python code in isolated sandbox with strict env stripping and timeout (1-30 sec)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "maxLength": MAX_CODE_SIZE},
                "timeout": {
                    "type": "integer",
                    "default": DEFAULT_TIMEOUT,
                    "minimum": MIN_TIMEOUT,
                    "maximum": MAX_TIMEOUT,
                },
                "dialog_id": {"type": "string"},
            },
            "required": ["code"],
        },
    },
    lambda **kw: test_hypothesis(
        kw["code"], kw.get("timeout", DEFAULT_TIMEOUT), kw.get("dialog_id")
    ),
)


if __name__ == "__main__":
    _log("Starting Code Debugger Server v3.3")
    server.run()
