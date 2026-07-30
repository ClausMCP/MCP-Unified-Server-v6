#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Cloud v2.1 (Context-Isolated)
Cloud storage abstraction via rclone with secure path handling,
sync operations, and conversation memory integration.
Uses contextvars for secure dialog isolation.
"""
import os
import sys
import json
import time
import subprocess
from pathlib import Path
from typing import List, Dict, Optional
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Configuration ──────────────────────────────────────────────────────────
RCLONE_PATH = os.environ.get("RCLONE_PATH", "rclone")
RCLONE_CONFIG = os.environ.get("RCLONE_CONFIG", "")
DEFAULT_REMOTE = os.environ.get("MCP_CLOUD_DEFAULT_REMOTE", "")

# ─── Helpers ────────────────────────────────────────────────────────────────
def _run_rclone(args: List[str], timeout: int = 300) -> Dict:
    """Execute rclone command with error handling."""
    cmd = [RCLONE_PATH] + args
    if RCLONE_CONFIG:
        cmd.extend(["--config", RCLONE_CONFIG])
    
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        return {
            "success": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {timeout}s"}
    except FileNotFoundError:
        return {"success": False, "error": f"rclone not found at '{RCLONE_PATH}'. Install rclone and add to PATH."}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _validate_remote(remote: str) -> bool:
    """Check if remote is configured in rclone."""
    result = _run_rclone(["listremotes", "--long"])
    if not result["success"]:
        return False
    remotes = [r.split(":")[0].strip() for r in result["stdout"].splitlines() if ":" in r]
    return remote in remotes

# ─── Core Operations ────────────────────────────────────────────────────────
def list_remotes() -> Dict:
    """List configured rclone remotes."""
    result = _run_rclone(["listremotes", "--long"])
    if not result["success"]:
        return {"status": "error", "message": result.get("error", result.get("stderr", "Unknown error"))}
    
    remotes = []
    for line in result["stdout"].splitlines():
        if ":" in line:
            name, desc = line.split(":", 1)
            remotes.append({"name": name.strip(), "type": desc.strip()})
    
    return {"status": "success", "remotes": remotes, "count": len(remotes)}

def sync_to_cloud(local_path: str, remote_path: str, remote_name: str = None,
                  dry_run: bool = False, delete_extras: bool = False) -> Dict:
    """Sync local directory to cloud remote."""
    d_id = dialog_ctx.get()
    local = Path(normalize_path(local_path))
    _ensure_allowed(local, "sync_to_cloud")
    
    if not local.exists() or not local.is_dir():
        return {"status": "error", "message": f"Local path not found or not a directory: {local_path}"}
    
    remote = remote_name or DEFAULT_REMOTE
    if not remote:
        return {"status": "error", "message": "No remote specified. Set MCP_CLOUD_DEFAULT_REMOTE or provide remote_name."}
    
    if not _validate_remote(remote):
        return {"status": "error", "message": f"Remote '{remote}' not configured in rclone."}
    
    full_remote = f"{remote}:{remote_path}"
    args = ["sync", str(local), full_remote, "-v"]
    
    if dry_run:
        args.append("--dry-run")
    if delete_extras:
        args.append("--delete-excluded")
    
    if dry_run:
        return {"status": "dry_run", "command": " ".join(args), "local": str(local), "remote": full_remote}
    
    start = time.time()
    result = _run_rclone(args, timeout=600)
    elapsed = time.time() - start
    
    if result["success"]:
        conversation_memory.add(
            op="sync_to_cloud",
            paths={"local": str(local), "remote": full_remote},
            status="synced", dialog=d_id,
            context=f"Synced {local.name} to {full_remote} in {elapsed:.1f}s"
        )
        return {
            "status": "success",
            "local": str(local),
            "remote": full_remote,
            "elapsed_sec": round(elapsed, 2),
            "output": result["stdout"][:500] if result.get("stdout") else ""
        }
    else:
        return {
            "status": "error",
            "message": result.get("error") or result.get("stderr", "Sync failed"),
            "command": " ".join(args)
        }

def sync_from_cloud(remote_path: str, local_path: str, remote_name: str = None,
                    dry_run: bool = False, delete_extras: bool = False) -> Dict:
    """Sync cloud remote to local directory."""
    d_id = dialog_ctx.get()
    local = Path(normalize_path(local_path))
    _ensure_allowed(local.parent if local.parent else local, "sync_from_cloud")
    
    remote = remote_name or DEFAULT_REMOTE
    if not remote:
        return {"status": "error", "message": "No remote specified."}
    
    if not _validate_remote(remote):
        return {"status": "error", "message": f"Remote '{remote}' not configured."}
    
    full_remote = f"{remote}:{remote_path}"
    args = ["sync", full_remote, str(local), "-v"]
    
    if dry_run:
        args.append("--dry-run")
    if delete_extras:
        args.append("--delete-excluded")
    
    if dry_run:
        return {"status": "dry_run", "command": " ".join(args), "remote": full_remote, "local": str(local)}
    
    start = time.time()
    result = _run_rclone(args, timeout=600)
    elapsed = time.time() - start
    
    if result["success"]:
        conversation_memory.add(
            op="sync_from_cloud",
            paths={"remote": full_remote, "local": str(local)},
            status="synced", dialog=d_id,
            context=f"Synced {full_remote} to {local.name} in {elapsed:.1f}s"
        )
        return {
            "status": "success",
            "remote": full_remote,
            "local": str(local),
            "elapsed_sec": round(elapsed, 2),
            "output": result["stdout"][:500] if result.get("stdout") else ""
        }
    else:
        return {
            "status": "error",
            "message": result.get("error") or result.get("stderr", "Sync failed"),
            "command": " ".join(args)
        }

def copy_to_cloud(local_path: str, remote_path: str, remote_name: str = None) -> Dict:
    """Copy file(s) to cloud without deleting extras (non-destructive)."""
    d_id = dialog_ctx.get()
    local = Path(normalize_path(local_path))
    _ensure_allowed(local, "copy_to_cloud")
    
    if not local.exists():
        return {"status": "error", "message": f"Source not found: {local_path}"}
    
    remote = remote_name or DEFAULT_REMOTE
    if not remote:
        return {"status": "error", "message": "No remote specified."}
    
    if not _validate_remote(remote):
        return {"status": "error", "message": f"Remote '{remote}' not configured."}
    
    full_remote = f"{remote}:{remote_path}"
    args = ["copy", str(local), full_remote, "-v"]
    
    start = time.time()
    result = _run_rclone(args, timeout=600)
    elapsed = time.time() - start
    
    if result["success"]:
        conversation_memory.add(
            op="copy_to_cloud",
            paths={"local": str(local), "remote": full_remote},
            status="copied", dialog=d_id,
            context=f"Copied {local.name} to {full_remote}"
        )
        return {
            "status": "success",
            "local": str(local),
            "remote": full_remote,
            "elapsed_sec": round(elapsed, 2)
        }
    else:
        return {
            "status": "error",
            "message": result.get("error") or result.get("stderr", "Copy failed")
        }

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-cloud", "2.1")
server.register_tool("list_remotes", {
    "description": "List configured rclone remotes",
    "inputSchema": {"type": "object", "properties": {}}
}, list_remotes)

server.register_tool("sync_to_cloud", {
    "description": "Sync local directory to cloud remote (mirror mode)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "local_path": {"type": "string"},
            "remote_path": {"type": "string"},
            "remote_name": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
            "delete_extras": {"type": "boolean", "default": False}
        },
        "required": ["local_path", "remote_path"]
    }
}, lambda **kw: sync_to_cloud(
    kw["local_path"], kw["remote_path"], kw.get("remote_name"),
    kw.get("dry_run", False), kw.get("delete_extras", False)
))

server.register_tool("sync_from_cloud", {
    "description": "Sync cloud remote to local directory (mirror mode)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "remote_path": {"type": "string"},
            "local_path": {"type": "string"},
            "remote_name": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
            "delete_extras": {"type": "boolean", "default": False}
        },
        "required": ["remote_path", "local_path"]
    }
}, lambda **kw: sync_from_cloud(
    kw["remote_path"], kw["local_path"], kw.get("remote_name"),
    kw.get("dry_run", False), kw.get("delete_extras", False)
))

server.register_tool("copy_to_cloud", {
    "description": "Copy file(s) to cloud without deleting extras (non-destructive)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "local_path": {"type": "string"},
            "remote_path": {"type": "string"},
            "remote_name": {"type": "string"}
        },
        "required": ["local_path", "remote_path"]
    }
}, lambda **kw: copy_to_cloud(
    kw["local_path"], kw["remote_path"], kw.get("remote_name")
))

if __name__ == "__main__":
    server.run()