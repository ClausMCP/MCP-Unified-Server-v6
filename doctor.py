#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doctor.py — авто-проверка и починка окружения проекта.

Запуск:
  python doctor.py            — только отчёт (ничего не меняет);
  python doctor.py --fix      — создать недостающие папки и прописать .env;
  python doctor.py --install  — то же + установить недостающие зависимости (нужен интернет).

Безопасно: только создаёт недостающее и ДОПОЛНЯЕТ .env (существующие значения не
трогает). Ничего не удаляет.
"""
import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _data_dir() -> Path:
    try:
        from mcp_shared import conversation_memory
        p = getattr(conversation_memory, "db_path", None)
        if p:
            return Path(p).resolve().parent
    except Exception:
        pass
    return Path(os.environ.get("MCP_DATA_DIR", ROOT / "data")).resolve()


def _required_dirs(data: Path):
    return {
        "data": data,
        "workspace": Path(os.environ.get("MCP_CODE_EXEC_DIR", data / "workspace")),
        "backups": Path(os.environ.get("MCP_BACKUP_DIR", data / "backups")),
    }


def locate_tesseract():
    try:
        from mcp_shared import locate_tesseract as lt
        return lt()
    except Exception:
        import shutil
        return shutil.which("tesseract") or shutil.which("tesseract.exe")


def check_dirs(fix: bool):
    data = _data_dir()
    report = []
    for name, d in _required_dirs(data).items():
        exists = d.exists()
        if not exists and fix:
            try:
                d.mkdir(parents=True, exist_ok=True)
                report.append((name, str(d), "создана"))
            except Exception as e:
                report.append((name, str(d), f"ошибка: {e}"))
        else:
            report.append((name, str(d), "есть" if exists else "ОТСУТСТВУЕТ"))
    return report


def _read_env(env_path: Path) -> dict:
    data = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    return data


def check_env(fix: bool):
    env_path = ROOT / ".env"
    existing = _read_env(env_path)
    report = []
    additions = {}

    # путь к tesseract, если найден и ещё не прописан
    tcmd = locate_tesseract()
    if tcmd and "MCP_TESSERACT_CMD" not in existing:
        additions["MCP_TESSERACT_CMD"] = tcmd

    if fix:
        if not env_path.exists():
            header = [
                "# Конфигурация MCP-ассистента (создано doctor.py)",
                "# Раскомментируйте и заполните нужное:",
                "# MCP_SEARXNG_URL=http://localhost:8888",
                "# BRAVE_SEARCH_API_KEY=",
                "# MCP_OFFLINE_MODE=auto",
                "",
            ]
            env_path.write_text("\n".join(header), encoding="utf-8")
        if additions:
            with open(env_path, "a", encoding="utf-8") as f:
                for k, v in additions.items():
                    f.write(f"{k}={v}\n")
            for k, v in additions.items():
                report.append((k, v, "прописано"))
        else:
            report.append(("(.env)", str(env_path), "ничего добавлять не нужно"))
    else:
        if additions:
            for k, v in additions.items():
                report.append((k, v, "будет прописано при --fix"))
        else:
            report.append(("(.env)", str(env_path), "ок" if env_path.exists() else "будет создан при --fix"))
    return report, tcmd


def check_deps(install: bool):
    try:
        import mcp_setup as s
        deps = sorted(s.get_full_deps(include_windows=(os.name == "nt")))
        inv = {v: k for k, v in getattr(s, "IMPORT_TO_PYPI", {}).items()}
    except Exception as e:
        return [("mcp_setup", "", f"не удалось получить список: {e}")], []

    missing = []
    for dep in deps:
        imp = inv.get(dep, dep.replace("-", "_"))
        try:
            __import__(imp)
        except Exception:
            # пробуем нижний регистр как запасной вариант
            try:
                __import__(imp.lower())
            except Exception:
                missing.append(dep)

    report = []
    if not missing:
        report.append(("зависимости", f"{len(deps)} проверено", "все на месте"))
        return report, missing

    report.append(("отсутствуют", ", ".join(missing[:30]), f"{len(missing)} шт."))
    if install:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", *missing],
                           check=False)
            report.append(("pip install", "", "выполнено (проверьте вывод выше)"))
        except Exception as e:
            report.append(("pip install", "", f"ошибка: {e}"))
    else:
        report.append(("подсказка", "", "установите: пункт меню 7, либо python doctor.py --install"))
    return report, missing


def check_db():
    try:
        from mcp_shared import conversation_memory  # импорт инициирует миграции
        _ = conversation_memory.db_path
        return [("память/БД", "", "инициализирована (миграции применены)")]
    except Exception as e:
        return [("память/БД", "", f"ошибка: {e}")]


def _tessdata_has_rus():
    """Проверяет наличие языкового пакета 'rus' в tessdata (без запуска tesseract)."""
    tcmd = locate_tesseract()
    candidates = []
    pref = os.environ.get("TESSDATA_PREFIX")
    if pref:
        candidates.append(pref if pref.rstrip("\\/").endswith("tessdata") else os.path.join(pref, "tessdata"))
    if tcmd:
        candidates.append(os.path.join(os.path.dirname(tcmd), "tessdata"))
    candidates += ["/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata",
                   "/usr/share/tessdata"]
    for d in candidates:
        if d and os.path.exists(os.path.join(d, "rus.traineddata")):
            return True, d
    return False, (candidates[0] if candidates else None)


def _embedding_model_cached():
    """Скачана ли модель эмбеддингов для RAG (проверка кэша на диске, без загрузки)."""
    model = os.environ.get("MCP_RAG_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    home = Path.home()
    slug = model.replace("/", "_")
    paths = [
        # кэш sentence-transformers (старый формат)
        home / ".cache" / "torch" / "sentence_transformers" / f"sentence-transformers_{slug}",
        home / ".cache" / "torch" / "sentence_transformers" / slug,
        # кэш HuggingFace hub
        home / ".cache" / "huggingface" / "hub" / f"models--sentence-transformers--{slug}",
        home / ".cache" / "huggingface" / "hub" / f"models--{slug}",
    ]
    for env in ("SENTENCE_TRANSFORMERS_HOME", "HF_HOME", "TRANSFORMERS_CACHE"):
        v = os.environ.get(env)
        if v:
            paths.append(Path(v) / f"sentence-transformers_{slug}")
            paths.append(Path(v) / "hub" / f"models--sentence-transformers--{slug}")
    for p in paths:
        if p.exists():
            return True, str(p)
    return False, model


def check_offline_readiness():
    """Что из ОНЛАЙН-зависимого уже собрано на диск (для ухода в офлайн)."""
    rows = []

    # 1. Tesseract + язык rus
    tcmd = locate_tesseract()
    if not tcmd:
        rows.append(("OCR сканов", "tesseract не найден", "НЕ готово — пункт I / install_ocr.py"))
    else:
        has_rus, td = _tessdata_has_rus()
        if has_rus:
            rows.append(("OCR сканов (рус)", td, "готово"))
        else:
            rows.append(("OCR сканов", "нет пакета rus", "частично — англ. ок, рус нет (install_ocr.py)"))

    # 2. Модель эмбеддингов для RAG/семантики
    try:
        import sentence_transformers  # noqa: только проверка наличия пакета
        cached, where = _embedding_model_cached()
        if cached:
            rows.append(("RAG: модель эмбеддингов", where, "готово (скачана)"))
        else:
            rows.append(("RAG: модель эмбеддингов", where,
                         "НЕ скачана — нужен интернет 1 раз (первый запуск RAG)"))
    except Exception:
        rows.append(("RAG: эмбеддинги", "sentence-transformers", "пакет не установлен (опц.)"))

    # 3. chromadb (хранилище RAG)
    try:
        import chromadb  # noqa
        rag_db = os.environ.get("MCP_RAG_DB_PATH", "./mcp_rag_db")
        collected = os.path.exists(rag_db)
        rows.append(("RAG: хранилище", rag_db, "есть данные" if collected else "пусто (нечего искать офлайн)"))
    except Exception:
        rows.append(("RAG: chromadb", "", "пакет не установлен (опц.)"))

    # 4. Playwright (рендеринг JS-страниц)
    try:
        import playwright  # noqa
        rows.append(("Playwright (JS-страницы)", "", "пакет есть (браузер: playwright install chromium)"))
    except Exception:
        rows.append(("Playwright", "", "не установлен (нужен для JS-страниц)"))

    # 5. Кэш веб-поиска (что уже собрано и доступно офлайн)
    cache_db = Path.home() / ".mcp_search_cache.db"
    if cache_db.exists():
        try:
            import sqlite3
            n = sqlite3.connect(str(cache_db)).execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
            rows.append(("Кэш веб-поиска", f"{n} записей", "есть (доступно офлайн)"))
        except Exception:
            rows.append(("Кэш веб-поиска", str(cache_db), "есть"))
    else:
        rows.append(("Кэш веб-поиска", "", "пуст (поиск офлайн вернёт только из памяти/RAG)"))

    return rows


def _print(title, rows):
    print(f"\n=== {title} ===")
    for r in rows:
        name, val, status = r
        v = f" [{val}]" if val else ""
        print(f"  {status:32} {name}{v}")


def main(argv):
    fix = "--fix" in argv or "--install" in argv
    install = "--install" in argv
    offline_only = "--offline" in argv

    if offline_only:
        print("=== doctor.py — офлайн-готовность ===")
        _print("Что собрано для офлайн-работы", check_offline_readiness())
        print("\n  (что 'НЕ готово' — соберите при включённом интернете заранее)")
        return 0

    mode = "ИСПРАВЛЕНИЕ" if fix else "ОТЧЁТ (без изменений)"
    print(f"=== doctor.py — режим: {mode} ===")

    _print("Папки", check_dirs(fix))
    env_rows, tcmd = check_env(fix)
    _print("Конфигурация (.env)", env_rows)
    print(f"\n  tesseract: {tcmd or 'НЕ найден — OCR сканов недоступен (см. пункт I / install_ocr.py)'}")
    _print("База данных", check_db())
    dep_rows, missing = check_deps(install)
    _print("Зависимости", dep_rows)
    _print("Офлайн-готовность (онлайн-зависимое на диске)", check_offline_readiness())

    print("\n=== Итог ===")
    if fix:
        print("  Папки и .env приведены в порядок.")
    else:
        print("  Это был отчёт. Запустите 'python doctor.py --fix' для авто-починки,")
        print("  или 'python doctor.py --install' чтобы ещё и доустановить зависимости.")
    if missing and not install:
        print(f"  Не хватает зависимостей: {len(missing)} (пункт 7 меню или --install).")
    print("  Офлайн-готовность: 'python doctor.py --offline' (что собрать заранее при интернете).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
