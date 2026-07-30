#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Versioning v1.2 (Context-Isolated & Stale-Lock Safe)
Simple file versioning system with automatic commits via watcher integration.
Stores versions in .versions/ subdirectory with timestamped names.
Includes robust file locking with PID/timestamp validation to prevent deadlocks.
Uses contextvars for secure dialog isolation.
"""
import os
import sys
import json
import shutil
import time
import hashlib
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

VERSIONS_DIR_NAME = ".versions"
LOCK_TIMEOUT_SECONDS = 10
LOCK_CHECK_INTERVAL = 0.5
LOCK_STALE_SEC = 300  # 5 minutes grace period for stale locks

# ─── Helper Functions ────────────────────────────────────────────────────────
def _get_versions_dir(file_path: Path) -> Path:
    return file_path.parent / VERSIONS_DIR_NAME

def _get_version_name(file_path: Path) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{file_path.stem}_{ts}{file_path.suffix}"

def _calculate_checksum(file_path: Path) -> str:
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception:
        return ""

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False

def _acquire_lock(file_path: Path) -> Optional[Path]:
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    start_time = time.time()
    while time.time() - start_time < LOCK_TIMEOUT_SECONDS:
        try:
            if lock_path.exists():
                is_stale = False
                try:
                    with open(lock_path, 'r') as f:
                        content = f.read().strip()
                    if ':' in content:
                        pid_str, ts_str = content.split(':', 1)
                        pid = int(pid_str)
                        lock_ts = float(ts_str)
                        # Check timestamp first (fastest)
                        if time.time() - lock_ts > LOCK_STALE_SEC:
                            is_stale = True
                        elif not _pid_alive(pid):
                            is_stale = True
                    else:
                        pid = int(content)
                        if not _pid_alive(pid):
                            is_stale = True
                except (ValueError, IOError):
                    is_stale = True

                if is_stale:
                    try:
                        lock_path.unlink()
                        _log(f"Removed stale lock: {lock_path}")
                        continue
                    except OSError:
                        pass
                time.sleep(LOCK_CHECK_INTERVAL)
                continue

            # Create fresh lock
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            lock_content = f"{os.getpid()}:{time.time()}"
            os.write(fd, lock_content.encode())
            os.close(fd)
            return lock_path
        except FileExistsError:
            time.sleep(LOCK_CHECK_INTERVAL)
        except Exception as e:
            _log(f"Lock acquisition error: {e}")
            return None
    return None

def _release_lock(lock_path: Path):
    try:
        if lock_path and lock_path.exists():
            lock_path.unlink()
    except Exception as e:
        _log(f"Failed to release lock {lock_path}: {e}")

# ─── Core Operations ─────────────────────────────────────────────────────────
def commit_version(file_path: str, comment: str = "") -> Dict:
    fp = Path(normalize_path(file_path))
    _ensure_allowed(fp, "commit_version")
    if not fp.exists() or not fp.is_file():
        return {"status": "error", "message": f"File not found: {file_path}"}
    
    versions_dir = _get_versions_dir(fp)
    versions_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'win32':
        try:
            import subprocess
            subprocess.run(["attrib", "+h", str(versions_dir)], capture_output=True, check=False)
        except Exception:
            pass

    version_name = _get_version_name(fp)
    dest_path = versions_dir / version_name
    try:
        shutil.copy2(str(fp), str(dest_path))
        checksum = _calculate_checksum(fp)
        meta = {
            "original_path": str(fp),
            "version_file": str(dest_path),
            "timestamp": datetime.now().isoformat(),
            "checksum": checksum,
            "size": fp.stat().st_size,
            "comment": comment
        }
        meta_path = dest_path.with_suffix(dest_path.suffix + ".meta.json")
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2, default=str)
        
        conversation_memory.add(
            op="commit_version",
            paths={"file": str(fp), "version": str(dest_path)},
            status="committed",
            dialog=dialog_ctx.get(),
            context=f"Version created: {version_name}. Comment: {comment}"
        )
        return {
            "status": "success",
            "version_file": str(dest_path),
            "timestamp": meta["timestamp"],
            "checksum": checksum
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

def list_versions(file_path: str) -> Dict:
    fp = Path(normalize_path(file_path))
    _ensure_allowed(fp, "list_versions")
    if not fp.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}
    
    versions_dir = _get_versions_dir(fp)
    if not versions_dir.exists():
        return {"status": "success", "versions": [], "count": 0}
    
    versions = []
    target_stem = fp.stem
    for v_file in versions_dir.iterdir():
        if v_file.is_file() and v_file.name.startswith(target_stem) and not v_file.name.endswith(".meta.json"):
            meta_path = v_file.with_suffix(v_file.suffix + ".meta.json")
            meta = {}
            if meta_path.exists():
                try:
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                except Exception:
                    pass
            versions.append({
                "version_file": str(v_file),
                "timestamp": meta.get("timestamp", "unknown"),
                "size": v_file.stat().st_size,
                "checksum": meta.get("checksum", ""),
                "comment": meta.get("comment", "")
            })
    versions.sort(key=lambda x: x["timestamp"], reverse=True)
    return {
        "status": "success",
        "original_file": str(fp),
        "versions": versions,
        "count": len(versions)
    }

def restore_version(version_file: str, target_path: str = None) -> Dict:
    vf = Path(normalize_path(version_file))
    _ensure_allowed(vf, "restore_version")
    if not vf.exists():
        return {"status": "error", "message": f"Version file not found: {version_file}"}
    
    if not target_path:
        meta_path = vf.with_suffix(vf.suffix + ".meta.json")
        if meta_path.exists():
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                target_path = meta.get("original_path")
            except Exception:
                pass
        if not target_path:
            return {"status": "error", "message": "Target path not specified and could not be determined from metadata"}
    
    tp = Path(normalize_path(target_path))
    _ensure_allowed(tp, "restore_version")
    
    lock_path = _acquire_lock(tp)
    if not lock_path:
        return {"status": "error", "message": f"Could not acquire lock for {tp}. File may be in use."}
    
    try:
        if tp.exists():
            commit_version(str(tp), comment="Auto-backup before restore")
        shutil.copy2(str(vf), str(tp))
        conversation_memory.add(
            op="restore_version",
            paths={"version": str(vf), "target": str(tp)},
            status="restored",
            dialog=dialog_ctx.get(),
            context=f"Restored version {vf.name} to {tp.name}"
        )
        return {
            "status": "success",
            "restored_to": str(tp),
            "from_version": str(vf)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        _release_lock(lock_path)

def diff_versions(version_file_1: str, version_file_2: str) -> Dict:
    v1 = Path(normalize_path(version_file_1))
    v2 = Path(normalize_path(version_file_2))
    if not v1.exists() or not v2.exists():
        return {"status": "error", "message": "One or both version files not found"}
    s1, s2 = v1.stat().st_size, v2.stat().st_size
    c1, c2 = _calculate_checksum(v1), _calculate_checksum(v2)
    return {
        "status": "success",
        "identical": c1 == c2,
        "v1": {"path": str(v1), "size": s1, "checksum": c1},
        "v2": {"path": str(v2), "size": s2, "checksum": c2}
    }

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-versioning", "1.2")
server.register_tool("commit_version", {
    "description": "Save a current version of a file with an optional comment",
    "inputSchema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "comment": {"type": "string", "default": ""}
        },
        "required": ["file_path"]
    }
}, lambda **kw: commit_version(kw["file_path"], kw.get("comment", "")))

server.register_tool("list_versions", {
    "description": "List all saved versions for a specific file",
    "inputSchema": {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"]
    }
}, lambda **kw: list_versions(kw["file_path"]))

server.register_tool("restore_version", {
    "description": "Restore a file from a specific version snapshot. Locks file to prevent corruption.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "version_file": {"type": "string"},
            "target_path": {"type": "string", "description": "Optional. Defaults to original path"}
        },
        "required": ["version_file"]
    }
}, lambda **kw: restore_version(kw["version_file"], kw.get("target_path")))

server.register_tool("diff_versions", {
    "description": "Compare two version files (checksum and size)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "version_file_1": {"type": "string"},
            "version_file_2": {"type": "string"}
        },
        "required": ["version_file_1", "version_file_2"]
    }
}, lambda **kw: diff_versions(kw["version_file_1"], kw["version_file_2"]))

if __name__ == "__main__":
    server.run()