#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Sync v3.2 (Context-Isolated & Optimized)
Incremental directory synchronization with hash/size/time comparison,
mirror/update strategies, dry-run preview, and contextvars integration.
NEW: Bidirectional sync (two-way) with conflict resolution.
"""
import os
import sys
import json
import time
import hashlib
import shutil
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple, Literal
from datetime import datetime
from mcp_shared import (
    _log, normalize_path, _ensure_allowed, _format_size,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Hash helpers (unchanged) ─────────────────────────────────────────────
def _file_hash(path: Path, algorithm: str = "xxh64", sample_size: int = 65536) -> str:
    """Fast hash: full for small files, sampled for large."""
    try:
        size = path.stat().st_size
        if size == 0:
            return "0"
        if algorithm == "xxh64":
            try:
                import xxhash
                h = xxhash.xxh64()
                with open(path, 'rb') as f:
                    if size <= sample_size * 4:
                        h.update(f.read())
                    else:
                        h.update(f.read(sample_size))
                        f.seek(size // 2)
                        h.update(f.read(sample_size))
                        f.seek(-sample_size, 2)
                        h.update(f.read(sample_size))
                return h.hexdigest()
            except ImportError:
                algorithm = "md5"
        if algorithm == "md5":
            h = hashlib.md5()
            with open(path, 'rb') as f:
                if size <= 10485760:  # 10 MB full
                    h.update(f.read())
                else:
                    h.update(f.read(sample_size))
                    f.seek(size // 2)
                    h.update(f.read(sample_size))
                    f.seek(-sample_size, 2)
                    h.update(f.read(sample_size))
            return h.hexdigest()[:16]
        return str(size)
    except Exception:
        return ""

def _file_signature(path: Path) -> Tuple[int, float, str]:
    """Return (size, mtime, hash) for comparison."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime, _file_hash(path))
    except Exception:
        return (0, 0, "")

# ─── ИТЕРАТИВНЫЕ ВЕРСИИ _scan_tree И _scan_tree_full (замена рекурсии) ────
def _scan_tree(root: Path) -> Dict[str, Tuple[int, float, str]]:
    """Итеративный обход директории, возвращает {относительный_путь: (размер, mtime, '')}."""
    result = {}
    stack = [(root, "")]  # (текущий путь, относительный префикс)
    while stack:
        current, prefix = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    rel = f"{prefix}{entry.name}" if prefix else entry.name
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), f"{rel}/"))
                    else:
                        try:
                            st = entry.stat(follow_symlinks=False)
                            result[rel] = (st.st_size, st.st_mtime, "")
                        except Exception:
                            pass
        except PermissionError:
            continue
    return result

def _scan_tree_full(root: Path) -> Dict[str, Tuple[int, float, str]]:
    """Итеративный обход с полной сигнатурой (размер, mtime, хеш)."""
    result = {}
    stack = [(root, "")]
    while stack:
        current, prefix = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    rel = f"{prefix}{entry.name}" if prefix else entry.name
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), f"{rel}/"))
                    else:
                        try:
                            st = entry.stat(follow_symlinks=False)
                            # Вычисляем хеш только для полной версии
                            fpath = Path(entry.path)
                            file_hash = _file_hash(fpath)  # используем существующую _file_hash
                            result[rel] = (st.st_size, st.st_mtime, file_hash)
                        except Exception:
                            pass
        except PermissionError:
            continue
    return result

# ─── Core sync logic (unidirectional) ─────────────────────────────────────
def sync_directories(source: str, target: str, strategy: str = "update",
                     dry_run: bool = True, max_depth: int = 20,
                     skip_errors: bool = True, chunk_size: int = 50) -> Dict:
    """
    Incremental sync: update or mirror.
    strategy: "update" (copy new/changed, keep extra in target)
              "mirror" (copy new/changed, delete extra in target)
    """
    start = time.time()
    sp = Path(normalize_path(source))
    tp = Path(normalize_path(target))
    _ensure_allowed(sp, "sync")
    _ensure_allowed(tp, "sync")
    if not sp.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    if not sp.is_dir():
        raise ValueError(f"Source is not a directory: {source}")

    # Scan phase
    source_files = _scan_tree(sp)
    target_files = _scan_tree(tp) if tp.exists() else {}

    # Determine operations
    to_copy = []
    to_delete = []
    for rel, s_sig in source_files.items():
        t_sig = target_files.get(rel)
        if not t_sig:
            to_copy.append({"rel": rel, "reason": "new", "source": str(sp / rel.replace("/", os.sep))})
        elif t_sig[0] != s_sig[0] or abs(t_sig[1] - s_sig[1]) > 2:
            to_copy.append({"rel": rel, "reason": "modified", "source": str(sp / rel.replace("/", os.sep))})

    if strategy == "mirror":
        for rel in target_files:
            if rel not in source_files:
                to_delete.append({"rel": rel, "target": str(tp / rel.replace("/", os.sep))})

    total_copy = len(to_copy)
    total_delete = len(to_delete)

    if dry_run:
        return {
            "status": "dry_run", "strategy": strategy, "source": str(sp), "target": str(tp),
            "to_copy": total_copy, "to_delete": total_delete,
            "copy_details": to_copy[:20], "delete_details": to_delete[:20],
            "scanned_source": len(source_files), "scanned_target": len(target_files),
            "elapsed_sec": round(time.time() - start, 2)
        }

    # Execute copy (chunked)
    copied = 0
    copy_errors = []
    chunk = to_copy[:chunk_size]
    remaining = to_copy[chunk_size:]

    for op in chunk:
        src = Path(op["source"])
        dst = tp / op["rel"].replace("/", os.sep)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            copied += 1
        except Exception as e:
            if skip_errors:
                copy_errors.append({"rel": op["rel"], "error": str(e)})
            else:
                raise

    # Execute delete (mirror)
    deleted = 0
    delete_errors = []
    if strategy == "mirror":
        for op in to_delete:
            try:
                p = Path(op["target"])
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
                deleted += 1
            except Exception as e:
                if skip_errors:
                    delete_errors.append({"rel": op["rel"], "error": str(e)})
                else:
                    raise

    elapsed = time.time() - start
    result = {
        "status": "completed" if not remaining else "partial",
        "strategy": strategy, "source": str(sp), "target": str(tp),
        "copied": copied, "deleted": deleted,
        "copy_errors": len(copy_errors), "delete_errors": len(delete_errors),
        "errors": copy_errors + delete_errors if (copy_errors or delete_errors) else None,
        "remaining": len(remaining), "elapsed_sec": round(elapsed, 2)
    }

    if copied > 0 or deleted > 0:
        conversation_memory.add(
            op="sync_directories",
            paths={"source": str(sp), "target": str(tp)},
            status=result["status"], dialog=dialog_ctx.get(),
            context=f"Synced {copied} copied, {deleted} deleted, strategy={strategy}"
        )
    return result

# ─── Bidirectional Sync (использует итеративный _scan_tree_full) ──────────
def sync_bidirectional(path_a: str, path_b: str,
                       conflict_strategy: Literal["newer_wins", "source_wins", "target_wins", "skip", "rename_conflict"] = "newer_wins",
                       dry_run: bool = True,
                       compare_by: Literal["mtime", "hash", "size_mtime"] = "mtime",
                       delete_orphaned: bool = False,
                       skip_errors: bool = True) -> Dict:
    """
    Двунаправленная синхронизация двух директорий.
    
    Обнаруживает:
    - Файлы, которые есть только в A (orphaned in B)
    - Файлы, которые есть только в B (orphaned in A)
    - Файлы, изменённые в A, но не в B
    - Файлы, изменённые в B, но не в A
    - Конфликты: файлы изменены с обеих сторон
    
    conflict_strategy:
        "newer_wins"      – копируется более новая версия (по mtime)
        "source_wins"     – всегда побеждает сторона A
        "target_wins"     – всегда побеждает сторона B
        "skip"            – конфликтные файлы пропускаются (логируются)
        "rename_conflict" – конфликтный файл переименовывается (добавляется .conflict) и копируется новейшая версия
    """
    start = time.time()
    pa = Path(normalize_path(path_a))
    pb = Path(normalize_path(path_b))
    _ensure_allowed(pa, "sync_bidirectional")
    _ensure_allowed(pb, "sync_bidirectional")
    
    if not pa.exists() or not pa.is_dir():
        raise FileNotFoundError(f"Directory A not found or not a directory: {path_a}")
    if not pb.exists() or not pb.is_dir():
        raise FileNotFoundError(f"Directory B not found or not a directory: {path_b}")
    
    # Сканируем обе стороны с полными сигнатурами (итеративно)
    files_a = _scan_tree_full(pa)
    files_b = _scan_tree_full(pb)
    
    # Множества относительных путей
    rels_a = set(files_a.keys())
    rels_b = set(files_b.keys())
    
    only_in_a = rels_a - rels_b
    only_in_b = rels_b - rels_a
    common = rels_a & rels_b
    
    # Анализ изменений для общих файлов
    a_newer = []
    b_newer = []
    conflicts = []
    identical = []
    
    for rel in common:
        sig_a = files_a[rel]
        sig_b = files_b[rel]
        
        # Сравнение по выбранному методу
        if compare_by == "hash":
            a_val = sig_a[2]  # hash
            b_val = sig_b[2]
            a_time = sig_a[1]
            b_time = sig_b[1]
        elif compare_by == "size_mtime":
            a_val = (sig_a[0], sig_a[1])
            b_val = (sig_b[0], sig_b[1])
            a_time = sig_a[1]
            b_time = sig_b[1]
        else:  # mtime
            a_val = sig_a[1]
            b_val = sig_b[1]
            a_time = sig_a[1]
            b_time = sig_b[1]
        
        if a_val == b_val:
            identical.append(rel)
        else:
            # Определяем, где новее (по mtime)
            if a_time > b_time + 1:  # допуск 1 секунда
                a_newer.append({"rel": rel, "source": str(pa / rel.replace("/", os.sep)), "mtime_a": a_time, "mtime_b": b_time})
            elif b_time > a_time + 1:
                b_newer.append({"rel": rel, "source": str(pb / rel.replace("/", os.sep)), "mtime_a": a_time, "mtime_b": b_time})
            else:
                # Времена близки, но содержимое разное -> конфликт
                conflicts.append({
                    "rel": rel,
                    "path_a": str(pa / rel.replace("/", os.sep)),
                    "path_b": str(pb / rel.replace("/", os.sep)),
                    "size_a": sig_a[0], "size_b": sig_b[0],
                    "mtime_a": a_time, "mtime_b": b_time,
                    "hash_a": sig_a[2], "hash_b": sig_b[2]
                })
    
    # Планирование операций
    operations = []
    
    # 1. Копирование from A to B для файлов, которые есть только в A (если delete_orphaned=False)
    if not delete_orphaned:
        for rel in only_in_a:
            src = pa / rel.replace("/", os.sep)
            dst = pb / rel.replace("/", os.sep)
            operations.append({
                "type": "copy_a_to_b",
                "rel": rel,
                "src": str(src),
                "dst": str(dst),
                "reason": "only_in_a"
            })
    
    # 2. Копирование from B to A для файлов, которые есть только в B
    for rel in only_in_b:
        src = pb / rel.replace("/", os.sep)
        dst = pa / rel.replace("/", os.sep)
        operations.append({
            "type": "copy_b_to_a",
            "rel": rel,
            "src": str(src),
            "dst": str(dst),
            "reason": "only_in_b"
        })
    
    # 3. Копирование обновлений: A новее -> копируем в B
    for item in a_newer:
        src = pa / item["rel"].replace("/", os.sep)
        dst = pb / item["rel"].replace("/", os.sep)
        operations.append({
            "type": "copy_a_to_b",
            "rel": item["rel"],
            "src": str(src),
            "dst": str(dst),
            "reason": "a_newer",
            "mtime_a": item["mtime_a"],
            "mtime_b": item["mtime_b"]
        })
    
    # 4. Копирование обновлений: B новее -> копируем в A
    for item in b_newer:
        src = pb / item["rel"].replace("/", os.sep)
        dst = pa / item["rel"].replace("/", os.sep)
        operations.append({
            "type": "copy_b_to_a",
            "rel": item["rel"],
            "src": str(src),
            "dst": str(dst),
            "reason": "b_newer",
            "mtime_a": item["mtime_a"],
            "mtime_b": item["mtime_b"]
        })
    
    # 5. Конфликты
    conflict_handled = []
    for conf in conflicts:
        rel = conf["rel"]
        if conflict_strategy == "newer_wins":
            # Определяем, у кого новее mtime (уже есть в conf)
            if conf["mtime_a"] > conf["mtime_b"]:
                src = conf["path_a"]
                dst = pb / rel.replace("/", os.sep)
                operations.append({
                    "type": "copy_a_to_b",
                    "rel": rel, "src": src, "dst": str(dst),
                    "reason": "conflict_newer_wins_a"
                })
                conflict_handled.append({"rel": rel, "resolution": "a_wins"})
            else:
                src = conf["path_b"]
                dst = pa / rel.replace("/", os.sep)
                operations.append({
                    "type": "copy_b_to_a",
                    "rel": rel, "src": src, "dst": str(dst),
                    "reason": "conflict_newer_wins_b"
                })
                conflict_handled.append({"rel": rel, "resolution": "b_wins"})
        elif conflict_strategy == "source_wins":
            src = conf["path_a"]
            dst = pb / rel.replace("/", os.sep)
            operations.append({
                "type": "copy_a_to_b",
                "rel": rel, "src": src, "dst": str(dst),
                "reason": "conflict_source_wins"
            })
            conflict_handled.append({"rel": rel, "resolution": "a_wins"})
        elif conflict_strategy == "target_wins":
            src = conf["path_b"]
            dst = pa / rel.replace("/", os.sep)
            operations.append({
                "type": "copy_b_to_a",
                "rel": rel, "src": src, "dst": str(dst),
                "reason": "conflict_target_wins"
            })
            conflict_handled.append({"rel": rel, "resolution": "b_wins"})
        elif conflict_strategy == "rename_conflict":
            # Переименовываем конфликтный файл на целевой стороне и копируем новейший
            if conf["mtime_a"] > conf["mtime_b"]:
                # Файл A новее, копируем в B, а старый B переименовываем
                old_b = Path(conf["path_b"])
                new_name = old_b.stem + ".conflict_" + str(int(time.time())) + old_b.suffix
                new_b_path = old_b.parent / new_name
                operations.append({
                    "type": "rename_b",
                    "rel": rel,
                    "src": str(old_b),
                    "dst": str(new_b_path),
                    "reason": "conflict_rename_b"
                })
                operations.append({
                    "type": "copy_a_to_b",
                    "rel": rel,
                    "src": conf["path_a"],
                    "dst": str(old_b),
                    "reason": "conflict_copy_newer_a"
                })
                conflict_handled.append({"rel": rel, "resolution": "renamed_b"})
            else:
                old_a = Path(conf["path_a"])
                new_name = old_a.stem + ".conflict_" + str(int(time.time())) + old_a.suffix
                new_a_path = old_a.parent / new_name
                operations.append({
                    "type": "rename_a",
                    "rel": rel,
                    "src": str(old_a),
                    "dst": str(new_a_path),
                    "reason": "conflict_rename_a"
                })
                operations.append({
                    "type": "copy_b_to_a",
                    "rel": rel,
                    "src": conf["path_b"],
                    "dst": str(old_a),
                    "reason": "conflict_copy_newer_b"
                })
                conflict_handled.append({"rel": rel, "resolution": "renamed_a"})
        else:  # skip
            conflict_handled.append({"rel": rel, "resolution": "skipped"})
    
    # 6. Удаление осиротевших файлов (если включено)
    if delete_orphaned:
        for rel in only_in_a:
            # Файл есть только в A, удаляем из B
            dst = pb / rel.replace("/", os.sep)
            operations.append({
                "type": "delete_in_b",
                "rel": rel,
                "dst": str(dst),
                "reason": "orphaned_in_b"
            })
        for rel in only_in_b:
            # Файл есть только в B, удаляем из A
            dst = pa / rel.replace("/", os.sep)
            operations.append({
                "type": "delete_in_a",
                "rel": rel,
                "dst": str(dst),
                "reason": "orphaned_in_a"
            })
    
    # Если dry_run, возвращаем план
    if dry_run:
        return {
            "status": "dry_run",
            "mode": "bidirectional",
            "path_a": str(pa),
            "path_b": str(pb),
            "conflict_strategy": conflict_strategy,
            "compare_by": compare_by,
            "delete_orphaned": delete_orphaned,
            "stats": {
                "only_in_a": len(only_in_a),
                "only_in_b": len(only_in_b),
                "identical": len(identical),
                "a_newer": len(a_newer),
                "b_newer": len(b_newer),
                "conflicts": len(conflicts)
            },
            "conflicts_list": conflicts[:20],
            "operations_count": len(operations),
            "operations_sample": operations[:20],
            "elapsed_sec": round(time.time() - start, 2)
        }
    
    # Выполнение операций
    executed = {"copied_a_to_b": 0, "copied_b_to_a": 0, "renamed": 0, "deleted": 0}
    errors = []
    
    for op in operations:
        try:
            if op["type"] in ("copy_a_to_b", "copy_b_to_a"):
                src = Path(op["src"])
                dst = Path(op["dst"])
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
                executed["copied_a_to_b" if op["type"] == "copy_a_to_b" else "copied_b_to_a"] += 1
            elif op["type"] in ("rename_a", "rename_b"):
                src = Path(op["src"])
                dst = Path(op["dst"])
                src.rename(dst)
                executed["renamed"] += 1
            elif op["type"] in ("delete_in_a", "delete_in_b"):
                p = Path(op["dst"])
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
                executed["deleted"] += 1
        except Exception as e:
            if skip_errors:
                errors.append({"operation": op, "error": str(e)})
            else:
                raise
    
    elapsed = time.time() - start
    result = {
        "status": "completed",
        "mode": "bidirectional",
        "path_a": str(pa),
        "path_b": str(pb),
        "conflict_strategy": conflict_strategy,
        "stats": {
            "only_in_a": len(only_in_a),
            "only_in_b": len(only_in_b),
            "identical": len(identical),
            "a_newer": len(a_newer),
            "b_newer": len(b_newer),
            "conflicts": len(conflicts)
        },
        "executed": executed,
        "errors": errors if errors else None,
        "elapsed_sec": round(elapsed, 2)
    }
    
    conversation_memory.add(
        op="sync_bidirectional",
        paths={"path_a": str(pa), "path_b": str(pb)},
        status=result["status"],
        dialog=dialog_ctx.get(),
        context=f"Bidirectional sync: {executed['copied_a_to_b']} A→B, {executed['copied_b_to_a']} B→A, {executed['deleted']} deleted, {len(conflicts)} conflicts"
    )
    return result

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-sync", "3.2")
server.register_tool("sync_directories", {
    "description": "Incremental sync: update or mirror directories with dry-run preview",
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "target": {"type": "string"},
            "strategy": {"type": "string", "enum": ["update", "mirror"], "default": "update"},
            "dry_run": {"type": "boolean", "default": True},
            "max_depth": {"type": "integer", "default": 20},
            "skip_errors": {"type": "boolean", "default": True},
            "chunk_size": {"type": "integer", "default": 50}
        },
        "required": ["source", "target"]
    }
}, lambda **kw: sync_directories(
    kw["source"], kw["target"],
    kw.get("strategy", "update"), kw.get("dry_run", True),
    kw.get("max_depth", 20), kw.get("skip_errors", True),
    kw.get("chunk_size", 50)
))

# NEW: Bidirectional sync tool
server.register_tool("sync_bidirectional", {
    "description": "Двунаправленная синхронизация двух директорий с обработкой конфликтов",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path_a": {"type": "string", "description": "Первая директория"},
            "path_b": {"type": "string", "description": "Вторая директория"},
            "conflict_strategy": {
                "type": "string",
                "enum": ["newer_wins", "source_wins", "target_wins", "skip", "rename_conflict"],
                "default": "newer_wins",
                "description": "Стратегия разрешения конфликтов"
            },
            "dry_run": {"type": "boolean", "default": True},
            "compare_by": {
                "type": "string",
                "enum": ["mtime", "hash", "size_mtime"],
                "default": "mtime",
                "description": "Чем сравнивать файлы"
            },
            "delete_orphaned": {"type": "boolean", "default": False, "description": "Удалять файлы, отсутствующие на другой стороне"},
            "skip_errors": {"type": "boolean", "default": True}
        },
        "required": ["path_a", "path_b"]
    }
}, lambda **kw: sync_bidirectional(
    kw["path_a"], kw["path_b"],
    kw.get("conflict_strategy", "newer_wins"),
    kw.get("dry_run", True),
    kw.get("compare_by", "mtime"),
    kw.get("delete_orphaned", False),
    kw.get("skip_errors", True)
))

if __name__ == "__main__":
    server.run()