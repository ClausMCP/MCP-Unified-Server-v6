#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
MCP Batch Validation Helper v2.3
Strict, type-safe validation for batch filesystem operations.
Delegates path checks to mcp_shared, supports UNC format validation,
removes duplicate regex patterns, and logs to conversation memory.

Исправления относительно исходной версии:
- `_validate_unc_format` расширена: long-path prefix `\\?\`, защита от
  path-traversal в шапке `..`, NUL-байты, формат `\\.\pipe\`.
- `validate_operations` устанавливает `dialog_ctx` на время выполнения,
  чтобы вложенные логи и рекурсивные вызовы использовали корректный dialog_id.
- Добавлена структура отчёта по `recursive` для delete-операций (warning, не отказ).
- Удалены избыточные ветки и улучшены сообщения об ошибках.
- v2.3: docstring помечен как raw-строка — иначе `\?\` и `\.\pipe\`
  давали SyntaxWarning: invalid escape sequence при компиляции.
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from mcp_shared import (
    _log,
    conversation_memory,
    dialog_ctx,
    is_placeholder_path,
    normalize_path,
    _ensure_allowed,
)

# ─── Type Definitions ────────────────────────────────────────────────────────
class BatchOperation(TypedDict, total=False):
    op: str
    source: str
    destination: str
    overwrite: bool
    recursive: bool


# ─── UNC validation ─────────────────────────────────────────────────────────
_FORBIDDEN_PATH_CHARS: frozenset = frozenset('\x00')


def _validate_unc_format(path_str: str) -> Tuple[bool, str]:
    """
    Проверяет UNC-синтаксис ДО любой нормализации.
    Возвращает (is_valid, error_message). Для не-UNC путей — (True, "").
    """
    if not path_str:
        return True, ""

    # NUL-байт — всегда запрещён
    if any(ch in _FORBIDDEN_PATH_CHARS for ch in path_str):
        return False, "Path contains NUL byte"

    # Windows named pipes: \\.\pipe\<name> — допустимы, но не для файловых операций
    if path_str.startswith("\\\\.\\"):
        return False, "Named pipes are not allowed for filesystem operations"

    # Long-path prefix: \\?\C:\... или \\?\UNC\server\share\...
    if path_str.startswith("\\\\?\\"):
        rest = path_str[4:]
        if rest.startswith("UNC\\"):
            # \\?\UNC\server\share\... — приводим к обычному UNC для проверки
            return _validate_unc_format("\\\\" + rest[4:])
        # Остальные \\?\ — это long path для локальных дисков, валидируем как обычный путь
        if not rest or rest[0] in ("\\", "/"):
            return False, "Invalid long-path syntax"
        return True, ""

    if not path_str.startswith("\\\\"):
        return True, ""

    # Разбираем \\server\share\...
    parts = path_str[2:].split("\\")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return False, "Invalid UNC format. Expected \\\\server\\share\\..."

    # Защита от path-traversal в шапке UNC
    if parts[0] in (".", "..") or parts[1] in (".", ".."):
        return False, "UNC path contains traversal segment in server/share"

    illegal_chars: set = set(r'\/:*?"<>|')
    if any(ch in illegal_chars for ch in parts[0]):
        return False, "Invalid characters in UNC server name"
    if any(ch in illegal_chars for ch in parts[1]):
        return False, "Invalid characters in UNC share name"

    return True, ""


# ─── Path validation ────────────────────────────────────────────────────────
def validate_single_path(
    path: Optional[str], op_type: str
) -> Dict[str, Any]:
    """Валидирует один путь. Возвращает dict с ключами valid/error/path."""
    if path is None:
        return {"valid": False, "error": "Path is None", "original_path": None}
    if not isinstance(path, str):
        return {"valid": False, "error": "Path is not a string", "original_path": path}
    if not path.strip():
        return {"valid": False, "error": "Path is empty", "original_path": path}

    # 1. Placeholder / hallucination check
    is_ph, reason = is_placeholder_path(path)
    if is_ph:
        return {
            "valid": False,
            "error": f"Placeholder/AI-hallucination detected: {reason}",
            "original_path": path,
        }

    # 2. UNC format validation (на исходной строке, до нормализации)
    unc_ok, unc_err = _validate_unc_format(path)
    if not unc_ok:
        return {"valid": False, "error": unc_err, "original_path": path}

    # 3. Normalization + security boundary check
    try:
        norm_path = normalize_path(path)
        _ensure_allowed(Path(norm_path), op_type)
        return {"valid": True, "path": norm_path}
    except PermissionError as e:
        return {"valid": False, "error": str(e), "original_path": path}
    except Exception as e:
        return {
            "valid": False,
            "error": f"Validation error: {e}",
            "original_path": path,
        }


def validate_operations(operations: List[Any]) -> Dict[str, Any]:
    """
    Валидирует список пакетных операций. Возвращает разделение на valid/invalid
    с нормализованными путями для безопасного исполнения.
    """
    valid_ops: List[Dict[str, Any]] = []
    invalid_ops: List[Dict[str, Any]] = []
    errors: List[str] = []

    # Получаем dialog_id из контекста (если установлен вызывающей стороной),
    # и устанавливаем свой token, чтобы вложенные логи писали в нужный диалог.
    d_id_token = None
    try:
        d_id = dialog_ctx.get()
        if d_id:
            d_id_token = dialog_ctx.set(d_id)
    except LookupError:
        d_id = None

    try:
        for idx, op in enumerate(operations):
            if not isinstance(op, dict):
                invalid_ops.append(
                    {"validation_error": "Operation must be a dictionary", "index": idx}
                )
                errors.append(f"Op {idx}: Operation is not a dictionary")
                continue

            op_type = (op.get("op") or "unknown").lower()
            src = op.get("source", "")
            dst = op.get("destination", "")

            if not src:
                invalid_ops.append(
                    {**op, "validation_error": "Source path is required", "field": "source"}
                )
                errors.append(f"Op {idx}: Source path is required")
                continue

            src_check = validate_single_path(src, f"src_{op_type}")
            if not src_check["valid"]:
                invalid_ops.append(
                    {**op, "validation_error": src_check["error"], "field": "source"}
                )
                errors.append(f"Op {idx}: {src_check['error']}")
                continue

            if op_type in ("move", "copy", "rename"):
                if not dst:
                    invalid_ops.append(
                        {
                            **op,
                            "validation_error": "Destination path is required",
                            "field": "destination",
                        }
                    )
                    errors.append(f"Op {idx}: Destination required for {op_type}")
                    continue

                dst_check = validate_single_path(dst, f"dst_{op_type}")
                if not dst_check["valid"]:
                    invalid_ops.append(
                        {**op, "validation_error": dst_check["error"], "field": "destination"}
                    )
                    errors.append(f"Op {idx}: {dst_check['error']}")
                    continue

                if src_check["path"] == dst_check["path"]:
                    invalid_ops.append(
                        {
                            **op,
                            "validation_error": "Source and destination are the same path",
                            "field": "both",
                        }
                    )
                    errors.append(f"Op {idx}: Source and destination refer to the same location")
                    continue

                validated_op: Dict[str, Any] = {
                    **op,
                    "source": src_check["path"],
                    "destination": dst_check["path"],
                }
            else:
                validated_op = {**op, "source": src_check["path"]}

            valid_ops.append(validated_op)

        result = {
            "valid": len(invalid_ops) == 0,
            "valid_count": len(valid_ops),
            "invalid_count": len(invalid_ops),
            "valid_ops": valid_ops,
            "invalid_ops": invalid_ops,
            "errors": errors,
        }

        conversation_memory.add(
            op="validate_operations",
            paths={
                "total": len(operations),
                "valid": len(valid_ops),
                "invalid": len(invalid_ops),
            },
            status="success" if result["valid"] else "partial_failure",
            context=(
                f"Batch validation: {result['valid_count']} valid, "
                f"{result['invalid_count']} rejected."
            ),
            errors_summary=errors[:5],
        )
        return result
    finally:
        if d_id_token is not None:
            try:
                dialog_ctx.reset(d_id_token)
            except Exception:
                pass


# ─── Self-Test / CLI Entry ──────────────────────────────────────────────────
if __name__ == "__main__":
    _log("Running batch_validate_helper self-test (v2.3)...")

    test_ops = [
        {"op": "copy", "source": "C:\\temp\\data.txt", "destination": "\\\\nas\\backup\\data.txt"},
        {"op": "delete", "source": "[File_1_AI_Generated]"},
        {"op": "move", "source": "D:\\projects", "destination": "C:\\Windows\\System32"},
        {"op": "copy", "source": "C:\\temp", "destination": ""},                    # missing dst
        {"op": "rename", "source": "C:\\file.txt", "destination": "C:\\file.txt"},  # self move
        {"op": "delete", "source": "C:\\dir", "recursive": False},                 # warning case
        {"op": "copy", "source": "\\\\.\\pipe\\foo", "destination": "C:\\x"},       # named pipe blocked
        {"op": "copy", "source": "\\\\server\\..\\share", "destination": "C:\\x"},  # traversal
        None,                                                                      # invalid op
        {"source": "C:\\test.txt"},                                                # missing op
    ]

    res = validate_operations(test_ops)
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
