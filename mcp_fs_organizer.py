#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Organizer v2.0 (Context-Isolated)
Smart file organization based on heuristics, metadata, and content analysis.
Uses contextvars for secure dialog isolation and integrates with conversation memory.
"""
import os
import sys
import json
import shutil
import time
import re
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Heuristics & Metadata Extraction ────────────────────────────────────────
def _get_file_category(file_path: Path) -> Dict:
    """Analyze file to determine category and suggested destination."""
    ext = file_path.suffix.lower()
    stat = file_path.stat()
    result = {
        "category": "unknown",
        "suggested_folder": "Unsorted",
        "confidence": 0.5,
        "metadata": {}
    }

    # Extension-based rules
    ext_map = {
        ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff'): ("images", "Images", 0.9),
        ('.mp3', '.wav', '.flac', '.aac', '.ogg'): ("audio", "Music", 0.9),
        ('.mp4', '.avi', '.mkv', '.mov', '.wmv'): ("video", "Videos", 0.9),
        ('.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt'): ("documents", "Documents", 0.8),
        ('.exe', '.msi', '.deb', '.rpm', '.dmg'): ("installers", "Installers", 0.95),
        ('.zip', '.rar', '.7z', '.tar', '.gz'): ("archives", "Archives", 0.95),
    }

    for exts, (cat, folder, conf) in ext_map.items():
        if ext in exts:
            result.update(category=cat, suggested_folder=folder, confidence=conf)
            if cat == "images":
                date = datetime.fromtimestamp(stat.st_mtime)
                result["suggested_folder"] = f"{folder}/{date.strftime('%Y')}/{date.strftime('%m')}"
            break

    # Content-based heuristics for text/code files
    if ext in {'.txt', '.md', '.py', '.js', '.json', '.xml', '.csv', '.log'}:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(1024)
            if any(kw in content for kw in ("def ", "import ", "function ", "class ")):
                result.update(category="code", suggested_folder="Code", confidence=0.85)
            if any(kw in content.lower() for kw in ("invoice", "receipt", "contract", "payment")):
                result.update(category="finance", suggested_folder="Documents/Finance", confidence=0.75)
        except Exception:
            pass

    return result

def _generate_filename(file_path: Path, category: str, metadata: Dict) -> str:
    """Generate a standardized filename based on category and metadata."""
    # Sanitize stem: remove brackets, extra spaces/symbols
    stem = re.sub(r'[\[\]\(\)\{\}\s]+', '_', file_path.stem).strip('_')
    ext = file_path.suffix
    
    if category == "images":
        date_str = datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d")
        return f"{date_str}_{stem}{ext}"
    return f"{stem}{ext}"

# ─── Core Operations ─────────────────────────────────────────────────────────
def analyze_and_plan(source_path: str, target_root: str, dry_run: bool = True) -> Dict:
    """Scan source directory and propose a reorganization plan."""
    d_id = dialog_ctx.get()
    src = Path(normalize_path(source_path))
    tgt = Path(normalize_path(target_root))
    
    _ensure_allowed(src, "analyze_and_plan")
    _ensure_allowed(tgt, "analyze_and_plan")
    
    if not src.exists() or not src.is_dir():
        return {"status": "error", "message": "Invalid source directory"}

    plan = []
    stats = {"total": 0, "categorized": 0, "uncategorized": 0}

    for entry in src.iterdir():
        if not entry.is_file():
            continue
        stats["total"] += 1
        info = _get_file_category(entry)
        new_folder = tgt / info["suggested_folder"]
        new_name = _generate_filename(entry, info["category"], info["metadata"])
        dest_path = new_folder / new_name

        # Avoid overwriting existing files
        if dest_path.exists():
            counter = 1
            while dest_path.exists():
                dest_path = new_folder / f"{entry.stem}_{counter}{entry.suffix}"
                counter += 1

        plan.append({
            "source": str(entry),
            "destination": str(dest_path),
            "category": info["category"],
            "confidence": info["confidence"],
            "action": "move"
        })
        if info["category"] != "unknown":
            stats["categorized"] += 1
        else:
            stats["uncategorized"] += 1

    conversation_memory.add(
        op="analyze_and_plan",
        paths={"source": str(src), "target": str(tgt)},
        status="planned",
        dialog=d_id,
        context=f"Planned {len(plan)} moves for {src.name}"
    )
    return {
        "status": "success",
        "dry_run": dry_run,
        "plan": plan,
        "stats": stats,
        "message": f"Planned {len(plan)} moves. {stats['categorized']} categorized."
    }

def execute_organization(plan: List[Dict]) -> Dict:
    """Execute the organization plan."""
    d_id = dialog_ctx.get()
    results = {"success": [], "errors": []}
    
    for item in plan:
        src = Path(item["source"])
        dst = Path(item["destination"])
        try:
            _ensure_allowed(src, "execute_organization")
            _ensure_allowed(dst, "execute_organization")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            results["success"].append({
                "source": item["source"],
                "destination": item["destination"]
            })
        except Exception as e:
            results["errors"].append({
                "source": item["source"],
                "error": str(e)
            })

    conversation_memory.add(
        op="execute_organization",
        paths={"success_count": len(results["success"]), "error_count": len(results["errors"])},
        status="completed",
        dialog=d_id,
        context=f"Organized {len(results['success'])} files. {len(results['errors'])} errors."
    )
    return results

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-organizer", "2.0")
server.register_tool("analyze_and_plan", {
    "description": "Analyze files in a directory and propose a reorganization plan based on content and type",
    "inputSchema": {
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "target_root": {"type": "string"},
            "dry_run": {"type": "boolean", "default": True}
        },
        "required": ["source_path", "target_root"]
    }
}, lambda **kw: analyze_and_plan(
    kw["source_path"], kw["target_root"], kw.get("dry_run", True)
))

server.register_tool("execute_organization", {
    "description": "Execute a previously generated organization plan",
    "inputSchema": {
        "type": "object",
        "properties": {
            "plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "destination": {"type": "string"}
                    }
                }
            }
        },
        "required": ["plan"]
    }
}, lambda **kw: execute_organization(kw["plan"]))

if __name__ == "__main__":
    server.run()