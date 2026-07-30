#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Analyzer v2.0 (Context-Isolated)
Disk audit wrapper over mcp_fs_search with cleanup suggestions,
duplicate analysis, and temporary file detection.
Uses contextvars for secure dialog isolation.
"""
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from mcp_shared import (
    _log, _format_size, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)
from mcp_fs_search import analyze_directory, find_duplicates, search_files

# ─── Temp File Patterns ──────────────────────────────────────────────────────
_TEMP_PATTERNS = [
    "*.tmp", "*.temp", "*.bak", "*.swp", "*.swo", "*.log",
    "~$*", "*.~*", "Thumbs.db", "desktop.ini", "*.cache",
    "__pycache__", "*.pyc", ".DS_Store", "ehthumbs.db"
]

# ─── Suggest Cleanup ─────────────────────────────────────────────────────────
def suggest_cleanup(path: str, max_age_days: int = 30, 
                    include_temp: bool = True, include_empty: bool = True,
                    min_size_kb: int = 0) -> Dict:
    """
    Analyze directory and suggest files/folders for cleanup.
    Returns categorized recommendations with estimated space recovery.
    """
    d_id = dialog_ctx.get()
    p = Path(normalize_path(path))
    _ensure_allowed(p, "suggest_cleanup")
    
    if not p.exists() or not p.is_dir():
        return {"status": "error", "message": f"Invalid directory: {path}"}
    
    start = time.time()
    candidates = {
        "temp_files": [],
        "old_files": [],
        "empty_dirs": [],
        "large_files": [],
        "duplicates": []
    }
    total_recoverable = 0
    
    # 1. Temp files scan
    if include_temp:
        for pattern in _TEMP_PATTERNS:
            try:
                results = search_files(str(p), pattern, recursive=True, max_files=500)
                for item in results.get("items", []):
                    candidates["temp_files"].append({
                        "path": item["path"],
                        "size": item["size"],
                        "size_human": item["size_human"],
                        "reason": "temporary/cache file"
                    })
                    total_recoverable += item["size"]
            except Exception:
                pass
    
    # 2. Old files scan
    cutoff = datetime.now() - timedelta(days=max_age_days)
    try:
        for root, dirs, files in os.walk(str(p)):
            for f in files:
                fp = Path(root) / f
                try:
                    mtime = datetime.fromtimestamp(fp.stat().st_mtime)
                    if mtime < cutoff:
                        size = fp.stat().st_size
                        if size >= min_size_kb * 1024:
                            candidates["old_files"].append({
                                "path": str(fp),
                                "size": size,
                                "size_human": _format_size(size),
                                "modified": mtime.isoformat(),
                                "age_days": (datetime.now() - mtime).days,
                                "reason": f"not modified in {max_age_days}+ days"
                            })
                            total_recoverable += size
                except Exception:
                    pass
    except Exception as e:
        _log(f"Old files scan error: {e}")
    
    # 3. Empty directories
    if include_empty:
        try:
            for root, dirs, files in os.walk(str(p), topdown=False):
                for d in dirs:
                    dp = Path(root) / d
                    try:
                        if not any(dp.iterdir()):
                            candidates["empty_dirs"].append({
                                "path": str(dp),
                                "reason": "empty directory"
                            })
                    except Exception:
                        pass
        except Exception as e:
            _log(f"Empty dirs scan error: {e}")
    
    # 4. Large files (>100MB by default)
    try:
        large_threshold = 100 * 1024 * 1024
        for root, dirs, files in os.walk(str(p)):
            for f in files:
                fp = Path(root) / f
                try:
                    size = fp.stat().st_size
                    if size > large_threshold:
                        candidates["large_files"].append({
                            "path": str(fp),
                            "size": size,
                            "size_human": _format_size(size),
                            "reason": "large file (>100MB)"
                        })
                except Exception:
                    pass
    except Exception as e:
        _log(f"Large files scan error: {e}")
    
    # 5. Duplicates (by size first, then hash for same-size)
    try:
        dup_groups = find_duplicates(str(p), by="hash", min_size_kb=100, max_size_mb=10000)
        for group in dup_groups[:20]:  # Limit to top 20 groups
            if len(group) > 1:
                keep = group[0]
                for dup in group[1:]:
                    try:
                        size = Path(dup).stat().st_size
                        candidates["duplicates"].append({
                            "path": dup,
                            "size": size,
                            "size_human": _format_size(size),
                            "original": keep,
                            "reason": "duplicate file"
                        })
                        total_recoverable += size
                    except Exception:
                        pass
    except Exception as e:
        _log(f"Duplicates scan error: {e}")
    
    elapsed = time.time() - start
    
    # Summarize
    summary = {
        "temp_files": len(candidates["temp_files"]),
        "old_files": len(candidates["old_files"]),
        "empty_dirs": len(candidates["empty_dirs"]),
        "large_files": len(candidates["large_files"]),
        "duplicates": len(candidates["duplicates"]),
        "total_recoverable_bytes": total_recoverable,
        "total_recoverable_human": _format_size(total_recoverable)
    }
    
    conversation_memory.add(
        op="suggest_cleanup",
        paths={"path": str(p)},
        status="completed",
        dialog=d_id,
        context=f"Cleanup suggestions: {summary['temp_files']} temp, {summary['old_files']} old, {summary['duplicates']} duplicates"
    )
    
    return {
        "status": "success",
        "path": str(p),
        "elapsed_sec": round(elapsed, 2),
        "parameters": {
            "max_age_days": max_age_days,
            "include_temp": include_temp,
            "include_empty": include_empty,
            "min_size_kb": min_size_kb
        },
        "summary": summary,
        "recommendations": {k: v[:50] for k, v in candidates.items()}  # Limit output
    }

# ─── Quick Audit ─────────────────────────────────────────────────────────────
def quick_audit(path: str) -> Dict:
    """
    Fast directory health check: size, file count, extensions, recent activity.
    """
    d_id = dialog_ctx.get()
    p = Path(normalize_path(path))
    _ensure_allowed(p, "quick_audit")
    
    if not p.exists() or not p.is_dir():
        return {"status": "error", "message": f"Invalid directory: {path}"}
    
    start = time.time()
    stats = {
        "total_size": 0,
        "file_count": 0,
        "dir_count": 0,
        "extensions": {},
        "recent_files": [],
        "largest_files": []
    }
    
    cutoff_recent = datetime.now() - timedelta(days=7)
    
    try:
        for root, dirs, files in os.walk(str(p)):
            stats["dir_count"] += len(dirs)
            for f in files:
                fp = Path(root) / f
                try:
                    st = fp.stat()
                    stats["total_size"] += st.st_size
                    stats["file_count"] += 1
                    
                    ext = fp.suffix.lower() or "no_ext"
                    stats["extensions"][ext] = stats["extensions"].get(ext, 0) + 1
                    
                    mtime = datetime.fromtimestamp(st.st_mtime)
                    if mtime > cutoff_recent:
                        stats["recent_files"].append({
                            "path": str(fp),
                            "modified": mtime.isoformat(),
                            "size_human": _format_size(st.st_size)
                        })
                    
                    stats["largest_files"].append({
                        "path": str(fp),
                        "size": st.st_size,
                        "size_human": _format_size(st.st_size)
                    })
                except Exception:
                    pass
    except Exception as e:
        _log(f"Audit scan error: {e}")
    
    elapsed = time.time() - start
    
    # Sort and limit
    stats["recent_files"] = sorted(
        stats["recent_files"], key=lambda x: x["modified"], reverse=True
    )[:20]
    stats["largest_files"] = sorted(
        stats["largest_files"], key=lambda x: x["size"], reverse=True
    )[:20]
    stats["extensions"] = dict(sorted(
        stats["extensions"].items(), key=lambda x: x[1], reverse=True
    )[:15])
    
    conversation_memory.add(
        op="quick_audit",
        paths={"path": str(p)},
        status="completed",
        dialog=d_id,
        context=f"Audit: {stats['file_count']} files, {_format_size(stats['total_size'])}"
    )
    
    return {
        "status": "success",
        "path": str(p),
        "elapsed_sec": round(elapsed, 2),
        "stats": {
            "total_size": stats["total_size"],
            "total_size_human": _format_size(stats["total_size"]),
            "file_count": stats["file_count"],
            "dir_count": stats["dir_count"],
            "avg_file_size_human": _format_size(
                stats["total_size"] // max(stats["file_count"], 1)
            )
        },
        "top_extensions": stats["extensions"],
        "recent_activity": stats["recent_files"],
        "largest_files": stats["largest_files"]
    }

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-analyzer", "2.0")
server.register_tool("suggest_cleanup", {
    "description": "Analyze directory and suggest files/folders for cleanup",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_age_days": {"type": "integer", "default": 30},
            "include_temp": {"type": "boolean", "default": True},
            "include_empty": {"type": "boolean", "default": True},
            "min_size_kb": {"type": "integer", "default": 0}
        },
        "required": ["path"]
    }
}, lambda **kw: suggest_cleanup(
    kw["path"], kw.get("max_age_days", 30), kw.get("include_temp", True),
    kw.get("include_empty", True), kw.get("min_size_kb", 0)
))

server.register_tool("quick_audit", {
    "description": "Fast directory health check: size, count, extensions, recent activity",
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"]
    }
}, lambda **kw: quick_audit(kw["path"]))

if __name__ == "__main__":
    server.run()