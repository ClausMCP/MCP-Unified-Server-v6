#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Backup v1.0 — консистентный бэкап и восстановление состояния.

Назначение: проект живёт в C:\\Tools и переносится на другой компьютер. Эти
инструменты упаковывают все БД (память, граф, планировщик, цели, планы,
гипотезы, эпизоды, диалоги, задачи, RAG) в один zip и восстанавливают их.

Снимок берётся через online-backup SQLite (source.backup(dest)) — это безопасно
даже при активных писателях и корректно работает с WAL. Для не-SQLite файлов —
обычное копирование.

Инструменты: backup_state, list_backups, restore_state.
"""
import os
import json
import time
import zipfile
import shutil
import sqlite3
import tempfile
import glob
from datetime import datetime
from typing import Dict, List, Any

from mcp_shared import BaseMCPServer, _log, conversation_memory


def _data_dir() -> str:
    """Папка с БД (по пути основной БД памяти, обычно C:\\Tools)."""
    path = getattr(conversation_memory, "db_path", None) or "."
    return os.path.dirname(os.path.abspath(path)) or "."


def _backup_dir() -> str:
    d = os.environ.get("MCP_BACKUP_DIR") or os.path.join(_data_dir(), "backups")
    os.makedirs(d, exist_ok=True)
    return d


def _list_db_files(data_dir: str) -> List[str]:
    """Все *.db в папке данных (исключая WAL/SHM-сопутствующие и сами бэкапы)."""
    files = []
    for f in glob.glob(os.path.join(data_dir, "*.db")):
        base = os.path.basename(f)
        if base.endswith("-wal") or base.endswith("-shm"):
            continue
        files.append(f)
    return sorted(files)


def _snapshot_sqlite(src_path: str, dst_path: str) -> str:
    """Консистентный снимок SQLite через online-backup. Возврат: метод копирования."""
    try:
        src = sqlite3.connect(src_path, timeout=10)
        try:
            dst = sqlite3.connect(dst_path)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return "sqlite_backup"
    except Exception:
        # не SQLite или иная проблема — обычное копирование
        shutil.copy2(src_path, dst_path)
        return "file_copy"


def backup_state(label: str = None) -> Dict[str, Any]:
    """Создаёт zip-снимок всех БД проекта. Не блокирует работу серверов."""
    data_dir = _data_dir()
    db_files = _list_db_files(data_dir)
    if not db_files:
        return {"status": "error", "error": f"БД не найдены в {data_dir}"}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = ("_" + "".join(c for c in (label or "") if c.isalnum() or c in "-_")) if label else ""
    archive = os.path.join(_backup_dir(), f"mcp_backup_{stamp}{safe_label}.zip")

    manifest = {"created_at": datetime.now().isoformat(), "data_dir": data_dir, "files": []}
    tmp = tempfile.mkdtemp(prefix="mcp_backup_")
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in db_files:
                base = os.path.basename(src)
                snap = os.path.join(tmp, base)
                method = _snapshot_sqlite(src, snap)
                size = os.path.getsize(snap) if os.path.exists(snap) else 0
                zf.write(snap, arcname=base)
                manifest["files"].append({"name": base, "bytes": size, "method": method})
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = sum(f["bytes"] for f in manifest["files"])
    _log(f"[Backup] Created {archive} ({len(manifest['files'])} БД, {total} байт)")
    return {
        "status": "success",
        "archive": archive,
        "files": manifest["files"],
        "total_bytes": total,
    }


def list_backups() -> Dict[str, Any]:
    """Список доступных бэкапов (новые сверху)."""
    d = _backup_dir()
    items = []
    for z in sorted(glob.glob(os.path.join(d, "mcp_backup_*.zip")), reverse=True):
        try:
            st = os.stat(z)
            items.append({
                "archive": z,
                "size_mb": round(st.st_size / (1024 * 1024), 3),
                "created": datetime.fromtimestamp(st.st_mtime).isoformat(),
            })
        except Exception:
            continue
    return {"status": "success", "backup_dir": d, "count": len(items), "backups": items}


def restore_state(archive_path: str, confirm: bool = False) -> Dict[str, Any]:
    """
    Восстанавливает БД из бэкапа в папку данных.
    Без confirm=True — только показывает план (что будет перезаписано).
    С confirm=True — ПЕРЕД восстановлением делает резервную копию текущего
    состояния (pre_restore), затем перезаписывает БД.
    """
    if not os.path.exists(archive_path):
        return {"status": "error", "error": f"Архив не найден: {archive_path}"}
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = [n for n in zf.namelist() if n.endswith(".db")]
            manifest = {}
            if "manifest.json" in zf.namelist():
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except Exception as e:
        return {"status": "error", "error": f"Не удалось прочитать архив: {e}"}

    data_dir = _data_dir()
    if not confirm:
        return {
            "status": "preview",
            "message": "Это перезапишет текущие БД. Повторите с confirm=true для восстановления.",
            "target_dir": data_dir,
            "would_restore": names,
            "manifest": manifest,
        }

    # Резервная копия текущего состояния перед перезаписью
    pre = backup_state(label="pre_restore")
    restored = []
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for name in names:
                target = os.path.join(data_dir, os.path.basename(name))
                # удаляем WAL/SHM, чтобы не было рассинхрона с восстановленным файлом
                for suffix in ("-wal", "-shm"):
                    p = target + suffix
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                # Пишем во временный файл рядом и атомарно заменяем (os.replace),
                # чтобы не повредить файл при сбое и не писать «поверх» открытого.
                tmp_target = target + ".restoring"
                with zf.open(name) as src, open(tmp_target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(tmp_target, target)
                restored.append(os.path.basename(name))
    except Exception as e:
        return {"status": "error", "error": f"Ошибка восстановления: {e}", "pre_restore_backup": pre.get("archive")}

    _log(f"[Backup] Restored {len(restored)} БД из {archive_path}")
    return {
        "status": "success",
        "restored": restored,
        "pre_restore_backup": pre.get("archive"),
        "note": "Перезапустите серверы, чтобы они подхватили восстановленные БД.",
    }


def register_tools(server: BaseMCPServer):
    server.register_tool("backup_state", {
        "description": "Create a consistent zip snapshot of all project databases (memory, graph, "
                       "scheduler, goals, plans, etc.) for moving to another computer. Safe during operation.",
        "inputSchema": {"type": "object", "properties": {
            "label": {"type": "string", "description": "Optional label appended to the filename"}
        }}
    }, lambda **kw: backup_state(kw.get("label")))

    server.register_tool("list_backups", {
        "description": "List available state backups (newest first).",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: list_backups())

    server.register_tool("restore_state", {
        "description": "Restore databases from a backup archive. Without confirm=true shows a preview; "
                       "with confirm=true backs up current state then overwrites. DANGEROUS — overwrites data.",
        "inputSchema": {"type": "object", "properties": {
            "archive_path": {"type": "string", "description": "Path to mcp_backup_*.zip"},
            "confirm": {"type": "boolean", "description": "Must be true to actually overwrite databases"}
        }, "required": ["archive_path"]}
    }, lambda **kw: restore_state(kw.get("archive_path"), kw.get("confirm", False)))


__mcp_plugin__ = {
    "name": "backup",
    "version": "1.0.0",
    "description": "Consistent backup/restore of all project state for portability",
    "dependencies": [],
    "on_load": lambda: _log("[backup] v1.0 loaded — tools: backup_state, list_backups, restore_state"),
}

if __name__ == "__main__":
    print(json.dumps(backup_state(label="manual"), indent=2, ensure_ascii=False, default=str))
