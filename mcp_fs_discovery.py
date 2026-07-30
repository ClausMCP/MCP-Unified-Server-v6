#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Discovery v3.1
Drive/path discovery delegated to shared core, secure metadata lookup,
and persistent context-aware logging.
"""
import os
import sys
import shutil
from pathlib import Path
from typing import Dict, List, Any
from datetime import datetime
from mcp_shared import (
    _log, _format_size, normalize_path, _ensure_allowed,
    get_allowed_paths, BaseMCPServer, conversation_memory, dialog_ctx
)

def get_available_drives() -> Dict[str, Any]:
    """Return cached drive lists with metadata, strictly based on allowed paths."""
    allowed_local, allowed_unc = get_allowed_paths()
    local_drives = []

    for drive_path in allowed_local:
        try:
            drive_str = str(drive_path)
            # Cross-platform disk usage (replaces os.statvfs)
            usage = shutil.disk_usage(drive_str)
            info: Dict[str, Any] = {
                "path": drive_str,
                "exists": drive_path.exists(),
                "readable": os.access(drive_str, os.R_OK) if drive_path.exists() else False,
                "total": usage.total,
                "total_human": _format_size(usage.total),
                "free": usage.free,
                "free_human": _format_size(usage.free),
                "used": usage.used,
                "used_human": _format_size(usage.used)
            }
            local_drives.append(info)
        except Exception as e:
            local_drives.append({"path": str(drive_path), "error": str(e)})

    result = {
        "local_drives": local_drives,
        "network_shares": allowed_unc,
        "total_local": len(local_drives),
        "total_network": len(allowed_unc)
    }

    # Context-aware logging
    dialog_id = dialog_ctx.get("default")
    conversation_memory.add(
        op="get_available_drives",
        paths={"local": len(local_drives), "unc": len(allowed_unc)},
        status="success",
        dialog=dialog_id,
        context=f"Queried drives. Found {len(local_drives)} local, {len(allowed_unc)} UNC."
    )
    return result

def verify_path(path: str) -> Dict[str, Any]:
    """Check path existence, accessibility and metadata with strict security checks."""
    p = Path(normalize_path(path))
    dialog_id = dialog_ctx.get("default")

    try:
        _ensure_allowed(p, "verify_path")
    except PermissionError as e:
        return {
            "path": str(p), "exists": False,
            "error": str(e), "allowed": False,
            "dialog": dialog_id
        }

    result: Dict[str, Any] = {
        "path": str(p),
        "exists": p.exists(),
        "allowed": True,
        "is_dir": None,
        "is_file": None,
        "is_symlink": False,
        "is_unc": str(p).startswith('\\\\'),
        "dialog": dialog_id
    }

    if result["exists"]:
        try:
            result["is_dir"] = p.is_dir()
            result["is_file"] = p.is_file()
            result["is_symlink"] = p.is_symlink()
            if result["is_file"]:
                st = p.stat()
                result["size"] = st.st_size
                result["size_human"] = _format_size(st.st_size)
                result["modified"] = datetime.fromtimestamp(st.st_mtime).isoformat()

            if result["is_dir"]:
                try:
                    usage = shutil.disk_usage(str(p))
                    result["space"] = {
                        "total": usage.total,
                        "total_human": _format_size(usage.total),
                        "free": usage.free,
                        "free_human": _format_size(usage.free)
                    }
                except Exception:
                    pass
        except Exception as e:
            result["error"] = f"Failed to get metadata: {e}"

    conversation_memory.add(
        op="verify_path",
        paths={"path": str(p)},
        status="success" if result["exists"] else "not_found",
        dialog=dialog_id,
        context=f"Verified path: {str(p)}"
    )
    return result

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-discovery", "3.1")
server.register_tool("get_available_drives", {
    "description": "List available local and network paths with space info",
    "inputSchema": {"type": "object", "properties": {}}
}, get_available_drives)

server.register_tool("verify_path", {
    "description": "Check path existence, accessibility and metadata",
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"]
    }
}, verify_path)

if __name__ == "__main__":
    server.run()