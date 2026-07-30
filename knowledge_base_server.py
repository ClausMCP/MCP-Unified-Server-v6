#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Knowledge Base v3.6 – исправлена статистика, проверка FTS через SQL, оптимизация.
"""
import os
import sys
import json
import sqlite3
import threading
import time
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from mcp_shared import (
    _log, normalize_path, BaseMCPServer, conversation_memory
)

DB_PATH = os.environ.get("KB_DB_PATH", os.path.join(os.path.dirname(__file__), "knowledge.db"))
VERSIONS_DIR = os.environ.get("KB_VERSIONS_DIR", os.path.join(os.path.dirname(DB_PATH), "kb_versions"))
EVENT_BUS_URL = os.environ.get("EVENT_BUS_URL", "")  # Optional event bus integration
AUTO_VERSION = os.environ.get("KB_AUTO_VERSION", "true").lower() == "true"
MAX_BACKUPS = int(os.environ.get("KB_MAX_BACKUPS", "10"))

# ─── Event Bus Integration (optional) ────────────────────────────────────────
def _publish_event(topic: str, payload: Dict):
    """Publish knowledge base events to MCP Event Bus (non-blocking)."""
    if not EVENT_BUS_URL:
        return
    try:
        import requests
        # Fire and forget with short timeout
        requests.post(EVENT_BUS_URL, json={"topic": topic, "payload": payload}, timeout=0.5)
    except ImportError:
        pass  # requests not installed
    except Exception:
        pass  # Silent fail, don't block KB operations

# ─── Connection Pool (improved with context manager) ─────────────────────────
_pool_lock = threading.Lock()
_pool_conn = None
_pool_refcount = 0

class DatabaseError(Exception):
    """Custom database exception."""
    pass

def _get_db():
    """Get or create SQLite connection with WAL mode and proper pragmas."""
    global _pool_conn, _pool_refcount
    with _pool_lock:
        if _pool_conn is None:
            os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)) or ".", exist_ok=True)
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            # Performance pragmas
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=268435456")  # 256MB mmap
            conn.execute("PRAGMA foreign_keys=ON")

            # Create tables with category support
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT UNIQUE NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tags TEXT DEFAULT '',
                    category TEXT DEFAULT 'general',
                    created TEXT DEFAULT CURRENT_TIMESTAMP,
                    modified TEXT DEFAULT CURRENT_TIMESTAMP,
                    version INTEGER DEFAULT 1
                )
            """)
            
            # Enhanced indices
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_title ON notes(title)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_tags ON notes(tags)")

            # Try FTS5 for full-text search
            fts_ok = False
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                        title, content, tags, category,
                        content_rowid=rowid,
                        prefix='2 3 4'
                    )
                """)
                # Drop old triggers if they exist
                conn.execute("DROP TRIGGER IF EXISTS notes_ai")
                conn.execute("DROP TRIGGER IF EXISTS notes_ad")
                conn.execute("DROP TRIGGER IF EXISTS notes_au")
                
                # Enhanced triggers to keep FTS in sync
                conn.execute("""
                    CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
                        INSERT INTO notes_fts(rowid, title, content, tags, category)
                        VALUES (new.id, new.title, new.content, new.tags, new.category);
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
                        INSERT INTO notes_fts(notes_fts, rowid, title, content, tags, category)
                        VALUES ('delete', old.id, old.title, old.content, old.tags, old.category);
                    END
                """)
                conn.execute("""
                    CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
                        INSERT INTO notes_fts(notes_fts, rowid, title, content, tags, category)
                        VALUES ('delete', old.id, old.title, old.content, old.tags, old.category);
                        INSERT INTO notes_fts(rowid, title, content, tags, category)
                        VALUES (new.id, new.title, new.content, new.tags, new.category);
                    END
                """)
                fts_ok = True
                _log("FTS5 enabled for knowledge base")
            except sqlite3.OperationalError as e:
                _log(f"FTS5 not available: {e}")
            
            # Сохраняем флаг в специальной таблице метаданных
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("INSERT OR REPLACE INTO kb_metadata (key, value) VALUES ('fts_enabled', ?)", 
                         ('1' if fts_ok else '0'))
            conn.commit()
            _pool_conn = conn
        _pool_refcount += 1
        return _pool_conn

def _close_db():
    """Close database connection when refcount reaches zero."""
    global _pool_conn, _pool_refcount
    with _pool_lock:
        _pool_refcount = max(0, _pool_refcount - 1)
        if _pool_refcount == 0 and _pool_conn:
            _pool_conn.close()
            _pool_conn = None

# ─── Version Management ──────────────────────────────────────────────────────
def _save_version(note_id: int, title: str, content: str, tags: str, category: str):
    """Save a version snapshot of a note."""
    if not AUTO_VERSION:
        return
        
    os.makedirs(VERSIONS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    version_file = os.path.join(VERSIONS_DIR, f"{note_id}_{timestamp}.json")
    
    version_data = {
        "note_id": note_id,
        "title": title,
        "content": content,
        "tags": tags,
        "category": category,
        "saved_at": datetime.now().isoformat()
    }
    
    try:
        with open(version_file, 'w', encoding='utf-8') as f:
            json.dump(version_data, f, ensure_ascii=False, indent=2)
        
        # Cleanup old versions (keep only MAX_BACKUPS)
        versions = sorted(Path(VERSIONS_DIR).glob(f"{note_id}_*.json"))
        for old_version in versions[:-MAX_BACKUPS]:
            old_version.unlink()
    except Exception as e:
        _log(f"Failed to save version for note {note_id}: {e}")

def list_versions(note_id: int) -> List[Dict]:
    """List all versions of a note."""
    versions = []
    if not os.path.exists(VERSIONS_DIR):
        return versions
        
    pattern = f"{note_id}_*.json"
    for vfile in sorted(Path(VERSIONS_DIR).glob(pattern)):
        try:
            with open(vfile, 'r', encoding='utf-8') as f:
                data = json.load(f)
                versions.append({
                    "timestamp": data.get("saved_at"),
                    "file": str(vfile),
                    "size": vfile.stat().st_size,
                    "title_preview": data.get("title", "")[:50]
                })
        except Exception:
            continue
            
    versions.sort(key=lambda x: x["timestamp"], reverse=True)
    return versions

def restore_version(note_id: int, timestamp: str = None) -> Dict:
    """Restore a note from a specific version."""
    versions = list_versions(note_id)
    if not versions:
        return {"status": "error", "message": "No versions found for this note"}
        
    # If timestamp not provided, use latest
    target = timestamp or versions[0]["timestamp"]
    
    # Find version file
    version_file = None
    for v in versions:
        if v["timestamp"] == target:
            version_file = v["file"]
            break
            
    if not version_file:
        return {"status": "error", "message": f"Version {target} not found"}
        
    try:
        with open(version_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        conn = _get_db()
        # Update current note
        conn.execute("""
            UPDATE notes 
            SET title = ?, content = ?, tags = ?, category = ?, 
                modified = ?, version = version + 1
            WHERE id = ?
        """, (data["title"], data["content"], data.get("tags", ""), 
              data.get("category", "general"), datetime.now().isoformat(), note_id))
        conn.commit()
        
        _publish_event("knowledge.note.restored", {
            "note_id": note_id,
            "title": data["title"],
            "restored_from": target
        })
        
        conversation_memory.add(
            op="restore_version",
            paths={"note_id": note_id},
            status="restored",
            context=f"Restored note '{data['title']}' from version {target}"
        )
        
        return {
            "status": "success",
            "note_id": note_id,
            "restored_from": target,
            "title": data["title"]
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ─── Store Note (enhanced with category and versioning) ──────────────────────
def store_note(title: str, content: str, tags: str = "", category: str = "general") -> Dict:
    """Save or update a note with automatic versioning and event publishing."""
    conn = _get_db()
    try:
        now = datetime.now().isoformat()
        
        # Check if note exists to save version
        cur = conn.execute("SELECT id, version FROM notes WHERE title = ?", (title,))
        existing = cur.fetchone()
        
        if existing:
            # Save version before update
            old_data = conn.execute(
                "SELECT title, content, tags, category FROM notes WHERE id = ?", 
                (existing["id"],)
            ).fetchone()
            _save_version(existing["id"], old_data["title"], old_data["content"], 
                         old_data["tags"], old_data["category"])
            
            # Update existing note
            conn.execute("""
                UPDATE notes 
                SET content = ?, tags = ?, category = ?, 
                    modified = ?, version = version + 1
                WHERE title = ?
            """, (content, tags, category, now, title))
            action = "updated"
            note_id = existing["id"]
        else:
            # Insert new note
            conn.execute("""
                INSERT INTO notes (title, content, tags, category, created, modified)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (title, content, tags, category, now, now))
            note_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            action = "created"
            
        conn.commit()

        # Publish event
        _publish_event(f"knowledge.note.{action}", {
            "note_id": note_id,
            "title": title,
            "category": category,
            "tags": tags.split(',') if tags else []
        })

        conversation_memory.add(
            op="store_note",
            paths={"title": title},
            status=action,
            context=f"Note {action}: '{title}' ({len(content)} chars, category: {category})"
        )
        
        return {
            "status": "success",
            "action": action,
            "note_id": note_id,
            "title": title,
            "timestamp": now
        }
    except sqlite3.IntegrityError:
        return {"status": "error", "message": f"Note with title '{title}' already exists (use update?)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        _close_db()

# ─── Search Notes (enhanced with filters) ────────────────────────────────────
def search_notes(query: str, limit: int = 50, category: str = None, 
                 tags: List[str] = None, offset: int = 0) -> Dict:
    """Enhanced search with category/tag filters and pagination."""
    conn = _get_db()
    results = []
    
    try:
        # Проверяем, включён ли FTS
        has_fts = False
        try:
            row = conn.execute("SELECT value FROM kb_metadata WHERE key = 'fts_enabled'").fetchone()
            if row and row["value"] == "1":
                has_fts = True
        except Exception:
            pass

        if has_fts and query and query.strip():
            # Build FTS5 query with filters
            sql = """
                SELECT n.id, n.title, n.content, n.tags, n.category, n.created, n.modified,
                       rank AS score
                FROM notes_fts
                JOIN notes n ON notes_fts.rowid = n.id
                WHERE notes_fts MATCH ?
            """
            params = [query]
            
            if category:
                sql += " AND n.category = ?"
                params.append(category)
                
            if tags:
                tag_conditions = " AND (" + " OR ".join([" n.tags LIKE ?" for _ in tags]) + ")"
                sql += tag_conditions
                params.extend([f"%{tag}%" for tag in tags])
                
            sql += " ORDER BY rank LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            rows = conn.execute(sql, params).fetchall()
            
            for r in rows:
                results.append({
                    "id": r["id"],
                    "title": r["title"],
                    "content_preview": r["content"][:300] + "..." if len(r["content"]) > 300 else r["content"],
                    "tags": r["tags"].split(',') if r["tags"] else [],
                    "category": r["category"],
                    "created": r["created"],
                    "modified": r["modified"],
                    "relevance_score": round(r["score"], 4) if r["score"] else None,
                    "source": "fts5"
                })
        else:
            # Fallback to LIKE search
            sql = """
                SELECT id, title, content, tags, category, created, modified
                FROM notes
                WHERE 1=1
            """
            params = []
            
            if query:
                sql += " AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)"
                pattern = f"%{query}%"
                params.extend([pattern, pattern, pattern])
                
            if category:
                sql += " AND category = ?"
                params.append(category)
                
            if tags:
                tag_conditions = " AND (" + " OR ".join([" tags LIKE ?" for _ in tags]) + ")"
                sql += tag_conditions
                params.extend([f"%{tag}%" for tag in tags])
                
            sql += " ORDER BY modified DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            rows = conn.execute(sql, params).fetchall()
            
            for r in rows:
                results.append({
                    "id": r["id"],
                    "title": r["title"],
                    "content_preview": r["content"][:300],
                    "tags": r["tags"].split(',') if r["tags"] else [],
                    "category": r["category"],
                    "created": r["created"],
                    "modified": r["modified"],
                    "source": "like"
                })
                
        # Get total count
        count_sql = "SELECT COUNT(*) FROM notes WHERE 1=1"
        count_params = []
        if query:
            count_sql += " AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)"
            pattern = f"%{query}%"
            count_params.extend([pattern, pattern, pattern])
        if category:
            count_sql += " AND category = ?"
            count_params.append(category)
            
        total = conn.execute(count_sql, count_params).fetchone()[0] if count_params else conn.execute(count_sql).fetchone()[0]
        
        return {
            "status": "success",
            "query": query,
            "results": results,
            "count": len(results),
            "total": total,
            "has_more": offset + limit < total,
            "offset": offset,
            "limit": limit
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "results": []}
    finally:
        _close_db()

# ─── Legacy search function (for backward compatibility) ─────────────────────
def search_notes_legacy(query: str, limit: int = 50) -> List[Dict]:
    """Legacy search function returning list (backward compatibility)."""
    result = search_notes(query, limit)
    return result.get("results", [])

# ─── Delete Note (enhanced with events) ──────────────────────────────────────
def delete_note(title: str = None, note_id: int = None) -> Dict:
    """Delete a note by title or ID with event publishing."""
    conn = _get_db()
    try:
        if note_id:
            # Get title for event
            row = conn.execute("SELECT title FROM notes WHERE id = ?", (note_id,)).fetchone()
            if not row:
                return {"status": "error", "message": f"Note with ID {note_id} not found"}
            title = row["title"]
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            deleted = 1
        elif title:
            cur = conn.execute("DELETE FROM notes WHERE title = ?", (title,))
            deleted = cur.rowcount
        else:
            return {"status": "error", "message": "Either title or note_id required"}
            
        conn.commit()
        
        if deleted > 0:
            # Publish event
            _publish_event("knowledge.note.deleted", {
                "note_id": note_id,
                "title": title
            })
            
            conversation_memory.add(
                op="delete_note",
                paths={"title": title},
                status="deleted",
                context=f"Note '{title}' deleted"
            )
        
        return {
            "status": "deleted" if deleted > 0 else "not_found",
            "title": title,
            "note_id": note_id,
            "rows_affected": deleted
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        _close_db()

# ─── Get Note (enhanced with ID lookup) ──────────────────────────────────────
def get_note(title: str = None, note_id: int = None) -> Dict:
    """Get full note by title or ID."""
    conn = _get_db()
    try:
        if note_id:
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
        elif title:
            row = conn.execute(
                "SELECT * FROM notes WHERE title = ?", (title,)
            ).fetchone()
        else:
            return {"error": "Either title or note_id required"}
            
        if row:
            return {
                "found": True,
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "tags": row["tags"].split(',') if row["tags"] else [],
                "category": row["category"],
                "created": row["created"],
                "modified": row["modified"],
                "version": row["version"]
            }
        return {"found": False, "title": title, "note_id": note_id}
    except Exception as e:
        return {"error": str(e)}
    finally:
        _close_db()

# ─── Categories and Statistics (исправлено) ─────────────────────────────────
def list_categories() -> Dict:
    """List all categories with note counts."""
    conn = _get_db()
    try:
        rows = conn.execute("""
            SELECT category, COUNT(*) as count 
            FROM notes 
            GROUP BY category 
            ORDER BY count DESC
        """).fetchall()
        
        categories = [{"name": r["category"], "count": r["count"]} for r in rows]
        
        return {
            "status": "success",
            "categories": categories,
            "total": len(categories)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        _close_db()

def get_stats() -> Dict:
    """Get knowledge base statistics (исправлено: проверка FTS через метаданные)."""
    conn = _get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        total_size = conn.execute("SELECT SUM(LENGTH(content)) FROM notes").fetchone()[0] or 0
        
        # Count versions
        version_count = 0
        if os.path.exists(VERSIONS_DIR):
            version_count = len(list(Path(VERSIONS_DIR).glob("*.json")))
            
        # Category stats
        category_stats = conn.execute("""
            SELECT category, COUNT(*) as count 
            FROM notes 
            GROUP BY category
        """).fetchall()
        
        # DB file size
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        
        # Проверяем FTS через метаданные
        fts_enabled = False
        try:
            row = conn.execute("SELECT value FROM kb_metadata WHERE key = 'fts_enabled'").fetchone()
            if row and row["value"] == "1":
                fts_enabled = True
        except Exception:
            pass
        
        return {
            "total_notes": total,
            "total_chars": total_size,
            "total_versions": version_count,
            "categories": [{"name": r["category"], "count": r["count"]} for r in category_stats],
            "db_size_bytes": db_size,
            "db_size_human": f"{db_size / 1024:.1f} KB" if db_size < 1024*1024 else f"{db_size / (1024*1024):.1f} MB",
            "versioning_enabled": AUTO_VERSION,
            "fts_enabled": fts_enabled
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        _close_db()

def export_knowledge_base(format: str = "json", output_path: str = None) -> Dict:
    """Export all notes to JSON or Markdown."""
    conn = _get_db()
    try:
        rows = conn.execute("SELECT * FROM notes ORDER BY title").fetchall()
        notes = []
        
        for r in rows:
            notes.append({
                "id": r["id"],
                "title": r["title"],
                "content": r["content"],
                "tags": r["tags"].split(',') if r["tags"] else [],
                "category": r["category"],
                "created": r["created"],
                "modified": r["modified"],
                "version": r["version"]
            })
            
        if format == "json":
            if output_path:
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(notes, f, ensure_ascii=False, indent=2)
                return {"status": "success", "exported_to": output_path, "count": len(notes)}
            else:
                return {"status": "success", "data": notes, "count": len(notes)}
                
        elif format == "markdown":
            md_lines = ["# Knowledge Base Export\n", f"Generated: {datetime.now().isoformat()}\n", f"Total notes: {len(notes)}\n\n"]
            
            for note in notes:
                md_lines.append(f"## {note['title']}\n")
                md_lines.append(f"**Category:** {note['category']}  \n")
                if note['tags']:
                    md_lines.append(f"**Tags:** {', '.join(note['tags'])}  \n")
                md_lines.append(f"**Created:** {note['created']}  \n")
                md_lines.append(f"**Modified:** {note['modified']}  \n")
                md_lines.append(f"**Version:** {note['version']}  \n\n")
                md_lines.append(f"{note['content']}\n\n")
                md_lines.append("---\n\n")
                
            markdown = "".join(md_lines)
            
            if output_path:
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(markdown)
                return {"status": "success", "exported_to": output_path, "format": "markdown"}
            else:
                return {"status": "success", "data": markdown, "format": "markdown"}
        else:
            return {"status": "error", "message": f"Unsupported format: {format}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        _close_db()

def backup_database(backup_path: str = None) -> Dict:
    """Create a backup of the entire knowledge base."""
    if not backup_path:
        backup_path = f"{DB_PATH}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    try:
        shutil.copy2(DB_PATH, backup_path)
        return {
            "status": "success",
            "backup_path": backup_path,
            "size_bytes": os.path.getsize(backup_path),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ─── Server Setup (enhanced with new tools) ─────────────────────────────────
server = BaseMCPServer("knowledge-base", "3.6")

# Legacy tools (backward compatible)
server.register_tool("store_note", {
    "description": "Save or update a note with tags and optional category",
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
            "tags": {"type": "string", "default": ""},
            "category": {"type": "string", "default": "general"}
        },
        "required": ["title", "content"]
    }
}, lambda **kw: store_note(kw["title"], kw["content"], kw.get("tags", ""), kw.get("category", "general")))

server.register_tool("search_notes", {
    "description": "Search notes by keyword with optional filters (FTS5 or LIKE fallback)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
            "category": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "offset": {"type": "integer", "default": 0}
        },
        "required": ["query"]
    }
}, lambda **kw: search_notes(
    kw["query"], 
    kw.get("limit", 50),
    kw.get("category"),
    kw.get("tags"),
    kw.get("offset", 0)
))

server.register_tool("delete_note", {
    "description": "Delete a note by title or ID",
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "note_id": {"type": "integer"}
        }
    }
}, lambda **kw: delete_note(kw.get("title"), kw.get("note_id")))

server.register_tool("get_note", {
    "description": "Get full note by title or ID",
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "note_id": {"type": "integer"}
        }
    }
}, lambda **kw: get_note(kw.get("title"), kw.get("note_id")))

# New enhanced tools
server.register_tool("list_categories", {
    "description": "List all categories with note counts",
    "inputSchema": {"type": "object", "properties": {}}
}, list_categories)

server.register_tool("get_kb_stats", {
    "description": "Get knowledge base statistics",
    "inputSchema": {"type": "object", "properties": {}}
}, get_stats)

server.register_tool("export_knowledge_base", {
    "description": "Export all notes to JSON or Markdown",
    "inputSchema": {
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"},
            "output_path": {"type": "string"}
        }
    }
}, lambda **kw: export_knowledge_base(kw.get("format", "json"), kw.get("output_path")))

server.register_tool("list_versions", {
    "description": "List all versions of a note",
    "inputSchema": {
        "type": "object",
        "properties": {"note_id": {"type": "integer"}},
        "required": ["note_id"]
    }
}, lambda **kw: {"status": "success", "versions": list_versions(kw["note_id"])})

server.register_tool("restore_version", {
    "description": "Restore a note from a specific version",
    "inputSchema": {
        "type": "object",
        "properties": {
            "note_id": {"type": "integer"},
            "timestamp": {"type": "string"}
        },
        "required": ["note_id"]
    }
}, lambda **kw: restore_version(kw["note_id"], kw.get("timestamp")))

server.register_tool("backup_database", {
    "description": "Create a backup of the entire knowledge base",
    "inputSchema": {
        "type": "object",
        "properties": {
            "backup_path": {"type": "string"}
        }
    }
}, lambda **kw: backup_database(kw.get("backup_path")))

if __name__ == "__main__":
    try:
        server.run()
    finally:
        _close_db()