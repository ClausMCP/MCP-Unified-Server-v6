#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install_ocr.py — установка OCR (Tesseract) и языковых пакетов для офлайн-работы.

Что делает:
  --check            показать статус: найден ли tesseract, какие языки есть;
  (по умолчанию)     скачать языковые пакеты rus+eng+osd в tessdata;
  --langs rus,eng,…  скачать указанные языки;
  --all              скачать ВСЕ языки (на всякий случай; это ~1 ГБ+);
  --installer        скачать установщик Tesseract для Windows (запустить вручную).

Требует интернет ОДИН РАЗ (при скачивании). После этого OCR работает офлайн.
Языковые файлы берутся из официального репозитория tesseract-ocr/tessdata_fast.

ВНИМАНИЕ: установка самого бинарника Tesseract на Windows — это запуск .exe
(возможно, с правами администратора). Скрипт может скачать установщик, но
запускать его, как правило, нужно вручную. Языковые .traineddata скрипт
раскладывает в папку tessdata автоматически.
"""
import os
import sys
import json
import ssl
import urllib.request

TESSDATA_API = "https://api.github.com/repos/tesseract-ocr/tessdata_fast/contents"
TESSDATA_RAW = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main"
# Зеркало установщика для Windows (UB Mannheim). Версия может меняться — при
# ошибке скачайте установщик вручную со страницы UB-Mannheim/tesseract.
WIN_INSTALLER = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.3.20231005.exe"

DEFAULT_LANGS = ["eng", "rus", "osd"]


def _locate_tesseract():
    """Тот же поиск, что и в mcp_pdf: PATH + типичные места Windows + env."""
    import shutil
    cand = os.environ.get("MCP_TESSERACT_CMD")
    if cand and os.path.exists(cand):
        return cand
    found = shutil.which("tesseract") or shutil.which("tesseract.exe")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    home = os.environ.get("USERPROFILE", "")
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


def _tessdata_dir(tcmd: str = None):
    """Папка tessdata: env TESSDATA_PREFIX или рядом с бинарником, иначе локальная."""
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        d = env if env.rstrip("\\/").endswith("tessdata") else os.path.join(env, "tessdata")
        return d
    if tcmd:
        d = os.path.join(os.path.dirname(tcmd), "tessdata")
        return d
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")


def check():
    tcmd = _locate_tesseract()
    print(f"tesseract бинарник: {tcmd or 'НЕ найден'}")
    td = _tessdata_dir(tcmd)
    print(f"tessdata: {td}")
    if os.path.isdir(td):
        langs = sorted(f[:-12] for f in os.listdir(td) if f.endswith(".traineddata"))
        print(f"установленные языки ({len(langs)}): {', '.join(langs) or '—'}")
    else:
        print("папка tessdata не существует")
    return 0 if tcmd else 1


def _download(url, dest):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "mcp-install-ocr"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())


def _all_langs():
    req = urllib.request.Request(TESSDATA_API, headers={"User-Agent": "mcp-install-ocr"})
    with urllib.request.urlopen(req, timeout=60) as r:
        items = json.load(r)
    return [it["name"][:-12] for it in items if it.get("name", "").endswith(".traineddata")]


def download_languages(langs):
    tcmd = _locate_tesseract()
    td = _tessdata_dir(tcmd)
    os.makedirs(td, exist_ok=True)
    ok, fail = [], []
    for lang in langs:
        url = f"{TESSDATA_RAW}/{lang}.traineddata"
        dest = os.path.join(td, f"{lang}.traineddata")
        try:
            print(f"  скачиваю {lang} -> {dest}")
            _download(url, dest)
            ok.append(lang)
        except Exception as e:
            print(f"  [!] {lang}: {e}")
            fail.append(lang)
    print(f"\nГотово: установлено {len(ok)}, ошибок {len(fail)}.")
    if not _locate_tesseract():
        print("ВНИМАНИЕ: бинарник tesseract не найден — установите его (см. --installer), "
              "иначе языки сами по себе OCR не выполнят.")
    return 0 if ok else 1


def download_installer():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       os.path.basename(WIN_INSTALLER))
    try:
        print(f"скачиваю установщик Tesseract -> {out}")
        _download(WIN_INSTALLER, out)
        print(f"Готово. Запустите установщик ВРУЧНУЮ: {out}\n"
              "При установке отметьте русский язык. После — повторите --check.")
        return 0
    except Exception as e:
        print(f"[!] Не удалось скачать установщик: {e}\n"
              "Скачайте вручную со страницы UB-Mannheim/tesseract.")
        return 1


def main(argv):
    if "--check" in argv:
        return check()
    if "--installer" in argv:
        return download_installer()
    if "--all" in argv:
        print("Получаю список всех языков…")
        try:
            langs = _all_langs()
        except Exception as e:
            print(f"[!] Не удалось получить список языков: {e}")
            return 1
        print(f"Всего языков: {len(langs)} (это объёмная загрузка).")
        return download_languages(langs)
    # --langs a,b,c
    langs = DEFAULT_LANGS
    for i, a in enumerate(argv):
        if a == "--langs" and i + 1 < len(argv):
            langs = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
    print(f"Скачиваю языки: {', '.join(langs)}")
    return download_languages(langs)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
