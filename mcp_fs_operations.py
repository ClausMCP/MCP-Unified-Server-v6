#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Operations v1.1 (Context-Isolated)
Базовые операции: move, copy, delete, create_dir, read, write
С поддержкой dry_run, валидацией путей и изоляцией диалогов через contextvars.
Добавлен инструмент write_file_from_rag для записи файлов из RAG.
"""
import os
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from mcp_shared import (
    normalize_path, _ensure_allowed, BaseMCPServer, conversation_memory,
    validate_paths_decorator, list_directory_sync, dialog_ctx
)

@validate_paths_decorator
def move_file(source: str, destination: str, dry_run: bool = False) -> dict:
    """Переместить файл или папку."""
    src = Path(normalize_path(source))
    dst = Path(normalize_path(destination))
    _ensure_allowed(src, "move_file")
    _ensure_allowed(dst.parent, "move_file")
    if not src.exists():
        return {"status": "error", "message": f"Source not found: {source}"}
    if dry_run:
        return {"status": "dry_run", "source": str(src), "destination": str(dst)}
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    conversation_memory.add(
        op="move_file", paths={"from": str(src), "to": str(dst)},
        status="moved", dialog=dialog_ctx.get(),
        context=f"Moved {src.name} -> {dst}"
    )
    return {"status": "moved", "source": str(src), "destination": str(dst)}

@validate_paths_decorator
def copy_file(source: str, destination: str, dry_run: bool = False) -> dict:
    """Скопировать файл или папку."""
    src = Path(normalize_path(source))
    dst = Path(normalize_path(destination))
    _ensure_allowed(src, "copy_file")
    _ensure_allowed(dst.parent, "copy_file")
    if not src.exists():
        return {"status": "error", "message": f"Source not found: {source}"}
    if dry_run:
        return {"status": "dry_run", "source": str(src), "destination": str(dst)}
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    conversation_memory.add(
        op="copy_file", paths={"from": str(src), "to": str(dst)},
        status="copied", dialog=dialog_ctx.get(),
        context=f"Copied {src.name} -> {dst}"
    )
    return {"status": "copied", "source": str(src), "destination": str(dst)}

@validate_paths_decorator
def delete_file(path: str, dry_run: bool = False, use_trash: bool = True) -> dict:
    """Удалить файл или папку. При use_trash=True отправляет в корзину."""
    p = Path(normalize_path(path))
    _ensure_allowed(p, "delete_file")
    if not p.exists():
        return {"status": "error", "message": f"Not found: {path}"}
    if dry_run:
        return {"status": "dry_run", "path": str(p), "use_trash": use_trash}
    if use_trash:
        try:
            from mcp_fs_trash import move_to_trash
            return move_to_trash(path, dialog_id=dialog_ctx.get())
        except ImportError:
            use_trash = False
    if not use_trash:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    conversation_memory.add(
        op="delete_file", paths={"path": str(p)},
        status="deleted", dialog=dialog_ctx.get(),
        context=f"Deleted {p.name}"
    )
    return {"status": "deleted", "path": str(p)}

@validate_paths_decorator
def create_directory(path: str, dry_run: bool = False) -> dict:
    """Создать директорию (рекурсивно)."""
    p = Path(normalize_path(path))
    _ensure_allowed(p, "create_directory")
    if dry_run:
        return {"status": "dry_run", "path": str(p)}
    p.mkdir(parents=True, exist_ok=True)
    conversation_memory.add(
        op="create_directory", paths={"path": str(p)},
        status="created", dialog=dialog_ctx.get(),
        context=f"Created directory {p}"
    )
    return {"status": "created", "path": str(p)}

def read_file(path: str, offset: int = 0, limit: int = 10000, encoding: str = "utf-8") -> dict:
    """Прочитать содержимое текстового файла."""
    p = Path(normalize_path(path))
    _ensure_allowed(p, "read_file")
    if not p.is_file():
        return {"error": f"Not a file or not found: {path}"}
    try:
        with open(p, 'r', encoding=encoding, errors='replace') as f:
            f.seek(offset)
            content = f.read(limit)
        return {
            "path": str(p), "size": p.stat().st_size,
            "encoding": encoding, "content": content,
            "offset": offset, "limit": limit,
            "has_more": offset + len(content) < p.stat().st_size
        }
    except UnicodeDecodeError:
        for enc in ["cp1251", "cp1252", "latin-1", "utf-8-sig"]:
            try:
                with open(p, 'r', encoding=enc, errors='replace') as f:
                    f.seek(offset)
                    content = f.read(limit)
                return {
                    "path": str(p), "size": p.stat().st_size,
                    "encoding": enc, "content": content,
                    "offset": offset, "limit": limit,
                    "has_more": offset + len(content) < p.stat().st_size
                }
            except Exception:
                continue
        return {"error": "Cannot decode file with known encodings"}

def write_file(path: str, content: str, append: bool = False) -> dict:
    """Записать или дополнить файл (атомарно)."""
    p = Path(normalize_path(path))
    _ensure_allowed(p, "write_file")
    p.parent.mkdir(parents=True, exist_ok=True)
    if append and p.exists():
        with open(p, 'a', encoding='utf-8') as f:
            f.write(content)
        conversation_memory.add(
            op="write_file", paths={"path": str(p)},
            status="appended", dialog=dialog_ctx.get(),
            context=f"Appended to {p.name}"
        )
        return {"status": "appended", "path": str(p)}
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp, str(p))
        conversation_memory.add(
            op="write_file", paths={"path": str(p)},
            status="written", dialog=dialog_ctx.get(),
            context=f"Written {len(content)} bytes to {p.name}"
        )
        return {"status": "written", "path": str(p), "bytes": len(content.encode('utf-8'))}
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise

def write_file_from_rag(doc_id: str, file_path: str, collection_name: str = "default") -> dict:
    """
    Записывает файл на диск, извлекая содержимое из RAG по doc_id.
    Использует существующую функцию write_file.
    """
    try:
        from mcp_rag_engine import rag_get_document
    except ImportError:
        return {"error": "RAG engine not available"}
    content = rag_get_document(doc_id, collection_name)
    if not content:
        return {"error": f"Document with doc_id '{doc_id}' not found in RAG"}
    # Используем существующую функцию write_file (без append)
    return write_file(file_path, content, append=False)

def list_directory(path: str, offset: int = 0, limit: int = 200) -> dict:
    """Вернуть содержимое директории компактным текстом (без рекурсии)."""
    result = list_directory_sync(path, recursive=False)
    if "error" in result:
        return result

    entries = result["entries"]
    total = len(entries)
    page = entries[offset:min(offset + limit, total)]

    dirs = [e["name"] for e in page if e.get("is_dir")]
    files = [e for e in page if not e.get("is_dir")]

    lines = []
    if dirs:
        lines.append("Папки: " + ", ".join(sorted(dirs)))
    if files:
        lines.append("Файлы:")
        for f in sorted(files, key=lambda x: x["name"]):
            kb = (f.get("size") or 0) // 1024
            lines.append(f"  {f['name']} ({kb} КБ)" if kb else f"  {f['name']}")

    text = f"{normalize_path(path)} — всего {total}\n" + "\n".join(lines)
    if offset + limit < total:
        text += f"\n(показано {len(page)} из {total}, offset={offset + limit} для продолжения)"

    return {"text": text}

# ---- Регистрация сервера ----
server = BaseMCPServer("filesystem-operations", "1.1")
server.register_tool("move_file", {
    "description": "Move a file or directory",
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False}
        },
        "required": ["source", "destination"]
    }
}, lambda **kw: move_file(kw["source"], kw["destination"], kw.get("dry_run", False)))

server.register_tool("copy_file", {
    "description": "Copy a file or directory",
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False}
        },
        "required": ["source", "destination"]
    }
}, lambda **kw: copy_file(kw["source"], kw["destination"], kw.get("dry_run", False)))

server.register_tool("delete_file", {
    "description": "Delete a file or directory (optionally move to trash)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
            "use_trash": {"type": "boolean", "default": True}
        },
        "required": ["path"]
    }
}, lambda **kw: delete_file(kw["path"], kw.get("dry_run", False), kw.get("use_trash", True)))

server.register_tool("create_directory", {
    "description": "Create a directory recursively",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False}
        },
        "required": ["path"]
    }
}, lambda **kw: create_directory(kw["path"], kw.get("dry_run", False)))

server.register_tool("read_file", {
    "description": "Read a text file with optional offset/limit",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "limit": {"type": "integer", "default": 10000},
            "encoding": {"type": "string", "default": "utf-8"}
        },
        "required": ["path"]
    }
}, lambda **kw: read_file(kw["path"], kw.get("offset", 0), kw.get("limit", 10000), kw.get("encoding", "utf-8")))

server.register_tool("write_file", {
    "description": "Write or append to a file atomically",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean", "default": False}
        },
        "required": ["path", "content"]
    }
}, lambda **kw: write_file(kw["path"], kw["content"], kw.get("append", False)))

server.register_tool("write_file_from_rag", {
    "description": "Write a file to disk using content retrieved from RAG by document ID",
    "inputSchema": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string"},
            "file_path": {"type": "string"},
            "collection_name": {"type": "string", "default": "default"}
        },
        "required": ["doc_id", "file_path"]
    }
}, lambda **kw: write_file_from_rag(kw["doc_id"], kw["file_path"], kw.get("collection_name", "default")))

server.register_tool("list_directory", {
    "description": "List directory contents with pagination",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "limit": {"type": "integer", "default": 1000}
        },
        "required": ["path"]
    }
}, lambda **kw: list_directory(kw["path"], kw.get("offset", 0), kw.get("limit", 1000)))

if __name__ == "__main__":
    server.run()