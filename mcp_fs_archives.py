#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Archives v2.0 (Context-Isolated & Secure)
Secure ZIP/TAR creation and extraction with ZipSlip protection,
dry-run support, and conversation memory integration.
Uses contextvars for secure dialog isolation.
"""
import os
import sys
import time
import zipfile
import tarfile
from pathlib import Path
from typing import List, Dict, Optional
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Security: ZipSlip Prevention ───────────────────────────────────────────
def _is_safe_path(base_dir: Path, target_path: Path) -> bool:
    """Check if target_path is securely contained within base_dir."""
    try:
        real_base = base_dir.resolve()
        real_target = target_path.resolve()
        return str(real_target).startswith(str(real_base))
    except Exception:
        return False

# ─── Archive Creation ───────────────────────────────────────────────────────
def archive_files(files: List[str], output_path: str, format: str = "zip",
                  compression: int = zipfile.ZIP_DEFLATED, dry_run: bool = False) -> Dict:
    """Create an archive from a list of files/directories."""
    d_id = dialog_ctx.get()
    out = Path(normalize_path(output_path))
    _ensure_allowed(out.parent, "archive_files")

    resolved_files = []
    for f in files:
        p = Path(normalize_path(f))
        _ensure_allowed(p, "archive_files")
        if not p.exists():
            return {"status": "error", "message": f"Source not found: {f}"}
        resolved_files.append(p)

    if dry_run:
        return {
            "status": "dry_run",
            "format": format,
            "output": str(out),
            "files_count": len(resolved_files),
            "total_size_bytes": sum(p.stat().st_size for p in resolved_files if p.is_file())
        }

    start = time.time()
    count, errors = 0, []
    try:
        if format == "zip":
            with zipfile.ZipFile(str(out), 'w', compression) as zf:
                for p in resolved_files:
                    if p.is_file():
                        zf.write(p, arcname=p.name)
                        count += 1
                    elif p.is_dir():
                        for root, _, files_in_dir in os.walk(p):
                            for file_in_dir in files_in_dir:
                                full = Path(root) / file_in_dir
                                arcname = str(full.relative_to(p.parent))
                                zf.write(full, arcname=arcname)
                                count += 1
        elif format == "tar":
            mode = "w:gz" if out.name.endswith((".gz", ".tgz")) else "w:bz2" if out.name.endswith((".bz2", ".tbz2")) else "w"
            with tarfile.open(str(out), mode) as tf:
                for p in resolved_files:
                    tf.add(str(p), arcname=p.name)
                    count += 1
        else:
            return {"status": "error", "message": f"Unsupported format: {format}. Use 'zip' or 'tar'."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

    elapsed = time.time() - start
    conversation_memory.add(
        op="archive_files",
        paths={"output": str(out)},
        status="created", dialog=d_id,
        context=f"Archived {count} files to {out.name} ({format}) in {elapsed:.1f}s"
    )
    return {
        "status": "success",
        "format": format,
        "output": str(out),
        "files_archived": count,
        "elapsed_sec": round(elapsed, 2)
    }

# ─── Archive Extraction ─────────────────────────────────────────────────────
def extract_archive(archive_path: str, destination: str, dry_run: bool = False) -> Dict:
    """Securely extract a ZIP or TAR archive with ZipSlip protection."""
    d_id = dialog_ctx.get()
    src = Path(normalize_path(archive_path))
    dest = Path(normalize_path(destination))
    _ensure_allowed(src, "extract_archive")
    _ensure_allowed(dest, "extract_archive")

    if not src.is_file():
        return {"status": "error", "message": f"Archive not found: {archive_path}"}

    if dry_run:
        try:
            if src.name.endswith(".zip"):
                with zipfile.ZipFile(src) as zf:
                    count = len(zf.namelist())
            else:
                with tarfile.open(src, "r:*") as tf:
                    count = len(tf.getmembers())
        except Exception:
            count = 0
        return {"status": "dry_run", "archive": str(src), "destination": str(dest), "estimated_entries": count}

    start = time.time()
    extracted, skipped, errors = 0, 0, []
    dest.mkdir(parents=True, exist_ok=True)

    try:
        if src.name.endswith(".zip"):
            with zipfile.ZipFile(src) as zf:
                for member in zf.namelist():
                    target = dest / member
                    if not _is_safe_path(dest, target):
                        skipped += 1
                        errors.append(f"Skipped unsafe path (ZipSlip): {member}")
                        continue
                    if member.endswith('/'):
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as mf, open(target, 'wb') as tf:
                            tf.write(mf.read())
                        extracted += 1
        elif src.name.endswith((".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz")):
            with tarfile.open(src, "r:*") as tf:
                for member in tf.getmembers():
                    target = dest / member.name
                    if not _is_safe_path(dest, target):
                        skipped += 1
                        errors.append(f"Skipped unsafe path (TarSlip): {member.name}")
                        continue
                    tf.extract(member, dest)
                    extracted += 1
        else:
            return {"status": "error", "message": "Unsupported archive format"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

    elapsed = time.time() - start
    conversation_memory.add(
        op="extract_archive",
        paths={"archive": str(src), "destination": str(dest)},
        status="extracted", dialog=d_id,
        context=f"Extracted {extracted} files from {src.name}. Skipped {skipped} unsafe entries."
    )
    return {
        "status": "success",
        "archive": str(src),
        "destination": str(dest),
        "extracted": extracted,
        "skipped_unsafe": skipped,
        "errors": errors if errors else None,
        "elapsed_sec": round(elapsed, 2)
    }

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-archives", "2.0")
server.register_tool("archive_files", {
    "description": "Create ZIP or TAR archive from list of files/directories",
    "inputSchema": {
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": {"type": "string"}},
            "output_path": {"type": "string"},
            "format": {"type": "string", "enum": ["zip", "tar"], "default": "zip"},
            "dry_run": {"type": "boolean", "default": False}
        },
        "required": ["files", "output_path"]
    }
}, lambda **kw: archive_files(
    kw["files"], kw["output_path"], kw.get("format", "zip"), kw.get("dry_run", False)
))

server.register_tool("extract_archive", {
    "description": "Securely extract ZIP/TAR archive with ZipSlip protection",
    "inputSchema": {
        "type": "object",
        "properties": {
            "archive_path": {"type": "string"},
            "destination": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False}
        },
        "required": ["archive_path", "destination"]
    }
}, lambda **kw: extract_archive(
    kw["archive_path"], kw["destination"], kw.get("dry_run", False)
))

if __name__ == "__main__":
    server.run()