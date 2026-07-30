#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Batch v3.6 (Server-Deterministic)
Secure batch operations with hallucination prevention, chunked execution,
and dialog context isolation. Integrates with batch_validate_helper.

v3.6 changes:
- FIX: validate_operations() called without removed `strict` kwarg (TypeError in v3.5).
- NEW: server-side filename sanitization (Windows-illegal chars, reserved names,
  trailing dots/spaces, length limit). The LLM no longer needs to know these rules.
- NEW: deterministic conflict detection BEFORE execution:
    * duplicate destinations inside the batch
    * destination already exists on disk
    * missing source files
  Default on_conflict="ask": nothing is executed, a `needs_confirmation` report
  is returned so the user decides (suffix / skip / overwrite).
- NEW: batch_rename_map tool — rename by explicit old→new mapping in one folder.
  No regex, no shell, no LLM reasoning required.
- FIX: smart_move destination logic; LLM plan now passes the same server pipeline.
- batch_rename (regex) now detects in-batch name collisions and sanitizes names.
"""
import os
import sys
import json
import time
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from mcp_shared import (
    _log, normalize_path, _ensure_allowed, is_placeholder_path,
    BaseMCPServer, conversation_memory, dialog_ctx,
    list_directory_sync, query_llm, validate_paths_decorator
)

try:
    from batch_validate_helper import validate_operations
    HAS_VALIDATOR = True
except ImportError:
    HAS_VALIDATOR = False

# ─── Filename Sanitization (server-side, deterministic) ─────────────────────
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}
_MAX_NAME_LEN = 200  # conservative; full path limit on Windows is 260 without long-path support


def sanitize_filename(name: str) -> Tuple[str, List[str]]:
    """
    Make a single file NAME (not a path) legal for Windows/NTFS.
    Returns (sanitized_name, list_of_changes). Empty change list => name was already legal.
    Rules:
      ':'  -> ' -'   (readable, common in document titles)
      other illegal chars < > " / \\ | ? * and control chars -> '_'
      trailing dots/spaces stripped (Windows silently rejects them)
      reserved device names (CON, NUL, COM1..) -> prefixed with '_'
      overlong names truncated, extension preserved
    """
    changes: List[str] = []
    original = name

    stem, dot, ext = name.rpartition(".")
    if not dot:  # no extension
        stem, ext = name, ""

    def _clean(part: str) -> str:
        out = part.replace(":", " -")
        out = re.sub(r'[<>"/\\|?*]', "_", out)
        out = re.sub(r"[\x00-\x1f]", "", out)
        out = re.sub(r"\s{2,}", " ", out)
        return out

    new_stem = _clean(stem).rstrip(" .")
    new_ext = _clean(ext).strip(" .")

    if not new_stem:
        new_stem = "unnamed"
        changes.append("empty name replaced with 'unnamed'")

    if new_stem.upper() in _WIN_RESERVED:
        new_stem = "_" + new_stem
        changes.append(f"reserved device name '{stem}' prefixed")

    new_name = f"{new_stem}.{new_ext}" if new_ext else new_stem

    if len(new_name) > _MAX_NAME_LEN:
        keep = _MAX_NAME_LEN - (len(new_ext) + 1 if new_ext else 0)
        new_stem = new_stem[:max(keep, 1)].rstrip(" .")
        new_name = f"{new_stem}.{new_ext}" if new_ext else new_stem
        changes.append(f"name truncated to {_MAX_NAME_LEN} chars")

    if new_name != original and not changes:
        changes.append("illegal characters replaced")

    return new_name, changes


def _suffix_name(name: str, n: int) -> str:
    """document.pdf, 2 -> document_2.pdf"""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name}_{n}"
    return f"{stem}_{n}.{ext}"


# ─── Deterministic Pre-flight Pipeline ───────────────────────────────────────
def prepare_operations(operations: List[Dict[str, str]],
                       on_conflict: str = "ask",
                       sanitize: bool = True) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """
    Server-side pipeline executed BEFORE any file is touched.
    1. Sanitize destination filenames (report every change).
    2. Check that sources exist (missing ones are skipped + reported,
       so re-running a partially completed batch is safe).
    3. Detect duplicate destinations inside the batch.
    4. Detect destinations that already exist on disk.

    on_conflict:
      "ask"       - if any conflict found, return NO operations; caller must
                    surface the report to the user and retry with another mode.
      "suffix"    - auto-append _2, _3 ... (checks both batch and disk).
      "skip"      - drop conflicting operations, execute the rest.
      "overwrite" - keep destinations as-is (existing files will be replaced).

    Returns (ready_operations, report).
    report["blocking"] is True when on_conflict == "ask" and conflicts exist.
    """
    report: Dict[str, Any] = {
        "sanitized": [],      # [{"from": ..., "to": ..., "reasons": [...]}]
        "missing_sources": [],
        "duplicate_targets": [],
        "existing_targets": [],
        "skipped": [],
        "blocking": False,
    }

    if on_conflict not in ("ask", "suffix", "skip", "overwrite"):
        on_conflict = "ask"

    # Phase 1: sanitize + existence checks
    staged: List[Dict[str, str]] = []
    for op in operations:
        src = Path(normalize_path(op["source"]))
        dst = Path(normalize_path(op["destination"]))

        if sanitize:
            clean_name, changes = sanitize_filename(dst.name)
            if changes:
                report["sanitized"].append(
                    {"from": dst.name, "to": clean_name, "reasons": changes}
                )
                dst = dst.parent / clean_name

        if not src.exists():
            report["missing_sources"].append(str(src))
            report["skipped"].append({"source": str(src), "reason": "source not found"})
            continue

        staged.append({"source": str(src), "destination": str(dst)})

    # Phase 2: conflict detection
    seen: Dict[str, int] = {}          # destination(lower) -> count so far
    ready: List[Dict[str, str]] = []
    taken_on_disk = set()              # destinations we will create in this batch

    for op in staged:
        src = Path(op["source"])
        dst = Path(op["destination"])
        key = str(dst).lower()

        dup_in_batch = key in seen
        exists_on_disk = dst.exists() and str(dst).lower() != str(src).lower()

        if dup_in_batch:
            report["duplicate_targets"].append(
                {"source": str(src), "destination": str(dst)}
            )
        if exists_on_disk:
            report["existing_targets"].append(
                {"source": str(src), "destination": str(dst)}
            )

        if dup_in_batch or exists_on_disk:
            if on_conflict == "skip":
                report["skipped"].append(
                    {"source": str(src), "reason": "conflict (skip mode)"}
                )
                continue
            if on_conflict == "suffix":
                n = seen.get(key, 1) + 1
                candidate = dst.parent / _suffix_name(dst.name, n)
                while (str(candidate).lower() in taken_on_disk
                       or candidate.exists()):
                    n += 1
                    candidate = dst.parent / _suffix_name(dst.name, n)
                seen[key] = n
                dst = candidate
                key = str(dst).lower()
            # "overwrite" and "ask" keep destination as-is

        seen.setdefault(key, 1)
        taken_on_disk.add(key)
        ready.append({"source": str(src), "destination": str(dst)})

    has_conflicts = bool(report["duplicate_targets"] or report["existing_targets"])
    if on_conflict == "ask" and has_conflicts:
        report["blocking"] = True
        return [], report

    return ready, report


def _needs_confirmation(report: Dict[str, Any], total: int) -> Dict[str, Any]:
    """Standard response when conflicts must be resolved by the USER."""
    return {
        "status": "needs_confirmation",
        "executed": 0,
        "total": total,
        "message": (
            "Обнаружены конфликты. Ничего не выполнено. "
            "Покажите пользователю списки duplicate_targets / existing_targets / "
            "missing_sources и спросите, как поступить. Затем повторите вызов с "
            "on_conflict='suffix' (добавить _2, _3...), 'skip' (пропустить) или "
            "'overwrite' (заменить)."
        ),
        "duplicate_targets": report["duplicate_targets"],
        "existing_targets": report["existing_targets"],
        "missing_sources": report["missing_sources"],
        "sanitized": report["sanitized"],
    }


def _run_validator(operations: List[Dict[str, str]]) -> Optional[Dict]:
    """Placeholder/hallucination validation. FIX v3.6: no `strict` kwarg."""
    if not HAS_VALIDATOR:
        return None
    return validate_operations(operations)


# ─── Core Batch Operations ───────────────────────────────────────────────────
def batch_move(operations: List[Dict[str, str]], dry_run: bool = False,
               on_conflict: str = "ask", sanitize: bool = True,
               chunk_size: int = 50, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    requested = len(operations)

    # Validation Phase (placeholders / hallucinated paths)
    validation = _run_validator(operations)
    if validation is not None:
        if not validation["valid"] and not validation.get("valid_ops"):
            conversation_memory.add(
                op="batch_move", paths={"count": requested},
                status="validation_failed", dialog=d_id,
                context=f"Batch validation failed: {len(validation.get('invalid_ops', []))} invalid ops"
            )
            return validation
        operations = validation["valid_ops"]
    else:
        for i, op in enumerate(operations):
            is_ph, reason = is_placeholder_path(op.get("source", ""))
            if is_ph:
                return {"status": "validation_failed", "index": i, "reason": reason}

    # Deterministic pre-flight: sanitize names, detect conflicts
    operations, report = prepare_operations(operations, on_conflict, sanitize)
    if report["blocking"]:
        conversation_memory.add(
            op="batch_move", paths={"count": requested},
            status="needs_confirmation", dialog=d_id,
            context=(f"Conflicts: {len(report['duplicate_targets'])} duplicates, "
                     f"{len(report['existing_targets'])} existing targets")
        )
        return _needs_confirmation(report, requested)

    if not operations:
        return {
            "status": "empty",
            "message": "No valid operations after validation",
            "missing_sources": report["missing_sources"],
            "skipped": report["skipped"],
        }

    # Execution Phase (Chunked)
    total = len(operations)
    success = []
    failed = []
    start = time.time()

    for chunk_start in range(0, total, chunk_size):
        chunk = operations[chunk_start:chunk_start + chunk_size]
        for op in chunk:
            src = Path(op["source"])
            dst = Path(op["destination"])
            try:
                _ensure_allowed(src, "batch_move")
                _ensure_allowed(dst.parent, "batch_move")

                if dry_run:
                    success.append({"source": str(src), "destination": str(dst), "status": "dry_run"})
                    continue

                dst.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    shutil.rmtree(src)
                else:
                    shutil.move(str(src), str(dst))
                success.append({"source": str(src), "destination": str(dst), "status": "moved"})
            except Exception as e:
                failed.append({"source": str(src), "destination": str(dst), "error": str(e)})

    elapsed = time.time() - start
    status = "completed" if not failed else "partial"

    conversation_memory.add(
        op="batch_move",
        paths={"source_count": total, "success_count": len(success)},
        status=status, dialog=d_id,
        context=f"Moved {len(success)}/{total} files in {elapsed:.1f}s"
    )

    return {
        "status": status,
        "requested": requested,
        "total": total,
        "success": len(success),
        "failed": len(failed),
        "details": failed if failed else None,
        "sanitized": report["sanitized"] or None,
        "skipped": report["skipped"] or None,
        "missing_sources": report["missing_sources"] or None,
        "elapsed_sec": round(elapsed, 2),
        "dry_run": dry_run
    }


def batch_rename_map(path: str, renames: List[Dict[str, str]],
                     on_conflict: str = "ask", sanitize: bool = True,
                     dry_run: bool = False, dialog_id: str = None) -> Dict:
    """
    Rename files inside ONE directory by an explicit old→new mapping.
    Designed so the LLM only forwards the user's list — the server handles
    illegal characters, duplicates, existing files and missing sources.
    Accepts items as {"old": ..., "new": ...} or {"source": ..., "destination": ...}.
    Extension of the source is appended to the new name if the user omitted it.
    """
    d_id = dialog_id or dialog_ctx.get()
    base = Path(normalize_path(path))
    _ensure_allowed(base, "batch_rename_map")

    if not base.is_dir():
        return {"status": "error", "message": f"Directory not found: {base}"}

    operations: List[Dict[str, str]] = []
    bad_items: List[Dict[str, Any]] = []
    for idx, item in enumerate(renames):
        if not isinstance(item, dict):
            bad_items.append({"index": idx, "error": "item is not an object"})
            continue
        old = item.get("old") or item.get("source") or ""
        new = item.get("new") or item.get("destination") or ""
        if not old or not new:
            bad_items.append({"index": idx, "error": "both 'old' and 'new' are required"})
            continue

        old_name = Path(old).name          # tolerate full paths from the model
        new_name = Path(new).name
        src_ext = Path(old_name).suffix
        if src_ext and not new_name.lower().endswith(src_ext.lower()):
            new_name += src_ext            # keep the original extension

        operations.append({
            "source": str(base / old_name),
            "destination": str(base / new_name),
        })

    if not operations:
        return {"status": "error", "message": "No valid rename pairs", "invalid_items": bad_items}

    result = batch_move(operations, dry_run=dry_run, on_conflict=on_conflict,
                        sanitize=sanitize, chunk_size=100, dialog_id=d_id)
    if bad_items:
        result["invalid_items"] = bad_items
    return result


def batch_copy(operations: List[Dict[str, str]], dry_run: bool = False,
               on_conflict: str = "ask", sanitize: bool = True,
               chunk_size: int = 50, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    requested = len(operations)

    validation = _run_validator(operations)
    if validation is not None:
        operations = validation["valid_ops"]

    operations, report = prepare_operations(operations, on_conflict, sanitize)
    if report["blocking"]:
        return _needs_confirmation(report, requested)

    total = len(operations)
    success, failed = [], []
    start = time.time()

    for op in operations:
        src = Path(op["source"])
        dst = Path(op["destination"])
        try:
            _ensure_allowed(src, "batch_copy")
            _ensure_allowed(dst.parent, "batch_copy")
            if dry_run:
                success.append({"source": str(src), "destination": str(dst), "status": "dry_run"})
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            success.append({"source": str(src), "destination": str(dst), "status": "copied"})
        except Exception as e:
            failed.append({"source": str(src), "destination": str(dst), "error": str(e)})

    conversation_memory.add(
        op="batch_copy", paths={"count": total}, status="completed" if not failed else "partial",
        dialog=d_id, context=f"Copied {len(success)}/{total} files"
    )
    return {
        "status": "completed" if not failed else "partial",
        "requested": requested,
        "success": len(success), "failed": len(failed),
        "details": failed if failed else None,
        "sanitized": report["sanitized"] or None,
        "skipped": report["skipped"] or None,
        "elapsed_sec": round(time.time() - start, 2),
        "dry_run": dry_run
    }


def batch_delete(paths: List[str], use_trash: bool = True,
                 dry_run: bool = False, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    success, failed = [], []
    start = time.time()

    for p_str in paths:
        p = Path(normalize_path(p_str))
        try:
            _ensure_allowed(p, "batch_delete")
            if dry_run:
                success.append({"path": str(p), "status": "dry_run"})
                continue

            if use_trash:
                try:
                    from mcp_fs_trash import move_to_trash
                    res = move_to_trash(str(p), dialog_id=d_id)
                    success.append({"path": str(p), "status": "trashed", "trash_id": res.get("trash_id")})
                except ImportError:
                    use_trash = False

            if not use_trash:
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p)
                else:
                    p.unlink()
                success.append({"path": str(p), "status": "deleted"})
        except Exception as e:
            failed.append({"path": str(p), "error": str(e)})

    conversation_memory.add(
        op="batch_delete", paths={"count": len(paths)},
        status="completed" if not failed else "partial", dialog=d_id,
        context=f"Deleted {len(success)}/{len(paths)} items (trash={use_trash})"
    )
    return {
        "status": "completed" if not failed else "partial",
        "success": len(success), "failed": len(failed),
        "details": failed if failed else None, "elapsed_sec": round(time.time() - start, 2)
    }


# ─── Smart Operations (LLM-Assisted) ────────────────────────────────────────
def smart_move(natural_query: str, target_dir: str, source_dir: str = None,
               dry_run: bool = False, on_conflict: str = "ask",
               dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    src = Path(normalize_path(source_dir or "."))
    tgt = Path(normalize_path(target_dir))
    _ensure_allowed(src, "smart_move")
    _ensure_allowed(tgt, "smart_move")

    if not tgt.exists():
        tgt.mkdir(parents=True, exist_ok=True)

    # 1. Scan directory
    dir_listing = list_directory_sync(str(src))
    if "error" in dir_listing:
        return {"status": "error", "message": dir_listing["error"]}

    files = [e["name"] for e in dir_listing.get("entries", []) if e["is_file"]]
    if not files:
        return {"status": "error", "message": "No files in source directory"}

    # 2. Ask LLM to generate move plan (names only — server builds real paths)
    prompt = f"""
    You are a file organization assistant.
    User query: "{natural_query}"
    Available files: {json.dumps(files[:50], ensure_ascii=False)}

    Return ONLY a valid JSON array of objects with "source" and "destination" keys.
    "source" must be one of the available file names EXACTLY as listed.
    "destination" is the new file name (optionally with a subfolder, e.g. "Docs/report.pdf").
    Example: [{{"source": "file1.jpg", "destination": "Images/file1.jpg"}}]
    Do not invent files. Do not use markdown formatting.
    """
    llm_resp = query_llm(prompt)
    if not llm_resp:
        return {"status": "error", "message": "LLM did not return a plan"}

    # 3. Parse LLM output
    try:
        plan = json.loads(llm_resp)
        if not isinstance(plan, list):
            raise ValueError("Expected JSON array")
    except (json.JSONDecodeError, ValueError) as e:
        return {"status": "error", "message": f"LLM returned invalid JSON: {e}"}

    # 4. Server-side hardening: only real files, destinations relative to target
    known = set(files)
    operations = []
    hallucinated = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        s_name = Path(str(item.get("source", ""))).name
        if s_name not in known:
            hallucinated.append(item.get("source"))
            continue
        d_raw = str(item.get("destination") or s_name)
        d_rel = Path(d_raw)
        if d_rel.is_absolute() or ".." in d_rel.parts:
            d_rel = Path(d_rel.name)   # never let the model escape target_dir
        operations.append({
            "source": str(src / s_name),
            "destination": str(tgt / d_rel),
        })

    if not operations:
        return {"status": "error", "message": "LLM plan contained no valid files",
                "hallucinated": hallucinated or None}

    # 5. Execute via the deterministic pipeline
    result = batch_move(operations, dry_run=dry_run, on_conflict=on_conflict, dialog_id=d_id)
    if hallucinated:
        result["hallucinated"] = hallucinated
    return result


# ─── Auto-Scan & Rename ─────────────────────────────────────────────────────
def auto_scan(path: str, rules: Dict[str, List[str]] = None,
              dry_run: bool = False, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    p = Path(normalize_path(path))
    _ensure_allowed(p, "auto_scan")

    if not rules:
        rules = {
            "Images": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"],
            "Documents": [".pdf", ".doc", ".docx", ".txt", ".xlsx", ".pptx"],
            "Archives": [".zip", ".rar", ".7z", ".tar", ".gz"],
            "Installers": [".exe", ".msi", ".dmg", ".pkg"],
            "Code": [".py", ".js", ".html", ".css", ".json", ".md"]
        }

    dir_listing = list_directory_sync(str(p))
    if "error" in dir_listing:
        return {"status": "error", "message": dir_listing["error"]}

    operations = []
    for entry in dir_listing.get("entries", []):
        if not entry["is_file"]:
            continue
        ext = Path(entry["name"]).suffix.lower()
        for category, exts in rules.items():
            if ext in exts:
                dest_dir = p / category
                operations.append({
                    "source": entry["path"],
                    "destination": str(dest_dir / entry["name"])
                })
                break

    # Sorting into fresh subfolders rarely conflicts; suffix silently if it does.
    return batch_move(operations, dry_run=dry_run, on_conflict="suffix", dialog_id=d_id)


def batch_rename(path: str, pattern: str, replacement: str = "",
                 on_conflict: str = "ask",
                 dry_run: bool = False, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    p = Path(normalize_path(path))
    _ensure_allowed(p, "batch_rename")

    dir_listing = list_directory_sync(str(p))
    if "error" in dir_listing:
        return {"status": "error", "message": dir_listing["error"]}

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return {"status": "error", "message": f"Invalid regex: {e}"}

    operations = []
    skipped_same = 0
    for entry in dir_listing.get("entries", []):
        if not entry["is_file"]:
            continue
        old_name = entry["name"]
        new_name = regex.sub(replacement, old_name)
        if old_name == new_name:
            skipped_same += 1
            continue
        operations.append({
            "source": entry["path"],
            "destination": str(Path(entry["path"]).parent / new_name),
        })

    if not operations:
        return {"status": "completed", "renamed": 0, "skipped": skipped_same,
                "message": "No files matched the pattern"}

    # Route through the same deterministic pipeline (collisions, sanitize, ask)
    result = batch_move(operations, dry_run=dry_run, on_conflict=on_conflict,
                        chunk_size=100, dialog_id=d_id)
    result["unmatched"] = skipped_same

    conversation_memory.add(
        op="batch_rename", paths={"path": str(p)},
        status=result.get("status", "completed"),
        dialog=d_id,
        context=f"Renamed {result.get('success', 0)} files matching '{pattern}'"
    )
    return result


# ─── Server Setup ────────────────────────────────────────────────────────────
_CONFLICT_NOTE = (
    "If the result status is 'needs_confirmation', DO NOT retry silently: show the "
    "user the conflicts (duplicate_targets, existing_targets, missing_sources) and "
    "ask how to proceed, then call again with on_conflict='suffix', 'skip' or 'overwrite'. "
    "Illegal Windows characters in names are fixed automatically by the server "
    "(see 'sanitized' in the result)."
)

_ON_CONFLICT_SCHEMA = {
    "type": "string",
    "enum": ["ask", "suffix", "skip", "overwrite"],
    "default": "ask",
    "description": "ask = stop and report conflicts; suffix = add _2, _3...; skip = drop conflicting ops; overwrite = replace existing files"
}

server = BaseMCPServer("filesystem-batch", "3.6")

server.register_tool("batch_move", {
    "description": "Move multiple files/directories with server-side validation, "
                   "filename sanitization and conflict detection. " + _CONFLICT_NOTE,
    "inputSchema": {
        "type": "object",
        "properties": {
            "operations": {"type": "array", "items": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}}},
            "dry_run": {"type": "boolean", "default": False},
            "on_conflict": _ON_CONFLICT_SCHEMA,
            "sanitize": {"type": "boolean", "default": True},
            "chunk_size": {"type": "integer", "default": 50},
            "dialog_id": {"type": "string"}
        },
        "required": ["operations"]
    }
}, lambda **kw: batch_move(
    kw["operations"], kw.get("dry_run", False), kw.get("on_conflict", "ask"),
    kw.get("sanitize", True), kw.get("chunk_size", 50), kw.get("dialog_id")
))

server.register_tool("batch_rename_map", {
    "description": "PREFERRED tool for renaming files by an explicit old→new list inside one "
                   "directory (e.g. user provides a mapping of scanned files to document titles). "
                   "Just forward the user's pairs as-is: the server fixes illegal Windows "
                   "characters, keeps extensions, detects duplicates and existing files. "
                   + _CONFLICT_NOTE,
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory containing the files"},
            "renames": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string", "description": "Current file name"},
                        "new": {"type": "string", "description": "New file name (extension optional)"}
                    },
                    "required": ["old", "new"]
                }
            },
            "on_conflict": _ON_CONFLICT_SCHEMA,
            "sanitize": {"type": "boolean", "default": True},
            "dry_run": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["path", "renames"]
    }
}, lambda **kw: batch_rename_map(
    kw["path"], kw["renames"], kw.get("on_conflict", "ask"),
    kw.get("sanitize", True), kw.get("dry_run", False), kw.get("dialog_id")
))

server.register_tool("batch_copy", {
    "description": "Copy multiple files/directories with sanitization and conflict detection. "
                   + _CONFLICT_NOTE,
    "inputSchema": {
        "type": "object",
        "properties": {
            "operations": {"type": "array", "items": {"type": "object"}},
            "dry_run": {"type": "boolean", "default": False},
            "on_conflict": _ON_CONFLICT_SCHEMA,
            "sanitize": {"type": "boolean", "default": True},
            "chunk_size": {"type": "integer", "default": 50},
            "dialog_id": {"type": "string"}
        },
        "required": ["operations"]
    }
}, lambda **kw: batch_copy(
    kw["operations"], kw.get("dry_run", False), kw.get("on_conflict", "ask"),
    kw.get("sanitize", True), kw.get("chunk_size", 50), kw.get("dialog_id")
))

server.register_tool("batch_delete", {
    "description": "Delete multiple files/directories (optionally to trash)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "use_trash": {"type": "boolean", "default": True},
            "dry_run": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["paths"]
    }
}, lambda **kw: batch_delete(
    kw["paths"], kw.get("use_trash", True), kw.get("dry_run", False), kw.get("dialog_id")
))

server.register_tool("smart_move", {
    "description": "Move files based on natural language query using LLM planning. "
                   "The plan is validated server-side (no invented files, no path escapes). "
                   + _CONFLICT_NOTE,
    "inputSchema": {
        "type": "object",
        "properties": {
            "natural_query": {"type": "string"},
            "target_dir": {"type": "string"},
            "source_dir": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
            "on_conflict": _ON_CONFLICT_SCHEMA,
            "dialog_id": {"type": "string"}
        },
        "required": ["natural_query", "target_dir"]
    }
}, lambda **kw: smart_move(
    kw["natural_query"], kw["target_dir"], kw.get("source_dir"),
    kw.get("dry_run", False), kw.get("on_conflict", "ask"), kw.get("dialog_id")
))

server.register_tool("auto_scan", {
    "description": "Automatically sort files into categories based on extensions",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "rules": {"type": "object"},
            "dry_run": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["path"]
    }
}, lambda **kw: auto_scan(
    kw["path"], kw.get("rules"), kw.get("dry_run", False), kw.get("dialog_id")
))

server.register_tool("batch_rename", {
    "description": "Rename multiple files using a regex pattern. For explicit old→new "
                   "lists use batch_rename_map instead. " + _CONFLICT_NOTE,
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "pattern": {"type": "string"},
            "replacement": {"type": "string", "default": ""},
            "on_conflict": _ON_CONFLICT_SCHEMA,
            "dry_run": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["path", "pattern"]
    }
}, lambda **kw: batch_rename(
    kw["path"], kw["pattern"], kw.get("replacement", ""),
    kw.get("on_conflict", "ask"), kw.get("dry_run", False), kw.get("dialog_id")
))

if __name__ == "__main__":
    server.run()
