#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Trash v3.1 (Context-Isolated)
Safe deletion with recoverable trash, metadata preservation,
and automatic cleanup by age AND size. Integrates with conversation memory.
Uses contextvars for secure dialog isolation.
"""
import os
import sys
import json
import shutil
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from mcp_shared import (
    _log, normalize_path, _ensure_allowed, _format_size,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Configuration ───────────────────────────────────────────────────────────
TRASH_ROOT = os.environ.get(
    "MCP_TRASH_PATH",
    os.path.join(os.path.expanduser("~"), ".mcp_trash")
)
TRASH_DB = os.path.join(TRASH_ROOT, "trash_index.json")
MAX_TRASH_AGE_DAYS = int(os.environ.get("MCP_TRASH_MAX_AGE", "30"))
MAX_TRASH_SIZE_GB = int(os.environ.get("MCP_TRASH_MAX_SIZE_GB", "50"))

# ─── Trash Index ─────────────────────────────────────────────────────────────
def _ensure_trash():
    os.makedirs(TRASH_ROOT, exist_ok=True)
    if not os.path.exists(TRASH_DB):
        with open(TRASH_DB, 'w', encoding='utf-8') as f:
            json.dump({"entries": [], "version": "3.1"}, f)

def _load_index() -> Dict:
    _ensure_trash()
    try:
        with open(TRASH_DB, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return {"entries": [], "version": "3.1"}

def _save_index(data: Dict):
    tmp = TRASH_DB + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, TRASH_DB)

def _generate_trash_id() -> str:
    return f"t_{int(time.time() * 1000) & 0xFFFFFFFF:08x}"

def _get_trash_path(trash_id: str) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return Path(TRASH_ROOT) / today / trash_id

# ─── Move to Trash ───────────────────────────────────────────────────────────
def move_to_trash(path: str, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    p = Path(normalize_path(path))
    _ensure_allowed(p, "move_to_trash")
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    trash_id = _generate_trash_id()
    trash_path = _get_trash_path(trash_id)
    trash_path.parent.mkdir(parents=True, exist_ok=True)

    is_dir = p.is_dir()
    try:
        st = p.stat()
        original_size = st.st_size if not is_dir else _dir_size(p)
    except Exception:
        original_size = 0

    try:
        shutil.move(str(p), str(trash_path))
    except Exception as e:
        raise RuntimeError(f"Failed to move to trash: {e}")

    data = _load_index()
    entry = {
        "id": trash_id,
        "original_path": str(p),
        "trash_path": str(trash_path),
        "name": p.name,
        "is_directory": is_dir,
        "size": original_size,
        "size_human": _format_size(original_size),
        "deleted_at": datetime.now().isoformat(),
        "dialog": d_id,
        "expires_at": (datetime.now() + timedelta(days=MAX_TRASH_AGE_DAYS)).isoformat()
    }
    data["entries"].append(entry)
    _save_index(data)

    conversation_memory.add(
        op="move_to_trash",
        paths={"from": str(p), "to": str(trash_path)},
        status="trashed",
        dialog=d_id,
        context=f"Moved '{p.name}' ({entry['size_human']}) to trash, expires in {MAX_TRASH_AGE_DAYS} days"
    )
    return {
        "status": "trashed",
        "trash_id": trash_id,
        "original_path": str(p),
        "name": p.name,
        "size_human": entry["size_human"],
        "expires_at": entry["expires_at"]
    }

def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try: total += os.path.getsize(fp)
                except: pass
    except: pass
    return total

# ─── Restore from Trash ──────────────────────────────────────────────────────
def restore_from_trash(trash_id: str, destination: str = None) -> Dict:
    d_id = dialog_ctx.get()
    data = _load_index()
    entry = next((e for e in data["entries"] if e["id"] == trash_id), None)
    if not entry:
        raise ValueError(f"Trash entry not found: {trash_id}")

    trash_path = Path(entry["trash_path"])
    if not trash_path.exists():
        raise FileNotFoundError(f"Trash file missing: {trash_path}")

    if destination:
        dest = Path(normalize_path(destination))
    else:
        dest = Path(entry["original_path"])

    _ensure_allowed(dest.parent, "restore_from_trash")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        dest = dest.parent / f"{stem}_restored_{int(time.time())}{suffix}"

    shutil.move(str(trash_path), str(dest))

    data["entries"] = [e for e in data["entries"] if e["id"] != trash_id]
    _save_index(data)

    conversation_memory.add(
        op="restore_from_trash",
        paths={"from": str(trash_path), "to": str(dest)},
        status="restored",
        dialog=entry.get("dialog", "default"),
        context=f"Restored '{entry['name']}' to {dest}"
    )
    return {
        "status": "restored",
        "trash_id": trash_id,
        "restored_to": str(dest),
        "name": entry["name"]
    }

# ─── List Trash ──────────────────────────────────────────────────────────────
def list_trash(dialog: str = None, limit: int = 50) -> Dict:
    data = _load_index()
    entries = data.get("entries", [])
    if dialog:
        entries = [e for e in entries if e.get("dialog") == dialog]
    entries.sort(key=lambda x: x["deleted_at"], reverse=True)
    total_size = sum(e.get("size", 0) for e in entries)
    now = datetime.now()
    result = []
    for e in entries[:limit]:
        expires = datetime.fromisoformat(e["expires_at"])
        days_left = (expires - now).days
        result.append({
            "trash_id": e["id"], "name": e["name"],
            "original_path": e["original_path"],
            "size_human": e["size_human"], "deleted_at": e["deleted_at"],
            "days_left": max(0, days_left), "dialog": e.get("dialog", "default")
        })
    return {
        "entries": result, "total_count": len(data.get("entries", [])),
        "shown_count": len(result), "total_size_human": _format_size(total_size),
        "trash_root": TRASH_ROOT
    }

# ─── Empty Trash ─────────────────────────────────────────────────────────────
def empty_trash(older_than_days: int = None, max_size_gb: float = None, dry_run: bool = True) -> Dict:
    data = _load_index()
    entries = data.get("entries", [])
    now = datetime.now()
    current_total_size = sum(e.get("size", 0) for e in entries)
    max_size_bytes = (max_size_gb or MAX_TRASH_SIZE_GB) * 1024 * 1024 * 1024

    to_remove = []
    to_keep = []

    for e in entries:
        should_remove = False
        if older_than_days is not None:
            deleted = datetime.fromisoformat(e["deleted_at"])
            if (now - deleted).days >= older_than_days:
                should_remove = True
        else:
            expires = datetime.fromisoformat(e["expires_at"])
            if now >= expires:
                should_remove = True
        if not Path(e["trash_path"]).exists():
            should_remove = True

        if should_remove: to_remove.append(e)
        else: to_keep.append(e)

    to_keep.sort(key=lambda x: x["deleted_at"])
    remaining_size = current_total_size - sum(e.get("size", 0) for e in to_remove)
    if remaining_size > max_size_bytes:
        i = 0
        while remaining_size > max_size_bytes and i < len(to_keep):
            e = to_keep[i]
            to_remove.append(e)
            remaining_size -= e.get("size", 0)
            i += 1
        to_keep = to_keep[i:]

    removed_size = 0
    removed_count = 0
    errors = []

    if not dry_run:
        for e in to_remove:
            try:
                trash_path = Path(e["trash_path"])
                if trash_path.exists():
                    if trash_path.is_dir(): shutil.rmtree(trash_path)
                    else: trash_path.unlink()
                    removed_size += e.get("size", 0)
                    removed_count += 1
            except Exception as ex:
                errors.append(f"{e['id']}: {ex}")
        data["entries"] = to_keep
        _save_index(data)

    return {
        "status": "emptied" if not dry_run else "dry_run",
        "removed_count": removed_count if not dry_run else len(to_remove),
        "removed_size_human": _format_size(removed_size) if not dry_run else _format_size(sum(e.get("size", 0) for e in to_remove)),
        "remaining_count": len(to_keep),
        "remaining_size_human": _format_size(remaining_size) if not dry_run else _format_size(remaining_size),
        "dry_run": dry_run,
        "errors": errors if errors else None,
        "reason": "age_and_size_cleanup" if (older_than_days is not None and max_size_gb is not None) else ("size_cleanup" if max_size_gb is not None else "age_cleanup")
    }

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-trash", "3.1")
server.register_tool("move_to_trash", {
    "description": "Move file/directory to recoverable trash",
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "dialog_id": {"type": "string"}},
        "required": ["path"]
    }
}, lambda **kw: move_to_trash(kw["path"], kw.get("dialog_id")))

server.register_tool("restore_from_trash", {
    "description": "Restore file from trash to original or custom location",
    "inputSchema": {
        "type": "object",
        "properties": {"trash_id": {"type": "string"}, "destination": {"type": "string"}},
        "required": ["trash_id"]
    }
}, lambda **kw: restore_from_trash(kw["trash_id"], kw.get("destination")))

server.register_tool("list_trash", {
    "description": "List trash contents with expiration info",
    "inputSchema": {
        "type": "object",
        "properties": {"dialog": {"type": "string"}, "limit": {"type": "integer", "default": 50}}
    }
}, lambda **kw: list_trash(kw.get("dialog"), kw.get("limit", 50)))

server.register_tool("empty_trash", {
    "description": "Permanently delete expired, old, or oversized trash entries",
    "inputSchema": {
        "type": "object",
        "properties": {
            "older_than_days": {"type": "integer"},
            "max_size_gb": {"type": "number"},
            "dry_run": {"type": "boolean", "default": True}
        }
    }
}, lambda **kw: empty_trash(kw.get("older_than_days"), kw.get("max_size_gb"), kw.get("dry_run", True)))

if __name__ == "__main__":
    server.run()