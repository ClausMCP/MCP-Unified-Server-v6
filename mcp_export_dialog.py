#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Export Dialog v1.5 – экспорт диалога в Markdown/PDF.
Фильтрует только сообщения (op='conversation') и красиво их форматирует.
"""
import json
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx, normalize_path, _ensure_allowed
)

# ─── Поиск инструментов для PDF ──────────────────────────────────────
def _find_pandoc() -> Optional[Path]:
    script_dir = Path(__file__).parent
    local_path = script_dir / "mcp_tools" / "pandoc" / "pandoc.exe"
    if local_path.exists():
        return local_path
    return shutil.which("pandoc")

def _find_wkhtmltopdf() -> Optional[Path]:
    script_dir = Path(__file__).parent
    local_path = script_dir / "mcp_tools" / "wkhtmltopdf" / "bin" / "wkhtmltopdf.exe"
    if local_path.exists():
        return local_path
    return shutil.which("wkhtmltopdf")

def is_pdf_available() -> Tuple[bool, str]:
    pandoc = _find_pandoc()
    wk = _find_wkhtmltopdf()
    if pandoc and wk:
        return True, "pandoc+wkhtmltopdf"
    return False, "no tools"

# ─── Основная функция экспорта ─────────────────────────────────────────
def export_dialog(dialog_id: Optional[str] = None,
                  format: str = "md",
                  output_path: Optional[str] = None,
                  include_metadata: bool = True,
                  max_entries: int = 2000) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    if not d_id or d_id == "default":
        return {"status": "error", "message": "No dialog ID specified"}

    # Получаем все записи диалога
    thread = conversation_memory.get_dialog_thread(dialog=d_id, limit=max_entries)
    if not thread or not thread.get("entries"):
        return {"status": "error", "message": f"No entries found for dialog {d_id}"}

    entries = thread["entries"]

    # Отбираем только записи с op='conversation' (сообщения)
    messages = []
    for e in entries:
        if e.get("op") == "conversation":
            # Извлекаем роль и текст
            paths = e.get("paths", {})
            role = paths.get("role", "unknown")
            text = e.get("context", "")
            ts = e.get("ts", "")
            messages.append({
                "timestamp": ts,
                "role": role,
                "text": text
            })

    if not messages:
        return {"status": "error", "message": "No conversation messages found (use log_conversation tool first)"}

    # Сортируем по времени
    messages.sort(key=lambda x: x["timestamp"])

    # Формируем Markdown
    md_lines = []
    if include_metadata:
        md_lines.append(f"# Dialog Export: {d_id}")
        md_lines.append(f"**Exported:** {datetime.now().isoformat()}")
        md_lines.append(f"**Messages:** {len(messages)}")
        md_lines.append("")

    for msg in messages:
        role_icon = "👤 **User:**" if msg["role"] == "user" else "🤖 **Assistant:**"
        md_lines.append(role_icon)
        md_lines.append(msg["text"])
        md_lines.append("")  # пустая строка после сообщения

    # Сохраняем Markdown
    if output_path is None:
        safe_dialog = d_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        output_path = f"dialog_export_{safe_dialog}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

    out_path = Path(normalize_path(output_path)).resolve()
    try:
        _ensure_allowed(out_path.parent, "export_dialog")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(md_lines))

    if format.lower() != "pdf":
        return {
            "status": "success",
            "format": "md",
            "output_path": str(out_path),
            "messages": len(messages),
            "dialog_id": d_id
        }

    # PDF conversion
    available, tool = is_pdf_available()
    if not available:
        return {
            "status": "pdf_unavailable",
            "message": "PDF conversion not available. Install pandoc+wkhtmltopdf or use format='md'.",
            "markdown_path": str(out_path)
        }

    pdf_path = out_path.with_suffix(".pdf").resolve()
    pandoc = _find_pandoc()
    wk = _find_wkhtmltopdf()
    if pandoc and wk:
        try:
            cmd = [str(pandoc), str(out_path), "-o", str(pdf_path),
                   "--to", "pdf", "--pdf-engine", str(wk)]
            subprocess.run(cmd, capture_output=True, check=True, timeout=60)
            return {
                "status": "success",
                "format": "pdf",
                "output_path": str(pdf_path),
                "messages": len(messages),
                "dialog_id": d_id
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"PDF conversion failed: {e}",
                "markdown_path": str(out_path)
            }
    else:
        return {"status": "error", "message": "PDF tools not found", "markdown_path": str(out_path)}

def register_export_tool(server: BaseMCPServer):
    server.register_tool("export_dialog", {
        "description": "Export conversation messages (user/assistant) to Markdown or PDF. Uses log_conversation entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dialog_id": {"type": "string"},
                "format": {"type": "string", "enum": ["md", "pdf"], "default": "md"},
                "output_path": {"type": "string"},
                "include_metadata": {"type": "boolean", "default": True},
                "max_entries": {"type": "integer", "default": 2000}
            }
        }
    }, lambda **kw: export_dialog(**kw))

if __name__ == "__main__":
    _log("Export dialog module v1.5 loaded.")