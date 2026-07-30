#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Setup Helper v9.1 (видимые ошибки pip, позапакетный fallback) – полная портативная установка всех зависимостей (включая когнитивные модули).
Все зависимости скачиваются в python_deps/, внешние инструменты – в tools/.
Работает полностью offline после первого скачивания.
"""
import os
import sys
import ast
import json
import shutil
import subprocess
import argparse
import time
import platform
import zipfile
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY_EXE = str(VENV / "Scripts" / "python.exe") if sys.platform == "win32" else str(VENV / "bin" / "python3")
PIP_CMD = [PY_EXE, "-m", "pip", "--no-input"]
DEPS_DIR = ROOT / "python_deps"
TOOLS_DIR = ROOT / "tools"
INSTALLERS_DIR = TOOLS_DIR / "installers"

# Пути к внешним инструментам (портативные)
TOOLS_PYTHON = TOOLS_DIR / "python" / "python.exe"
TOOLS_TESSERACT = TOOLS_DIR / "tesseract"
TOOLS_FFMPEG = TOOLS_DIR / "ffmpeg"
TOOLS_PANDOC = TOOLS_DIR / "pandoc"
TOOLS_WKHTMLTOPDF = TOOLS_DIR / "wkhtmltopdf"

# Добавляем инструменты в PATH для текущего процесса
for p in [TOOLS_PYTHON.parent, TOOLS_TESSERACT, TOOLS_FFMPEG, TOOLS_PANDOC, TOOLS_WKHTMLTOPDF]:
    if p.exists():
        os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")

# ========== ПОЛНЫЙ СПИСОК ЗАВИСИМОСТЕЙ ==========
BASE_DEPS = {
    # Базовые
    "pip", "setuptools", "wheel",
    # Сеть и системное
    "watchdog", "psutil", "requests", "xxhash", "cryptography", "keyring",
    # Парсинг и документы
    "beautifulsoup4", "feedparser", "icalendar", "openpyxl", "python-docx",
    "python-pptx", "pytesseract", "Pillow", "mutagen",
    # Базы данных и SQL
    "duckdb", "pyodbc", "sqlalchemy", "apscheduler",
    # Архивация
    "patool", "py7zr", "rarfile",
    # Веб-автоматизация
    "playwright",
    # Data science / ML (для RAG и эпизодической памяти)
    "pandas", "numpy", "scikit-learn", "sentence-transformers", "chromadb",
    # PDF и электронные книги
    "pypdf", "PyPDF2", "pdfplumber", "ebooklib",
    # Извлечение текста из веба
    "trafilatura", "readability-lxml", "html-table-takeout",
    # Память и утилиты
    "mempalace", "tiktoken",
    # Экспорт
    "pypandoc", "markdown", "tabulate",
    # Конфигурация и планирование
    "python-dotenv", "schedule",
    # MCP протокол + когнитивные плагины (граф знаний / планировщик)
    "mcp", "pydantic", "networkx",
    # XML/HTML парсер — требуется для readability-lxml и trafilatura
    "lxml",
    # Создание PDF из текста (mcp_pdf.create_pdf), офлайн
    "reportlab",
    # Рендер страниц PDF для OCR сканов (mcp_pdf), офлайн, чистый python
    "pypdfium2",
}

# Windows-only зависимости (ставятся только на Windows).
# pywin32 нужен mcp_calendar для интеграции с Outlook (win32com).
WINDOWS_DEPS = {"pywin32"}

# Зеркала PyPI
PIP_MIRRORS = [
    "",   # официальный
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.cloud.tencent.com/pypi/simple",
]

def get_base_python():
    """Возвращает путь к Python (системный или портативный)."""
    if TOOLS_PYTHON.exists():
        return str(TOOLS_PYTHON)
    sys_python = shutil.which("python")
    if sys_python:
        return sys_python
    return None

def load_dotenv_if_exists():
    try:
        from dotenv import load_dotenv
        env_file = ROOT / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass

# Карта: имя-импорта (как объявляют плагины) -> имя пакета PyPI.
# Без неё pip ставил бы НЕ те пакеты (docx, readability) или несуществующие.
IMPORT_TO_PYPI = {
    "bs4": "beautifulsoup4",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "PIL": "Pillow",
    "readability": "readability-lxml",
    "sklearn": "scikit-learn",
    "sentence_transformers": "sentence-transformers",
    "html_table_takeout": "html-table-takeout",
    "dotenv": "python-dotenv",
    "win32com": "pywin32",
    "yaml": "pyyaml",
}

# Имена внутренних плагинов (НЕ пакеты PyPI) — их нельзя передавать в pip.
INTERNAL_PLUGIN_NAMES = {
    "planning-engine", "task-manager", "world-model", "hypothesis-engine",
    "memory-graph", "episodic-memory", "reflection-engine", "goal-manager",
    "cognitive-core", "dialog-manager", "mempalace", "dialog-indexer",
}

def _normalize_dep(name: str) -> str:
    """Приводит имя зависимости плагина к корректному имени пакета PyPI."""
    name = name.strip()
    return IMPORT_TO_PYPI.get(name, name)

def find_plugin_deps():
    """Сканирует плагины на наличие секции __mcp_plugin__ и извлекает dependencies."""
    deps = set()
    for search_dir in [ROOT, ROOT / "mcp_plugins"]:
        if not search_dir.is_dir(): continue
        for py_file in search_dir.glob("*.py"):
            if py_file.name.startswith("_"): continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == "__mcp_plugin__":
                                if isinstance(node.value, ast.Dict):
                                    keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
                                    if "dependencies" in keys:
                                        idx = keys.index("dependencies")
                                        val = node.value.values[idx]
                                        if isinstance(val, ast.List):
                                            for elem in val.elts:
                                                # ast.Constant.value — единственный
                                                # корректный доступ начиная с Python 3.8.
                                                # Не трогаем устаревший .s (иначе
                                                # DeprecationWarning на 3.12+).
                                                raw = elem.value if isinstance(elem, ast.Constant) else ""
                                                if isinstance(raw, str):
                                                    pkg = raw.split("^")[0].split("=")[0].strip()
                                                    if not pkg or pkg in INTERNAL_PLUGIN_NAMES:
                                                        continue  # это внутренний плагин, не пакет
                                                    deps.add(_normalize_dep(pkg))
            except Exception:
                pass
    return deps

def get_full_deps(include_windows: bool = None):
    if include_windows is None:
        include_windows = (sys.platform == "win32")
    base = set(BASE_DEPS)
    if include_windows:
        base |= WINDOWS_DEPS
    return sorted(base | find_plugin_deps())

def run(cmd, check=False, env=None, timeout=600, silent=False):
    try:
        if not silent:
            print(f"[RUN] {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        if proc.stdout and not silent:
            print(proc.stdout)
        if proc.stderr and not silent:
            print(proc.stderr, file=sys.stderr)
        if check and proc.returncode != 0:
            sys.exit(proc.returncode)
        return proc
    except subprocess.TimeoutExpired:
        print("Command timed out")
        return type('Proc', (), {'returncode': 1, 'stdout': '', 'stderr': 'Timeout'})()
    except Exception as e:
        print(f"Run error: {e}")
        return type('Proc', (), {'returncode': 1, 'stdout': '', 'stderr': str(e)})()

def ensure_venv():
    venv_py = Path(PY_EXE)
    if VENV.exists():
        if not venv_py.exists():
            shutil.rmtree(VENV, ignore_errors=True)
        else:
            try:
                subprocess.run([str(venv_py), "--version"], capture_output=True, check=True, timeout=10)
            except Exception:
                print("⚠️ Virtual environment is broken. Recreating...")
                shutil.rmtree(VENV, ignore_errors=True)

    if not VENV.exists():
        base_py = get_base_python()
        if not base_py:
            print("❌ No Python found! Please run setup.bat and choose option 3 (download) then 4 (install) to set up portable Python.")
            sys.exit(1)
        print(f"📦 Creating virtual environment from {base_py}...")
        run([base_py, "-m", "venv", str(VENV)], check=True)
        if not venv_py.exists():
            print(f"❌ Error: {venv_py} not found")
            sys.exit(1)

def check_pip():
    if subprocess.run([PY_EXE, "-m", "pip", "--version"], capture_output=True).returncode != 0:
        print("❌ Pip not available in venv.")
        sys.exit(1)

def check_import(package: str) -> bool:
    import_name = package.split('[')[0].replace('-', '_')
    mapping = {
        "beautifulsoup4": "bs4",
        "Pillow": "PIL",
        "python-docx": "docx",
        "python-pptx": "pptx",
        "patool": None,
        "readability-lxml": "readability",
        "sentence-transformers": "sentence_transformers",
        "scikit-learn": "sklearn",
        "html-table-takeout": "html_table_takeout",
        "python-dotenv": "dotenv",
        "pywin32": "win32com",
        "PyPDF2": "PyPDF2",
        "mempalace": None,   # CLI-пакет, не импортируется напрямую
    }
    if package in mapping:
        if mapping[package] is None:
            return True
        import_name = mapping[package]
    if not import_name.isidentifier():
        return True  # пропускаем
    try:
        subprocess.run([PY_EXE, "-c", f"import {import_name}"], capture_output=True, check=True, timeout=30)
        return True
    except subprocess.CalledProcessError:
        return False
    except Exception:
        return True  # таймаут/прочее — не считаем отсутствующим

def _pip_try(base_cmd, packages):
    """Пробует команду pip по всем зеркалам. Возвращает True при успехе."""
    for mirror in PIP_MIRRORS:
        cmd = base_cmd.copy()
        if mirror:
            cmd += ["--index-url", mirror]
        if run(cmd + packages, silent=True).returncode == 0:
            return True
    return False

def _pip_per_package(base_cmd, packages, verb):
    """Позапакетный режим: один проблемный пакет не валит все остальные.
    Возвращает список пакетов, которые не удалось обработать."""
    failed = []
    for pkg in packages:
        if _pip_try(base_cmd, [pkg]):
            print(f"  [OK]   {pkg}")
        else:
            print(f"  [FAIL] {pkg}")
            failed.append(pkg)
    if failed:
        print(f"\n!!! Не удалось {verb}: {', '.join(failed)}")
        print("--- Подробная ошибка pip по первому проблемному пакету: ---")
        run(base_cmd + [failed[0]], silent=False)   # показываем реальную причину
        print("-----------------------------------------------------------")
        if sys.version_info >= (3, 13):
            print(f"ПОДСКАЗКА: у вас Python {sys.version_info.major}.{sys.version_info.minor} — "
                  "очень новый. Для части пакетов ещё нет собранных wheels под него.")
            print("Надёжный вариант: пункты меню 3 и 4 (портативный Python 3.10), затем пункт 2, затем 5/6.")
    return failed

def install_with_mirrors(packages, upgrade=False):
    if not packages:
        return True
    cmd_base = PIP_CMD + (["install", "--upgrade"] if upgrade else ["install"])
    if _pip_try(cmd_base, list(packages)):
        return True
    print("Пакетная установка не удалась — устанавливаю по одному...")
    return not _pip_per_package(cmd_base, list(packages), "установить")

def download_with_mirrors(packages):
    if not packages:
        return True
    DEPS_DIR.mkdir(exist_ok=True)
    cmd_base = PIP_CMD + ["download", "-d", str(DEPS_DIR), "--prefer-binary"]
    # Попытка 1: весь список одной командой (быстро, если всё доступно)
    if _pip_try(cmd_base, list(packages)):
        return True
    # Попытка 2: по одному пакету — видно, кто именно не скачивается
    print("Пакетная загрузка не удалась — скачиваю по одному (это дольше)...")
    failed = _pip_per_package(cmd_base, list(packages), "скачать")
    return not failed

def ensure_playwright_browsers():
    try:
        subprocess.run([PY_EXE, "-m", "playwright", "install", "chromium"], capture_output=True, check=True, timeout=300)
        print("✅ Playwright browser installed.")
    except Exception as e:
        print(f"⚠️ Playwright browser install failed: {e}")

def check_and_install_missing():
    ensure_venv()
    check_pip()
    load_dotenv_if_exists()
    deps = get_full_deps()
    missing = [dep for dep in deps if not check_import(dep)]
    if not missing:
        print("✅ All dependencies are already installed.")
        ensure_playwright_browsers()
        return

    print(f"⚠️ Missing: {', '.join(missing)}")
    local_whls = {whl.stem.split('-')[0].lower() for whl in DEPS_DIR.glob("*.whl")} if DEPS_DIR.exists() else set()

    offline_pkgs = [pkg for pkg in missing if pkg.lower() in local_whls]
    online_pkgs = [pkg for pkg in missing if pkg.lower() not in local_whls]

    if offline_pkgs:
        run(PIP_CMD + ["install", "--no-index", "--find-links", str(DEPS_DIR), "--no-build-isolation"] + offline_pkgs)
    if online_pkgs:
        install_with_mirrors(online_pkgs)

    ensure_playwright_browsers()
    print("✅ Done.")

def online_mode():
    ensure_venv()
    check_pip()
    install_with_mirrors(["pip", "setuptools", "wheel"], upgrade=True)
    all_deps = get_full_deps()
    ok = download_with_mirrors(all_deps)
    downloaded = len(list(DEPS_DIR.glob("*.whl"))) + len(list(DEPS_DIR.glob("*.tar.gz"))) if DEPS_DIR.exists() else 0
    if not ok:
        print(f"\n!!! Скачано частично: в {DEPS_DIR} лежит {downloaded} файлов.")
        print("Список проблемных пакетов и причина — выше. Остальное скачано и пригодно для пункта 6.")
        sys.exit(1)
    print(f"OK: скачано {len(all_deps)} пакетов ({downloaded} файлов) в {DEPS_DIR}")

def offline_mode():
    if not DEPS_DIR.exists() or not any(DEPS_DIR.glob("*.whl")):
        print("❌ python_deps is empty or missing. Run --online first to download packages.")
        sys.exit(1)
    ensure_venv()
    check_pip()
    run(PIP_CMD + ["install", "--no-index", "--find-links", str(DEPS_DIR), "--no-build-isolation"] + get_full_deps())
    ensure_playwright_browsers()
    print("✅ Installed all dependencies from local cache.")

def fix_config(config_path: str, python_exe: str):
    config_file = Path(config_path)
    if not config_file.exists():
        return 1
    backup = config_file.with_name(f"{config_file.name}.backup_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(config_file, backup)
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return 1

    counter = {"n": 0}
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if k == "command" and isinstance(v, str) and "python" in v.lower():
                    obj[k] = python_exe
                    counter["n"] += 1
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
    walk(data)
    config_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ Updated {counter['n']} entries in {config_file}")
    return 0

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--online", action="store_true", help="Скачать все зависимости в python_deps")
    group.add_argument("--offline", action="store_true", help="Установить зависимости из python_deps")
    group.add_argument("--check", action="store_true", help="Проверить и установить недостающие")
    parser.add_argument("--fix-config", nargs=2, metavar=("CONFIG_FILE", "PYTHON_EXE"), help="Исправить пути в JSON конфиге")
    args = parser.parse_args()

    if args.fix_config:
        sys.exit(fix_config(args.fix_config[0], args.fix_config[1]))
    elif args.online:
        online_mode()
    elif args.offline:
        offline_mode()
    elif args.check:
        check_and_install_missing()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()