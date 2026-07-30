#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP memPalace Integration v1.2 (CLI-adaptive search)
Обеспечивает доступ к внешнему пакету mempalace через CLI.
Поддерживает инициализацию, индексацию и поиск.
Добавлено: mempalace_add – добавление одного документа по тексту.
"""
import subprocess
import json
import os
import hashlib
import uuid
from pathlib import Path
from typing import Dict, Optional, List
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx, normalize_path, _ensure_allowed
)

__mcp_plugin__ = {
    "name": "mempalace",
    "version": "1.2.0",
    "description": "Интеграция с memPalace – семантическая память для кода и чатов",
    "dependencies": ["mempalace"],
    "on_load": lambda: _log("[mempalace] Loaded. Use mempalace_init/mine/search/add."),
    "on_unload": lambda: _log("[mempalace] Unloaded.")
}

def _run_mempalace(args: List[str], timeout: int = 300) -> Dict:
    """Выполняет команду mempalace и возвращает результат."""
    try:
        proc = subprocess.run(
            ["mempalace"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8',
            errors='replace'
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "success": proc.returncode == 0
        }
    except FileNotFoundError:
        return {"success": False, "error": "mempalace not installed. Run: pip install mempalace"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _get_folder_hash(folder_path: str) -> str:
    """Вычисляет хеш на основе имён, mtime и размеров всех файлов в папке."""
    hasher = hashlib.md5()
    try:
        for root, dirs, files in os.walk(folder_path):
            for f in sorted(files):
                file_path = os.path.join(root, f)
                try:
                    stat = os.stat(file_path)
                    hasher.update(f.encode())
                    hasher.update(str(stat.st_mtime).encode())
                    hasher.update(str(stat.st_size).encode())
                except Exception:
                    pass
    except Exception:
        pass
    return hasher.hexdigest()

def mempalace_init(project_path: str) -> Dict:
    """Инициализирует дворец памяти в указанной директории."""
    d_id = dialog_ctx.get()
    path = Path(normalize_path(project_path))
    try:
        _ensure_allowed(path, "mempalace_init")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}
    
    result = _run_mempalace(["init", str(path)])
    if result["success"]:
        conversation_memory.add(
            op="mempalace_init",
            paths={"project": str(path)},
            status="success",
            dialog=d_id,
            context=f"Initialized memPalace in {path}"
        )
        return {"status": "success", "project": str(path), "output": result["stdout"]}
    else:
        return {"status": "error", "error": result.get("error") or result["stderr"]}

# CLI `mempalace mine` принимает только --mode {projects, convos}.
# Прежний код передавал несуществующий режим "files", из-за чего
# любой вызов mine/add падал с "invalid choice: 'files'".
_VALID_MINE_MODES = {"projects", "convos"}
_MINE_MODE_ALIASES = {
    "files": "projects", "docs": "projects", "project": "projects", "all": "projects",
    "convo": "convos", "chats": "convos", "dialogs": "convos",
}

def _resolve_mine_mode(mode: str) -> str:
    return _MINE_MODE_ALIASES.get((mode or "").lower(), (mode or "").lower())

def mempalace_mine(path: Optional[str] = None, mode: str = "projects", force: bool = False) -> Dict:
    """
    Индексирует файлы (mode="projects") или чаты (mode="convos").
    Устаревшие значения "files"/"docs" автоматически приводятся к "projects".
    Если path не указан, используется MCP_MEMPALACE_PROJECT или текущая директория.
    force=True – игнорировать хеш и принудительно индексировать.
    """
    mode = _resolve_mine_mode(mode)
    if mode not in _VALID_MINE_MODES:
        return {"status": "error",
                "error": f"invalid mode '{mode}', choose from {sorted(_VALID_MINE_MODES)}"}

    if path is None:
        path = os.environ.get("MCP_MEMPALACE_PROJECT")
        if not path:
            path = os.getcwd()
    
    d_id = dialog_ctx.get()
    path_obj = Path(normalize_path(path))
    try:
        _ensure_allowed(path_obj, "mempalace_mine")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}

    hash_file = path_obj / ".mempalace_hash"
    current_hash = _get_folder_hash(str(path_obj))
    
    if not force and hash_file.exists():
        try:
            with open(hash_file, 'r') as f:
                last_hash = f.read().strip()
            if last_hash == current_hash:
                return {
                    "status": "skipped",
                    "message": "No changes detected, indexing skipped",
                    "path": str(path_obj),
                    "mode": mode
                }
        except Exception:
            pass

    result = _run_mempalace(["mine", str(path_obj), "--mode", mode], timeout=600)
    if result["success"]:
        try:
            with open(hash_file, 'w') as f:
                f.write(current_hash)
        except Exception:
            pass
            
        conversation_memory.add(
            op="mempalace_mine",
            paths={"path": str(path_obj), "mode": mode},
            status="success",
            dialog=d_id,
            context=f"Indexed {mode} in {path_obj}"
        )
        return {"status": "success", "path": str(path_obj), "mode": mode, "output": result["stdout"]}
    else:
        return {"status": "error", "error": result.get("error") or result["stderr"]}

def mempalace_search(query: str, project_path: Optional[str] = None,
                     limit: int = 10, mode: str = "all") -> Dict:
    """Поиск по памяти memPalace. mode: "all", "code", "convos", "docs"

    v1.2: адаптируется к установленной версии CLI. Если mempalace не знает
    флаги --limit/--mode/--project, они автоматически отбрасываются
    (limit применяется на нашей стороне), а --project пробуется как --palace.
    """
    d_id = dialog_ctx.get()

    proj = None
    if project_path:
        p = Path(normalize_path(project_path))
        try:
            _ensure_allowed(p, "mempalace_search")
        except PermissionError as e:
            return {"status": "error", "message": str(e)}
        proj = str(p)

    # Варианты вызова: от самого подробного к самому простому.
    variants: List[List[str]] = []
    if proj:
        variants.append(["search", query, "--limit", str(limit), "--mode", mode, "--project", proj])
        variants.append(["--palace", proj, "search", query, "--limit", str(limit), "--mode", mode])
        variants.append(["--palace", proj, "search", query])
    else:
        variants.append(["search", query, "--limit", str(limit), "--mode", mode])
    variants.append(["search", query])

    result = None
    last_err = ""
    for args in variants:
        result = _run_mempalace(args, timeout=60)
        if result.get("success"):
            break
        last_err = result.get("error") or result.get("stderr") or ""
        marker = last_err.lower()
        # CLI этой версии не знает переданный флаг — пробуем упрощённый вариант
        if ("unrecognized arguments" in marker or "invalid choice" in marker
                or "error: argument" in marker or "no such option" in marker):
            continue
        break  # другая ошибка — перебирать бессмысленно

    if not result or not result.get("success"):
        return {"status": "error", "error": last_err or "mempalace search failed"}

    try:
        data = json.loads(result["stdout"])
        results = data.get("results", []) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        text_out = (result["stdout"] or "").strip()
        results = [{"text": line} for line in text_out.splitlines() if line.strip()]

    conversation_memory.add(
        op="mempalace_search",
        paths={"query": query, "project": proj},
        status="success",
        dialog=d_id,
        context=f"Found {len(results)} results in memPalace"
    )

    return {
        "status": "success",
        "query": query,
        "count": len(results),
        "results": results[:limit],
        "raw_output": result["stdout"]
    }

def mempalace_status(project_path: Optional[str] = None) -> Dict:
    """Показывает статус дворца памяти."""
    args = ["status"]
    if project_path:
        p = Path(normalize_path(project_path))
        try:
            _ensure_allowed(p, "mempalace_status")
        except PermissionError as e:
            return {"status": "error", "message": str(e)}
        args.extend(["--project", str(p)])

    result = _run_mempalace(args)
    if result["success"]:
        return {"status": "success", "info": result["stdout"]}
    else:
        return {"status": "error", "error": result.get("error") or result["stderr"]}

# ─── Добавление одного документа (новая функция) ──────────────────────────
def mempalace_add(content: str, metadata: Dict = None, project_path: Optional[str] = None) -> Dict:
    """
    Сохраняет один документ в memPalace и индексирует его.
    
    Args:
        content: Текст документа
        metadata: Словарь с метаданными (будет сохранён в файл-спутник)
        project_path: Путь к проекту mempalace (если не указан, берётся из MCP_MEMPALACE_PROJECT или текущей директории)
    """
    d_id = dialog_ctx.get()
    
    if project_path is None:
        project_path = os.environ.get("MCP_MEMPALACE_PROJECT")
        if not project_path:
            project_path = os.getcwd()
    
    proj = Path(normalize_path(project_path))
    try:
        _ensure_allowed(proj, "mempalace_add")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}
    
    docs_dir = proj / "_mcp_docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    
    doc_id = uuid.uuid4().hex[:12]
    filename = f"doc_{doc_id}.txt"
    file_path = docs_dir / filename
    
    try:
        file_path.write_text(content, encoding='utf-8')
    except Exception as e:
        return {"status": "error", "message": f"Failed to write document: {e}"}
    
    if metadata:
        meta_path = docs_dir / f"doc_{doc_id}.meta.json"
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, default=str), encoding='utf-8')
    
    # Индексируем папку _mcp_docs (инкрементально).
    # "projects" — единственный корректный режим CLI для индексации директории с файлами.
    result = mempalace_mine(path=str(docs_dir), mode="projects", force=False)
    
    if result.get("status") == "error":
        return result
    
    conversation_memory.add(
        op="mempalace_add",
        paths={"project": str(proj), "document": str(file_path)},
        status="success",
        dialog=d_id,
        context=f"Added document {filename} to mempalace project {proj.name}"
    )
    
    return {
        "status": "success",
        "document_id": doc_id,
        "file_path": str(file_path),
        "project_path": str(proj),
        "index_result": result
    }

# ─── Регистрация инструментов (обновлённая) ────────────────────────────────
def register_tools(server: BaseMCPServer):
    server.register_tool("mempalace_init", {
        "description": "Инициализировать хранилище memPalace в проекте",
        "inputSchema": {
            "type": "object",
            "properties": {"project_path": {"type": "string"}},
            "required": ["project_path"]
        }
    }, lambda **kw: mempalace_init(kw["project_path"]))

    server.register_tool("mempalace_mine", {
        "description": "Индексировать файлы или чаты с помощью memPalace. Если path не указан, используется MCP_MEMPALACE_PROJECT. force=True для принудительной переиндексации.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к проекту (опционально)"},
                "mode": {"type": "string", "enum": ["projects", "convos"], "default": "projects", "description": "projects — индексировать файлы/директорию; convos — индексировать чаты"},
                "force": {"type": "boolean", "default": False, "description": "Принудительная индексация без проверки хеша"}
            },
            "required": []
        }
    }, lambda **kw: mempalace_mine(kw.get("path"), kw.get("mode", "projects"), kw.get("force", False)))

    server.register_tool("mempalace_search", {
        "description": "Поиск по памяти memPalace (код, чаты, документы)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project_path": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "mode": {"type": "string", "enum": ["all", "code", "convos", "docs"], "default": "all"}
            },
            "required": ["query"]
        }
    }, lambda **kw: mempalace_search(
        kw["query"], kw.get("project_path"), kw.get("limit", 10), kw.get("mode", "all")
    ))

    server.register_tool("mempalace_status", {
        "description": "Показать статус дворца памяти",
        "inputSchema": {
            "type": "object",
            "properties": {"project_path": {"type": "string"}}
        }
    }, lambda **kw: mempalace_status(kw.get("project_path")))

    # Новый инструмент
    server.register_tool("mempalace_add", {
        "description": "Добавить один документ в память memPalace (текст, метаданные). Документ будет проиндексирован.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Текст документа"},
                "metadata": {"type": "object", "description": "Метаданные (например, source_file, title, date_processed)"},
                "project_path": {"type": "string", "description": "Путь к проекту mempalace (опционально)"}
            },
            "required": ["content"]
        }
    }, lambda **kw: mempalace_add(kw["content"], kw.get("metadata"), kw.get("project_path")))