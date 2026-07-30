#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP PDF v1.0 — работа с PDF-файлами (офлайн).

Чтение/извлечение текста, метаданные, слияние, извлечение диапазона страниц
(через pypdf) и создание PDF из текста (через reportlab, мягкий фоллбэк, если
не установлен). Интернет не требуется.

Инструменты: read_pdf, pdf_info, merge_pdfs, extract_pages, create_pdf.
"""
import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Any

from mcp_shared import BaseMCPServer, _log, normalize_path, _ensure_allowed

try:
    from pypdf import PdfReader, PdfWriter
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

# OCR-фоллбэк для отсканированных PDF: рендер страницы (pypdfium2) + распознавание
# (pytesseract). Оба опциональны — без них read_pdf просто не делает OCR.
try:
    import pypdfium2 as _pdfium
    import pytesseract as _tess
    from PIL import Image as _PILImage  # noqa
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False


def _locate_tesseract() -> Optional[str]:
    """
    Находит бинарник tesseract, даже если он не в PATH (частая ситуация на
    Windows). Порядок: MCP_TESSERACT_CMD -> PATH -> типичные места установки.
    """
    import shutil
    cand = os.environ.get("MCP_TESSERACT_CMD")
    if cand and os.path.exists(cand):
        return cand
    found = shutil.which("tesseract") or shutil.which("tesseract.exe")
    if found:
        return found
    home = os.environ.get("USERPROFILE", "")
    local = os.environ.get("LOCALAPPDATA", "")
    for p in [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(local, r"Tesseract-OCR\tesseract.exe") if local else "",
        os.path.join(local, r"Programs\Tesseract-OCR\tesseract.exe") if local else "",
        os.path.join(home, r"AppData\Local\Tesseract-OCR\tesseract.exe") if home else "",
        "/usr/bin/tesseract", "/usr/local/bin/tesseract",
    ]:
        if p and os.path.exists(p):
            return p
    return None


TESSERACT_CMD = None
if OCR_AVAILABLE:
    _tcmd = _locate_tesseract()
    if _tcmd:
        try:
            _tess.pytesseract.tesseract_cmd = _tcmd
            TESSERACT_CMD = _tcmd
        except Exception:
            pass

_INSTALL_HINT = ("OCR недоступен: не найден бинарник Tesseract. Установите Tesseract-OCR "
                 "(Windows: UB-Mannheim installer), при установке отметьте русский язык, "
                 "или укажите путь в переменной MCP_TESSERACT_CMD. Для кириллицы нужен пакет 'rus'.")

_MAX_TEXT = 200000
_OCR_MIN_CHARS = 10            # если на странице меньше — считаем её сканом
_DEFAULT_OCR_LANG = "rus+eng"  # документы часто на русском
_AUTO_OCR_CAP = 3              # макс. страниц для OCR внутри read_pdf (иначе таймаут; для полного — ocr_pdf)


def _ocr_page(pdf_path: str, page_index: int, lang: str, dpi: int) -> str:
    """Рендерит одну страницу PDF в изображение и распознаёт текст (OCR)."""
    pdf = _pdfium.PdfDocument(pdf_path)
    try:
        page = pdf[page_index]
        scale = max(1.0, dpi / 72.0)
        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        try:
            return _tess.image_to_string(pil, lang=lang) or ""
        except Exception:
            # язык не установлен — пробуем eng как запасной
            return _tess.image_to_string(pil, lang="eng") or ""
    finally:
        pdf.close()


import threading as _threading
import json as _json

_ocr_jobs = {}
_ocr_lock = _threading.Lock()


def _progress_path(output_path: str) -> str:
    return str(output_path) + ".progress.json"


def _write_progress(output_path: str, data: dict):
    try:
        with open(_progress_path(output_path), "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _ocr_to_file(pdf_path: str, output_path: str, lang: str, dpi: int,
                 start_page: int, end_page: int):
    """OCR страниц [start..end) с инкрементальной записью текста и прогресса в файл."""
    total = end_page - start_page
    done = 0
    try:
        with open(output_path, "w", encoding="utf-8") as out:
            for i in range(start_page, end_page):
                try:
                    txt = _ocr_page(pdf_path, i, lang, dpi)
                except Exception as e:
                    txt = f"[OCR error on page {i+1}: {e}]"
                out.write(f"\n--- Страница {i+1} ---\n{txt}\n")
                out.flush()
                done += 1
                _write_progress(output_path, {
                    "status": "running", "done": done, "total": total,
                    "output_path": output_path
                })
        _write_progress(output_path, {
            "status": "done", "done": done, "total": total, "output_path": output_path
        })
        with _ocr_lock:
            if output_path in _ocr_jobs:
                _ocr_jobs[output_path].update({"status": "done", "done": done})
    except Exception as e:
        _write_progress(output_path, {"status": "error", "error": str(e),
                                      "done": done, "total": total})
        with _ocr_lock:
            if output_path in _ocr_jobs:
                _ocr_jobs[output_path].update({"status": "error", "error": str(e)})


def ocr_status(output_path: str) -> Dict[str, Any]:
    """Статус фонового OCR по пути выходного файла."""
    pp = _progress_path(normalize_path(output_path)) if not str(output_path).endswith(".progress.json") else output_path
    try:
        with open(pp, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return {"status": "success", **data}
    except FileNotFoundError:
        return {"status": "error", "error": "прогресс не найден (задача не запускалась?)"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _need_pdf() -> Optional[Dict]:
    if not PDF_AVAILABLE:
        return {"status": "error", "error": "pypdf не установлен (pip install pypdf)"}
    return None


def read_pdf(path: str, start_page: int = 0, max_pages: Optional[int] = None,
             ocr: str = "auto", lang: str = _DEFAULT_OCR_LANG, dpi: int = 200) -> Dict[str, Any]:
    """
    Извлечь текст из PDF (постранично).

    ocr: 'auto' (по умолч.) — распознать страницы без текстового слоя (сканы);
         'force' — OCR для всех страниц; 'off' — без OCR.
    lang: языки tesseract (по умолч. 'rus+eng'; для русского нужен языковой пакет).
    """
    err = _need_pdf()
    if err:
        return err
    p = Path(normalize_path(path))
    _ensure_allowed(p, "read_pdf")
    if not p.exists():
        return {"status": "error", "error": f"файл не найден: {p}"}
    try:
        reader = PdfReader(str(p))
        total = len(reader.pages)
        end = total if max_pages is None else min(total, start_page + max_pages)
        pages = []
        chars = 0
        ocr_used = 0
        ocr_wanted = ocr in ("auto", "force")
        ocr_cap = _AUTO_OCR_CAP if ocr == "auto" else 10**9  # лимит OCR-страниц в read_pdf
        ocr_capped = False
        for i in range(max(0, start_page), end):
            txt = reader.pages[i].extract_text() or ""
            method = "text"
            need_ocr = (ocr == "force") or (ocr == "auto" and len(txt.strip()) < _OCR_MIN_CHARS)
            if need_ocr and ocr_wanted:
                if not OCR_AVAILABLE:
                    method = "text (ocr unavailable: установите pypdfium2+pytesseract+tesseract)"
                elif not TESSERACT_CMD:
                    method = "text (ocr unavailable: tesseract не найден)"
                elif ocr_used >= ocr_cap:
                    # не распознаём весь большой скан синхронно — это таймаут MCP
                    ocr_capped = True
                    method = "text (ocr skipped: используйте ocr_pdf для полного документа)"
                else:
                    try:
                        ocr_txt = _ocr_page(str(p), i, lang, dpi)
                        if len(ocr_txt.strip()) > len(txt.strip()):
                            txt, method = ocr_txt, "ocr"
                            ocr_used += 1
                    except Exception as e:
                        method = f"text (ocr failed: {e})"
            chars += len(txt)
            pages.append({"page": i + 1, "text": txt, "method": method})
            if chars > _MAX_TEXT:
                pages.append({"page": None, "text": "...[обрезано]", "method": "truncated"})
                break
        scanned = all(len((x.get("text") or "").strip()) < _OCR_MIN_CHARS
                      for x in pages if x.get("page"))
        note = None
        if ocr_capped:
            note = (f"Документ — многостраничный скан. read_pdf распознал первые {ocr_cap} стр.; "
                    f"для ПОЛНОГО OCR вызовите ocr_pdf('{p}') — он распознаёт в фоне в текстовый файл.")
        elif ocr in ("auto", "force") and ocr_used == 0 and scanned:
            if not OCR_AVAILABLE:
                note = ("Похоже на скан. OCR недоступен: установите pypdfium2, pytesseract и Tesseract-OCR "
                        "(+ языковой пакет 'rus' для кириллицы).")
            elif not TESSERACT_CMD:
                note = _INSTALL_HINT
        return {
            "status": "success", "file": str(p), "total_pages": total,
            "returned_pages": len([x for x in pages if x["page"]]),
            "ocr_pages": ocr_used, "tesseract": TESSERACT_CMD, "pages": pages,
            **({"note": note} if note else {}),
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def ocr_pdf(path: str, output_path: Optional[str] = None, lang: str = _DEFAULT_OCR_LANG,
            dpi: int = 200, start_page: int = 0, max_pages: Optional[int] = None,
            background: Optional[bool] = None) -> Dict[str, Any]:
    """
    OCR отсканированного PDF с записью текста в .txt файл.

    Для многостраничных сканов работает в ФОНЕ (иначе один синхронный вызов
    упирается в таймаут MCP): возвращает путь к выходному файлу сразу, а текст
    дописывается по мере распознавания. Прогресс — инструментом ocr_status.
    background=None — авто (фон, если страниц > 3).
    """
    err = _need_pdf()
    if err:
        return err
    if not OCR_AVAILABLE:
        return {"status": "error", "error": "OCR недоступен: установите pypdfium2, pytesseract и Tesseract-OCR."}
    if not TESSERACT_CMD:
        return {"status": "error", "error": _INSTALL_HINT}

    p = Path(normalize_path(path))
    _ensure_allowed(p, "ocr_pdf")
    if not p.exists():
        return {"status": "error", "error": f"файл не найден: {p}"}

    out = Path(normalize_path(output_path)) if output_path else p.with_suffix(".ocr.txt")
    _ensure_allowed(out, "ocr_pdf")
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        total = len(PdfReader(str(p)).pages)
    except Exception as e:
        return {"status": "error", "error": f"не удалось открыть PDF: {e}"}

    s = max(0, int(start_page))
    e = total if max_pages is None else min(total, s + int(max_pages))
    n = e - s
    if n <= 0:
        return {"status": "error", "error": "пустой диапазон страниц"}

    if background is None:
        background = n > 3

    if background:
        _write_progress(str(out), {"status": "running", "done": 0, "total": n, "output_path": str(out)})
        with _ocr_lock:
            _ocr_jobs[str(out)] = {"status": "running", "done": 0, "total": n}
        th = _threading.Thread(target=_ocr_to_file, args=(str(p), str(out), lang, dpi, s, e), daemon=True)
        th.start()
        return {
            "status": "started", "background": True, "output_path": str(out),
            "total_pages": n, "progress_path": _progress_path(str(out)),
            "message": f"OCR {n} стр. запущен в фоне. Текст пишется в {out}. "
                       f"Проверяйте прогресс: ocr_status('{out}'); по завершении читайте файл.",
        }

    # синхронно (мало страниц)
    _ocr_to_file(str(p), str(out), lang, dpi, s, e)
    try:
        text = open(out, encoding="utf-8").read()
    except Exception:
        text = ""
    return {"status": "success", "background": False, "output_path": str(out),
            "pages": n, "chars": len(text), "preview": text[:2000]}


def pdf_info(path: str) -> Dict[str, Any]:
    """Метаданные PDF: число страниц, шифрование, автор/заголовок и т.п."""
    err = _need_pdf()
    if err:
        return err
    p = Path(normalize_path(path))
    _ensure_allowed(p, "pdf_info")
    if not p.exists():
        return {"status": "error", "error": f"файл не найден: {p}"}
    try:
        reader = PdfReader(str(p))
        meta = reader.metadata or {}
        return {
            "status": "success",
            "file": str(p),
            "pages": len(reader.pages),
            "encrypted": bool(getattr(reader, "is_encrypted", False)),
            "size_bytes": p.stat().st_size,
            "metadata": {k.lstrip("/"): str(v) for k, v in dict(meta).items()},
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def merge_pdfs(paths: List[str], output: str) -> Dict[str, Any]:
    """Объединить несколько PDF в один (в указанном порядке)."""
    err = _need_pdf()
    if err:
        return err
    if not paths or len(paths) < 2:
        return {"status": "error", "error": "нужно минимум 2 PDF для слияния"}
    out = Path(normalize_path(output))
    _ensure_allowed(out, "merge_pdfs")
    try:
        writer = PdfWriter()
        added = 0
        for path in paths:
            p = Path(normalize_path(path))
            _ensure_allowed(p, "merge_pdfs")
            if not p.exists():
                return {"status": "error", "error": f"файл не найден: {p}"}
            reader = PdfReader(str(p))
            for page in reader.pages:
                writer.add_page(page)
                added += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as f:
            writer.write(f)
        return {"status": "success", "output": str(out), "merged_files": len(paths), "total_pages": added}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def extract_pages(path: str, start: int, end: int, output: str) -> Dict[str, Any]:
    """Извлечь диапазон страниц [start..end] (1-based, включительно) в новый PDF."""
    err = _need_pdf()
    if err:
        return err
    p = Path(normalize_path(path))
    _ensure_allowed(p, "extract_pages")
    out = Path(normalize_path(output))
    _ensure_allowed(out, "extract_pages")
    if not p.exists():
        return {"status": "error", "error": f"файл не найден: {p}"}
    try:
        reader = PdfReader(str(p))
        total = len(reader.pages)
        s = max(1, int(start))
        e = min(total, int(end))
        if s > e:
            return {"status": "error", "error": f"неверный диапазон {start}-{end} (всего {total} стр.)"}
        writer = PdfWriter()
        for i in range(s - 1, e):
            writer.add_page(reader.pages[i])
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as f:
            writer.write(f)
        return {"status": "success", "output": str(out), "pages": f"{s}-{e}", "count": e - s + 1}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def create_pdf(text: str, output: str, title: Optional[str] = None) -> Dict[str, Any]:
    """Создать простой PDF из текста (через reportlab; офлайн)."""
    out = Path(normalize_path(output))
    _ensure_allowed(out, "create_pdf")
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import cm
    except ImportError:
        return {"status": "error", "error": "reportlab не установлен (нужен для создания PDF). "
                                            "Установите через установщик (он в зависимостях)."}
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        c = canvas.Canvas(str(out), pagesize=A4)
        width, height = A4
        margin = 2 * cm
        y = height - margin
        line_h = 14
        if title:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(margin, y, title[:120])
            y -= line_h * 2
        c.setFont("Helvetica", 11)
        max_chars = int((width - 2 * margin) / 6)  # грубая оценка символов в строке
        for raw_line in text.split("\n"):
            # перенос длинных строк
            chunks = [raw_line[i:i + max_chars] for i in range(0, max(1, len(raw_line)), max_chars)] or [""]
            for chunk in chunks:
                if y < margin:
                    c.showPage()
                    c.setFont("Helvetica", 11)
                    y = height - margin
                c.drawString(margin, y, chunk)
                y -= line_h
        c.save()
        return {"status": "success", "output": str(out), "size_bytes": out.stat().st_size}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def images_to_pdf(image_paths=None, output: str = "", folder: Optional[str] = None) -> Dict[str, Any]:
    """
    Объединяет изображения (сканы) в один PDF. Можно передать список image_paths
    или папку folder (берутся все картинки, сортируются по имени). Офлайн (PIL).
    """
    try:
        from PIL import Image
    except ImportError:
        return {"status": "error", "error": "Pillow не установлен. Установите: pip install pillow"}
    if not output:
        return {"status": "error", "error": "не указан output (путь к итоговому PDF)"}

    exts = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
    paths = []
    if folder:
        d = Path(normalize_path(folder))
        _ensure_allowed(d, "images_to_pdf")
        if not d.is_dir():
            return {"status": "error", "error": f"папка не найдена: {d}"}
        paths = sorted([p for p in d.iterdir() if p.suffix.lower() in exts], key=lambda x: x.name.lower())
    elif image_paths:
        for ip in image_paths:
            p = Path(normalize_path(ip))
            _ensure_allowed(p, "images_to_pdf")
            paths.append(p)
    if not paths:
        return {"status": "error", "error": "не найдено изображений для объединения"}

    out = Path(normalize_path(output))
    _ensure_allowed(out, "images_to_pdf")
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        imgs = []
        for p in paths:
            if not p.exists():
                return {"status": "error", "error": f"файл не найден: {p}"}
            im = Image.open(p)
            imgs.append(im.convert("RGB") if im.mode != "RGB" else im)
        first, rest = imgs[0], imgs[1:]
        first.save(str(out), "PDF", save_all=True, append_images=rest)
        return {"status": "success", "output": str(out), "pages": len(imgs),
                "size_bytes": out.stat().st_size,
                "files": [p.name for p in paths]}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def register_tools(server: BaseMCPServer):
    server.register_tool("read_pdf", {
        "description": "Extract text from a PDF, page by page (offline). Auto-OCRs scanned pages that have "
                       "no text layer (needs pypdfium2+pytesseract+tesseract; lang default 'rus+eng').",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_page": {"type": "integer", "description": "0-based start page (default 0)"},
            "max_pages": {"type": "integer", "description": "Max pages to read (default all)"},
            "ocr": {"type": "string", "description": "'auto' (default), 'force', or 'off'"},
            "lang": {"type": "string", "description": "OCR languages (default 'rus+eng')"},
            "dpi": {"type": "integer", "description": "OCR render DPI (default 200)"}
        }, "required": ["path"]}
    }, lambda **kw: read_pdf(kw["path"], kw.get("start_page", 0), kw.get("max_pages"),
                             kw.get("ocr", "auto"), kw.get("lang", _DEFAULT_OCR_LANG), kw.get("dpi", 200)))

    server.register_tool("ocr_pdf", {
        "description": "OCR a scanned PDF to a .txt file. Multi-page scans run in BACKGROUND (returns "
                       "output path immediately; avoids MCP timeout). Check progress with ocr_status, then "
                       "read the output file. Needs Tesseract (+ 'rus' pack for Cyrillic).",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "output_path": {"type": "string", "description": "Where to write text (default <pdf>.ocr.txt)"},
            "lang": {"type": "string", "description": "OCR languages (default 'rus+eng')"},
            "dpi": {"type": "integer", "description": "Render DPI (default 200)"},
            "start_page": {"type": "integer"},
            "max_pages": {"type": "integer"},
            "background": {"type": "boolean", "description": "Force background on/off (default auto: on if >3 pages)"}
        }, "required": ["path"]}
    }, lambda **kw: ocr_pdf(kw["path"], kw.get("output_path"), kw.get("lang", _DEFAULT_OCR_LANG),
                            kw.get("dpi", 200), kw.get("start_page", 0), kw.get("max_pages"), kw.get("background")))

    server.register_tool("ocr_status", {
        "description": "Check progress of a background OCR job by its output file path "
                       "(returns done/total/status). When status='done', read the output .txt file.",
        "inputSchema": {"type": "object", "properties": {
            "output_path": {"type": "string"}
        }, "required": ["output_path"]}
    }, lambda **kw: ocr_status(kw["output_path"]))

    server.register_tool("pdf_info", {
        "description": "PDF metadata: page count, encryption, author/title, size.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    }, lambda **kw: pdf_info(kw["path"]))

    server.register_tool("merge_pdfs", {
        "description": "Merge multiple PDFs into one (in order).",
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "output": {"type": "string"}
        }, "required": ["paths", "output"]}
    }, lambda **kw: merge_pdfs(kw["paths"], kw["output"]))

    server.register_tool("extract_pages", {
        "description": "Extract a page range [start..end] (1-based, inclusive) into a new PDF.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "start": {"type": "integer"},
            "end": {"type": "integer"}, "output": {"type": "string"}
        }, "required": ["path", "start", "end", "output"]}
    }, lambda **kw: extract_pages(kw["path"], kw["start"], kw["end"], kw["output"]))

    server.register_tool("images_to_pdf", {
        "description": "Combine images (scans) into a single PDF. Pass image_paths (list) or folder "
                       "(all images, sorted by name). Offline (Pillow).",
        "inputSchema": {"type": "object", "properties": {
            "image_paths": {"type": "array", "items": {"type": "string"}},
            "folder": {"type": "string", "description": "Folder with images (alternative to image_paths)"},
            "output": {"type": "string", "description": "Output PDF path"}
        }, "required": ["output"]}
    }, lambda **kw: images_to_pdf(kw.get("image_paths"), kw.get("output", ""), kw.get("folder")))

    server.register_tool("create_pdf", {
        "description": "Create a simple PDF from text (offline, reportlab).",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string"}, "output": {"type": "string"},
            "title": {"type": "string"}
        }, "required": ["text", "output"]}
    }, lambda **kw: create_pdf(kw["text"], kw["output"], kw.get("title")))


__mcp_plugin__ = {
    "name": "pdf",
    "version": "1.1.0",
    "description": "Offline PDF: read_pdf (+OCR for scans), ocr_pdf, pdf_info, merge_pdfs, extract_pages, create_pdf",
    "dependencies": ["pypdf"],
    "on_load": lambda: _log("[pdf] v1.1 loaded — tools: read_pdf, ocr_pdf, pdf_info, merge_pdfs, extract_pages, create_pdf"),
}

if __name__ == "__main__":
    print(json.dumps({"available": PDF_AVAILABLE}, indent=2))
