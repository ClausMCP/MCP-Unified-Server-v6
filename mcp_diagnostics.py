#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_diagnostics.py — диагностика MCP Portable Server (пункт G в setup.bat).

Автономный скрипт: использует ТОЛЬКО стандартную библиотеку, поэтому
работает даже при полностью сломанных зависимостях.

Проверяет:
  1. Python и виртуальное окружение (.venv)
  2. Зависимости Python (ядро = FAIL, опциональные = WARN)
  3. Синтаксис всех модулей проекта (*.py, без импорта — не запускает потоки)
  4. Базы данных SQLite (existence + PRAGMA quick_check, read-only)
  5. Внешние инструменты (tesseract, ffmpeg, pandoc, wkhtmltopdf)
  6. Конфигурация (mcp_fs_server.py, .env, конфиг LM Studio)
  7. Подключение к сети (информационно, офлайн — не ошибка)

Коды выхода: 0 — всё в порядке (WARN допустимы), 1 — есть FAIL.
"""
import os
import sys
import ast
import json
import shutil
import socket
import sqlite3
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Консоль setup.bat переключена в UTF-8 (chcp 65001); подстрахуемся
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_counts = {"PASS": 0, "WARN": 0, "FAIL": 0}


def report(tag: str, name: str, detail: str = ""):
    _counts[tag] += 1
    line = f"[{tag}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def section(title: str):
    print()
    print(f"----- {title} -----")


# ─── 1. Python / venv ────────────────────────────────────────────────────────
def check_python():
    section("Python / окружение")
    v = sys.version_info
    if v >= (3, 10):
        report("PASS", f"Python {v.major}.{v.minor}.{v.micro}", sys.executable)
    else:
        report("FAIL", f"Python {v.major}.{v.minor}", "нужен Python 3.10+")

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    venv_dir = ROOT / ".venv"
    if in_venv:
        report("PASS", "Виртуальное окружение активно", sys.prefix)
    elif venv_dir.exists():
        report("WARN", "Запущен НЕ из .venv",
               f"ожидалось {venv_dir}, запущен {sys.executable}. "
               "Пакеты могут отличаться от установленных в venv")
    else:
        report("WARN", ".venv не найден", "создайте окружение (пункт 2 меню)")


# ─── 2. Зависимости ──────────────────────────────────────────────────────────
# (pip-имя, import-имя). Ядро — без него сервер не работает.
CORE_DEPS = [
    ("mcp", "mcp"), ("requests", "requests"), ("psutil", "psutil"),
    ("watchdog", "watchdog"), ("beautifulsoup4", "bs4"),
    ("openpyxl", "openpyxl"), ("python-docx", "docx"),
    ("python-pptx", "pptx"), ("pandas", "pandas"), ("pypdf", "pypdf"),
    ("Pillow", "PIL"), ("python-dotenv", "dotenv"), ("schedule", "schedule"),
    ("xxhash", "xxhash"), ("cryptography", "cryptography"),
    ("markdown", "markdown"), ("tabulate", "tabulate"), ("tiktoken", "tiktoken"),
]
# Опциональные: без них теряется часть функций, но сервер стартует.
OPTIONAL_DEPS = [
    ("sentence-transformers", "sentence_transformers", "RAG/эпизодическая память (пункт 8)"),
    ("chromadb", "chromadb", "RAG-хранилище (пункт 8)"),
    ("mempalace", "mempalace", "семантическая память memPalace"),
    ("duckdb", "duckdb", "db_tools"),
    ("pyodbc", "pyodbc", "db_tools: ODBC-источники"),
    ("pdfplumber", "pdfplumber", "извлечение таблиц из PDF"),
    ("PyPDF2", "PyPDF2", "старые PDF-функции"),
    ("ebooklib", "ebooklib", "чтение EPUB"),
    ("trafilatura", "trafilatura", "web_reader: извлечение текста"),
    ("readability-lxml", "readability", "web_reader: резервный парсер"),
    ("feedparser", "feedparser", "RSS"),
    ("icalendar", "icalendar", "календарь"),
    ("pytesseract", "pytesseract", "OCR (нужен ещё tesseract.exe)"),
    ("mutagen", "mutagen", "метаданные аудио"),
    ("py7zr", "py7zr", "архивы 7z"),
    ("rarfile", "rarfile", "архивы RAR"),
    ("patool", "patoolib", "универсальные архивы"),
    ("playwright", "playwright", "JS-рендеринг страниц"),
    ("keyring", "keyring", "хранение секретов"),
    ("pypandoc", "pypandoc", "экспорт диалогов в PDF"),
]


def _module_exists(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def check_deps():
    section("Зависимости Python (ядро)")
    missing_core = []
    for pip_name, imp in CORE_DEPS:
        if _module_exists(imp):
            report("PASS", pip_name)
        else:
            report("FAIL", pip_name, "не установлен")
            missing_core.append(pip_name)

    section("Зависимости Python (опциональные)")
    for pip_name, imp, why in OPTIONAL_DEPS:
        if _module_exists(imp):
            report("PASS", pip_name)
        else:
            report("WARN", pip_name, f"не установлен ({why})")

    if missing_core:
        print()
        print("  Установка недостающих (пункт 7 меню) или вручную:")
        print("  pip install " + " ".join(missing_core))


# ─── 3. Модули проекта (синтаксис, БЕЗ импорта) ──────────────────────────────
def check_project_modules():
    section("Модули проекта (синтаксис)")
    py_files = sorted(ROOT.glob("*.py"))
    if not py_files:
        report("FAIL", "*.py", f"в {ROOT} нет ни одного модуля проекта")
        return
    bad = 0
    for f in py_files:
        if f.name == Path(__file__).name:
            continue
        try:
            ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            report("FAIL", f.name, f"синтаксическая ошибка: строка {e.lineno}: {e.msg}")
            bad += 1
    if not bad:
        report("PASS", f"Синтаксис {len(py_files)} модулей", "ошибок нет")


# ─── 4. Базы данных ──────────────────────────────────────────────────────────
def _quick_check(db: Path) -> str:
    """Открывает БД строго read-only и выполняет PRAGMA quick_check."""
    uri = f"file:{db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return row[0] if row else "no result"
    finally:
        conn.close()


def check_databases():
    section("Базы данных SQLite")
    search_dirs = [ROOT]
    data_dir = Path(os.environ.get("MCP_DATA_DIR", str(ROOT / "data")))
    if data_dir.exists() and data_dir != ROOT:
        search_dirs.append(data_dir)
    rag_dir = Path(os.environ.get("MCP_RAG_DB_PATH", str(ROOT / "mcp_rag_db")))

    seen = set()
    dbs = []
    for d in search_dirs:
        for db in sorted(d.glob("*.db")):
            if db.resolve() not in seen:
                seen.add(db.resolve())
                dbs.append(db)
    if rag_dir.exists():
        for db in sorted(rag_dir.glob("*.sqlite3")) + sorted(rag_dir.glob("*.db")):
            if db.resolve() not in seen:
                seen.add(db.resolve())
                dbs.append(db)

    if not dbs:
        report("WARN", "Базы данных",
               "ни одной *.db не найдено — нормально для первого запуска")
        return

    for db in dbs:
        size_mb = db.stat().st_size / 1024 / 1024
        try:
            res = _quick_check(db)
            if res == "ok":
                report("PASS", db.name, f"{size_mb:.1f} MB, целостность ok")
            else:
                report("FAIL", db.name, f"quick_check: {res}")
        except Exception as e:
            report("FAIL", db.name, f"не открывается: {e}")


# ─── 5. Внешние инструменты ──────────────────────────────────────────────────
EXTERNAL_TOOLS = [
    ("tesseract", "OCR изображений и PDF"),
    ("ffmpeg", "медиа-метаданные и конвертация"),
    ("ffprobe", "анализ медиафайлов"),
    ("pandoc", "экспорт диалогов в PDF/DOCX"),
    ("wkhtmltopdf", "HTML -> PDF"),
    ("rclone", "облачные хранилища"),
]


def check_external_tools():
    section("Внешние инструменты")
    # setup.bat добавляет tools\* в PATH; проверим и портативную папку явно
    portable = ROOT / "tools"
    for exe, why in EXTERNAL_TOOLS:
        found = shutil.which(exe)
        if not found and portable.exists():
            suffix = ".exe" if os.name == "nt" else ""
            for cand in portable.rglob(exe + suffix):
                found = str(cand)
                break
        if found:
            report("PASS", exe, found)
        else:
            report("WARN", exe, f"не найден в PATH ({why})")


# ─── 6. Конфигурация ─────────────────────────────────────────────────────────
def check_config():
    section("Конфигурация")
    server_py = ROOT / "mcp_fs_server.py"
    if server_py.exists():
        report("PASS", "mcp_fs_server.py", "главный сервер на месте")
    else:
        report("FAIL", "mcp_fs_server.py", f"не найден в {ROOT}")

    env_file = ROOT / ".env"
    if env_file.exists():
        report("PASS", ".env", "найден")
    else:
        report("WARN", ".env", "не найден — создайте через пункт C меню")

    # Конфиг LM Studio (mcp.json / mcpServers)
    lm_candidates = [
        Path.home() / ".lmstudio" / "mcp.json",
        Path.home() / ".cache" / "lm-studio" / "mcp.json",
        ROOT / "mcpServers.json",
    ]
    found_cfg = None
    for cfg in lm_candidates:
        if cfg.exists():
            found_cfg = cfg
            break
    if not found_cfg:
        report("WARN", "Конфиг LM Studio", "mcp.json не найден — пункт B меню сгенерирует его")
        return
    try:
        data = json.loads(found_cfg.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", data)
        n = len(servers) if isinstance(servers, dict) else 0
        report("PASS", f"Конфиг LM Studio ({found_cfg.name})", f"{n} серверов, JSON валиден")
        # Проверяем, что пути в конфиге существуют
        if isinstance(servers, dict):
            for name, srv in servers.items():
                args = srv.get("args", []) if isinstance(srv, dict) else []
                for a in args:
                    if isinstance(a, str) and a.endswith(".py") and not Path(a).exists():
                        report("FAIL", f"Конфиг: сервер '{name}'",
                               f"путь не существует: {a} (пункт A/B меню исправит)")
    except json.JSONDecodeError as e:
        report("FAIL", f"Конфиг LM Studio ({found_cfg})", f"битый JSON: {e}")


# ─── 7. Сеть ─────────────────────────────────────────────────────────────────
def check_connectivity():
    section("Сеть (информационно)")
    if os.environ.get("MCP_OFFLINE_MODE", "").lower() == "force_offline":
        report("PASS", "Режим", "MCP_OFFLINE_MODE=force_offline — сеть отключена намеренно")
        return
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=3):
            report("PASS", "Интернет", "доступен")
    except OSError:
        report("WARN", "Интернет", "недоступен — офлайн-функции продолжат работать")
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        report("PASS", "HuggingFace", "офлайн-режим (модель эмбеддингов из локального кэша)")


# ─── main ────────────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 56)
    print("  MCP Portable Server — диагностика")
    print(f"  Каталог: {ROOT}")
    print("=" * 56)

    check_python()
    check_deps()
    check_project_modules()
    check_databases()
    check_external_tools()
    check_config()
    check_connectivity()

    print()
    print("=" * 56)
    print(f"  ИТОГ: PASS={_counts['PASS']}  WARN={_counts['WARN']}  FAIL={_counts['FAIL']}")
    if _counts["FAIL"]:
        print("  Есть критические проблемы — исправьте пункты [FAIL] выше.")
    elif _counts["WARN"]:
        print("  Критических проблем нет. [WARN] — необязательные компоненты.")
    else:
        print("  Всё в порядке.")
    print("=" * 56)
    return 1 if _counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
