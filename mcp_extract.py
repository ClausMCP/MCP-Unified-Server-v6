#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Extract v1.0 — универсальное извлечение текста из любого файла (офлайн).

`extract_any_text(path)` сам выбирает нужную читалку по типу файла:
  • PDF        -> read_pdf (с авто-OCR сканов; для больших сканов подскажет ocr_pdf)
  • изображения-> OCR (eng+rus)
  • .docx      -> текст документа
  • .xlsx/.xls -> таблица как markdown
  • .txt/.md/.csv/.json/.log/код -> как есть
Это закрывает запрос «извлеки текст из чего угодно» одним инструментом.
"""
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional

from mcp_shared import BaseMCPServer, _log, normalize_path, _ensure_allowed

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
_TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml",
             ".py", ".js", ".ts", ".html", ".css", ".ini", ".cfg", ".sql", ".sh", ".bat"}
_MAX_TEXT = 200000


def extract_any_text(path: str, ocr: str = "auto", lang: str = "rus+eng") -> Dict[str, Any]:
    """Извлекает текст из файла любого поддерживаемого типа (PDF/изображение/docx/xlsx/текст)."""
    p = Path(normalize_path(path))
    _ensure_allowed(p, "extract_any_text")
    if not p.exists():
        return {"status": "error", "error": f"файл не найден: {p}"}
    ext = p.suffix.lower()

    try:
        # PDF
        if ext == ".pdf":
            from mcp_pdf import read_pdf
            r = read_pdf(str(p), ocr=ocr, lang=lang)
            if r.get("status") != "success":
                return {"status": "error", "type": "pdf", "error": r.get("error")}
            text = "\n".join(pg.get("text", "") for pg in r.get("pages", []) if pg.get("page"))
            out = {"status": "success", "type": "pdf", "file": str(p),
                   "pages": r.get("total_pages"), "ocr_pages": r.get("ocr_pages", 0),
                   "text": text[:_MAX_TEXT]}
            if r.get("note"):
                out["note"] = r["note"]
            return out

        # Изображения -> OCR
        if ext in _IMAGE_EXT:
            from mcp_fs_media import extract_text_from_image
            r = extract_text_from_image(str(p), lang=lang)
            if r.get("error"):
                return {"status": "error", "type": "image", "error": r["error"]}
            return {"status": "success", "type": "image", "file": str(p),
                    "method": "ocr", "text": (r.get("text") or "")[:_MAX_TEXT]}

        # Word
        if ext == ".docx":
            from mcp_office_reader import docx_to_markdown
            r = docx_to_markdown(str(p))
            text = r.get("markdown") or r.get("text") or r.get("content") or ""
            return {"status": "success", "type": "docx", "file": str(p), "text": text[:_MAX_TEXT]}

        # Excel (через read_excel — устойчиво; excel_to_markdown бывает падает)
        if ext in (".xlsx", ".xls", ".xlsm"):
            from mcp_office_reader import read_excel
            r = read_excel(str(p))
            if r.get("error"):
                return {"status": "error", "type": "excel", "error": r["error"]}
            headers = r.get("headers") or []
            lines = []
            if headers:
                lines.append("\t".join(str(h) for h in headers))
            for row in (r.get("data") or []):
                if isinstance(row, dict):
                    lines.append("\t".join(str(row.get(h, "")) for h in headers) if headers
                                 else "\t".join(str(v) for v in row.values()))
                elif isinstance(row, (list, tuple)):
                    lines.append("\t".join(str(v) for v in row))
                else:
                    lines.append(str(row))
            text = "\n".join(lines)
            return {"status": "success", "type": "excel", "file": str(p),
                    "sheet": r.get("sheet"), "text": text[:_MAX_TEXT]}

        # Текстовые/код
        if ext in _TEXT_EXT or ext == "":
            try:
                data = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                data = p.read_bytes()[:_MAX_TEXT].decode("utf-8", "replace")
            return {"status": "success", "type": "text", "file": str(p), "text": data[:_MAX_TEXT]}

        # Неизвестный тип — пробуем как текст
        try:
            data = p.read_text(encoding="utf-8", errors="replace")
            return {"status": "success", "type": "unknown-as-text", "file": str(p),
                    "text": data[:_MAX_TEXT]}
        except Exception:
            return {"status": "error", "type": ext or "unknown",
                    "error": f"тип {ext} не поддерживается для извлечения текста"}
    except ImportError as e:
        return {"status": "error", "error": f"нужный модуль недоступен: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def batch_extract_text(folder: str, output_dir: Optional[str] = None, recursive: bool = False,
                       ocr: str = "auto", lang: str = "rus+eng", max_files: int = 500) -> Dict[str, Any]:
    """
    Извлекает текст из всех поддерживаемых файлов папки в .txt (по файлу на каждый),
    возвращает сводку. Рекурсивно по recursive=true. Не блокирует на большой папке —
    результаты пишутся на диск, в ответе только сводка.
    """
    d = Path(normalize_path(folder))
    _ensure_allowed(d, "batch_extract_text")
    if not d.is_dir():
        return {"status": "error", "error": f"папка не найдена: {d}"}
    out_dir = Path(normalize_path(output_dir)) if output_dir else (d / "_extracted_text")
    _ensure_allowed(out_dir, "batch_extract_text")
    out_dir.mkdir(parents=True, exist_ok=True)

    supported = {".pdf", ".docx", ".xlsx", ".xls", ".xlsm"} | _IMAGE_EXT | _TEXT_EXT
    files = (d.rglob("*") if recursive else d.iterdir())
    done, skipped, failed = [], [], []
    count = 0
    for f in sorted(files, key=lambda x: str(x).lower()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in supported:
            skipped.append(f.name)
            continue
        if count >= max_files:
            break
        count += 1
        r = extract_any_text(str(f), ocr=ocr, lang=lang)
        if r.get("status") == "success":
            txt_out = out_dir / (f.stem + ".txt")
            try:
                txt_out.write_text(r.get("text", ""), encoding="utf-8")
                done.append({"file": f.name, "out": txt_out.name, "type": r.get("type"),
                             "chars": len(r.get("text", ""))})
            except Exception as e:
                failed.append({"file": f.name, "error": str(e)})
        else:
            failed.append({"file": f.name, "error": r.get("error")})
    return {
        "status": "success", "folder": str(d), "output_dir": str(out_dir),
        "extracted": len(done), "failed": len(failed), "skipped_types": len(skipped),
        "files": done, "failures": failed[:20],
    }


def register_tools(server: BaseMCPServer):
    server.register_tool("batch_extract_text", {
        "description": "Extract text from ALL supported files in a folder into .txt files (one per file) "
                       "and return a summary. recursive=true to walk subfolders. OCRs scans/images. Offline.",
        "inputSchema": {"type": "object", "properties": {
            "folder": {"type": "string"},
            "output_dir": {"type": "string", "description": "Where to write .txt (default <folder>/_extracted_text)"},
            "recursive": {"type": "boolean"},
            "ocr": {"type": "string", "description": "'auto' (default), 'force', 'off'"},
            "lang": {"type": "string", "description": "OCR languages (default 'rus+eng')"},
            "max_files": {"type": "integer", "description": "Safety cap (default 500)"}
        }, "required": ["folder"]}
    }, lambda **kw: batch_extract_text(kw["folder"], kw.get("output_dir"), kw.get("recursive", False),
                                       kw.get("ocr", "auto"), kw.get("lang", "rus+eng"), kw.get("max_files", 500)))

    server.register_tool("extract_any_text", {
        "description": "Universal text extraction from ANY file: PDF (auto-OCR scans), images (OCR), "
                       ".docx, .xlsx, and text/code files. One tool for 'extract all text'. Offline. "
                       "For big scanned PDFs it extracts what it can and points to ocr_pdf for the full doc.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "ocr": {"type": "string", "description": "PDF/image OCR mode: 'auto' (default), 'force', 'off'"},
            "lang": {"type": "string", "description": "OCR languages (default 'rus+eng')"}
        }, "required": ["path"]}
    }, lambda **kw: extract_any_text(kw["path"], kw.get("ocr", "auto"), kw.get("lang", "rus+eng")))


__mcp_plugin__ = {
    "name": "extract",
    "version": "1.1.0",
    "description": "Universal text extraction (extract_any_text, batch_extract_text) across PDF/image/docx/xlsx/text",
    "dependencies": [],
    "on_load": lambda: _log("[extract] v1.1 loaded — tools: extract_any_text, batch_extract_text"),
}

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(extract_any_text(sys.argv[1]), ensure_ascii=False, indent=2)[:2000])
