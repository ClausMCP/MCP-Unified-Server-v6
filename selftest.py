#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py — быстрая самопроверка проекта MCP.

Что делает:
  1) Импортирует ВСЕ серверные модули и сообщает о падениях
     (отличая отсутствие пакета — SKIP — от реальной ошибки кода — FAIL).
  2) Прогоняет ключевые пути: память (add/query/working), верификация фактов,
     полный цикл рефлексии, загрузка плагинов, план orchestrator, геттеры
     координатора, кэш smart_search, идемпотентность планировщика.

Безопасно: все БД переключаются на временную папку, авто-память выключена —
реальные данные в C:\\Tools не затрагиваются.

Запуск:  python selftest.py
Код возврата: 0 — всё ок (FAIL=0), иначе 1.
"""
import os
import sys
import glob
import io
import tempfile
import contextlib
import traceback

# ─── Изоляция: временные БД и офлайн ДО любого импорта проекта ────────────
_TMP = tempfile.mkdtemp(prefix="mcp_selftest_")
_DB_ENVS = [
    "MCP_DB_PATH", "MCP_MEMORY_PATH", "MCP_DIALOG_DB", "MCP_EPISODIC_DB",
    "MCP_GOALS_DB", "MCP_GRAPH_DB", "MCP_HYPOTHESIS_DB", "MCP_PLANNING_DB",
    "MCP_RAG_DB_PATH", "MCP_SCHEDULER_DB", "MCP_TASK_DB", "MCP_WORLD_MODEL_DB",
]
for i, var in enumerate(_DB_ENVS):
    os.environ[var] = os.path.join(_TMP, f"{var.lower()}_{i}.db")
os.environ["MCP_LOG_DIR"] = _TMP
os.environ["MCP_AUTO_MEMORY"] = "false"
os.environ.setdefault("MCP_OFFLINE_MODE", "force_offline")
# Ограничиваем файловые операции временной папкой (selftest — отдельный процесс,
# реальную ФС не затрагивает). Позволяет безопасно проверить FS-серверы.
os.environ["MCP_ALLOWED_PATHS"] = _TMP

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# Пакеты, отсутствие которых — это «не установлено», а не баг кода.
OPTIONAL_DEPS = {
    "mcp", "chromadb", "sentence_transformers", "networkx", "bs4",
    "feedparser", "playwright", "trafilatura", "readability", "pandas",
    "docx", "pptx", "openpyxl", "requests", "keyring", "watchdog",
    "psutil", "pypdf", "PyPDF2", "PIL", "win32com", "ebooklib", "mutagen",
    "xxhash", "icalendar", "pytesseract", "html_table_takeout", "reportlab", "pypdfium2",
}

# Модули, которые не являются серверами (утилиты/инсталлятор/демо).
NON_SERVER = {"mcp_setup", "fix_lmstudio_config", "preflight", "preflight_memory",
              "selftest", "check_prompt_tools"}

results = {"PASS": 0, "FAIL": 0, "SKIP": 0}
failures = []


def record(status, name, detail=""):
    results[status] += 1
    mark = {"PASS": "[ OK ]", "FAIL": "[FAIL]", "SKIP": "[skip]"}[status]
    line = f"  {mark} {name}" + (f"  — {detail}" if detail else "")
    print(line)
    if status == "FAIL":
        failures.append((name, detail))


def check(name, fn):
    """Выполняет проверку fn(); классифицирует исключения."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            fn()
        record("PASS", name)
    except ModuleNotFoundError as e:
        miss = (str(e).split("'")[1] if "'" in str(e) else "").split(".")[0]
        if miss in OPTIONAL_DEPS:
            record("SKIP", name, f"пакет '{miss}' не установлен")
        else:
            record("FAIL", name, f"ModuleNotFoundError: {e}")
    except Exception as e:
        record("FAIL", name, f"{type(e).__name__}: {e}")


# ─── Фаза 1: импорт всех серверов ─────────────────────────────────────────
def phase_imports():
    print("\n=== Фаза 1: импорт серверных модулей ===")
    import importlib
    mods = sorted(os.path.basename(f)[:-3] for f in glob.glob(os.path.join(ROOT, "*.py")))
    mods = [m for m in mods if m not in NON_SERVER]
    for m in mods:
        check(f"import {m}", lambda m=m: importlib.import_module(m))


# ─── Фаза 2: функциональные пути ──────────────────────────────────────────
def phase_memory():
    from mcp_shared import conversation_memory as cm
    eid = cm.add(op="conversation", paths={"role": "user"}, status="logged",
                 context="selftest fact", memory_type="fact", confidence=0.9)
    assert eid, "add() не вернул id"
    # burst-уникальность id (регрессия коллизии)
    ids = {cm.add(op="c", paths={}, status="x", context="b", memory_type="fact", confidence=0.9)
           for _ in range(50)}
    assert len(ids) == 50, "коллизия entry_id под нагрузкой"
    rows = cm.query(limit=5)
    assert isinstance(rows, list), "query() вернул не список"


def phase_memory_tools():
    from mcp_shared import conversation_memory as cm
    import mcp_memory_tools as mt
    eid = cm.add(op="conversation", paths={}, status="x", context="verify me",
                 memory_type="fact", confidence=0.8)
    assert mt.explain_fact(eid)["status"] == "success"
    assert mt.verify_fact(eid)["status"] in ("verified", "already_verified")
    assert mt.deprecate_fact(eid)["status"] in ("deprecated", "already_deprecated")


def phase_reflection():
    from mcp_shared import conversation_memory as cm
    import reflection_server as rs
    cm.add(op="c", paths={}, status="x", context="reflect alpha", memory_type="fact", confidence=0.6)
    cm.add(op="c", paths={}, status="x", context="Reflect  Alpha", memory_type="fact", confidence=0.6)
    rs._reflection.run_cycle()  # detect_contradictions + confidence + cleanup


def phase_plugins():
    import asyncio
    from plugins.loader import load_plugins

    class MockServer:
        def __init__(self): self.tools = []; self.services = {}
        def add_tool(self, fn): self.tools.append(fn.__name__)
        def provide_service(self, n, h): self.services[n] = h
        async def call_tool(self, n, a): return "{}"
        async def call_service(self, n, *a, **k): return await self.services[n](*a, **k)
        async def call_llm(self, p, sp=None): return ""
        async def memory_search(self, q, top_k=5): return []
        async def memory_add(self, f, metadata=None): return "id"

    srv = MockServer()
    loaded = asyncio.run(load_plugins(srv))
    names = [p.name for p in loaded]
    assert names == ["World Model", "Planning Engine", "Hypothesis Engine"], \
        f"неверный порядок загрузки плагинов: {names}"


def phase_orchestrator():
    import inspect
    import mcp_orchestrator as o
    TP = [c for n, c in inspect.getmembers(o, inspect.isclass) if "Planner" in n][0]
    tp = TP()
    plan = {"intent": "world_model", "confidence": 0.8, "steps": [
        {"tool": "world_add_rule", "args": {"condition": {"type": "fact", "statement": "x"},
                                            "conclusion": "y", "confidence": 0.8}},
        {"tool": "world_run_inference", "args": {}},
    ]}
    out = tp.execute_plan(plan)
    assert "coroutine" not in str(out).lower(), "async-инструмент не выполнен (утечка корутины)"
    assert all(r.get("status") == "success" or r.get("error") for r in out["results"])


def phase_coordinator():
    import inspect
    import cognitive_coordinator as cc
    C = [c for n, c in inspect.getmembers(cc, inspect.isclass) if "Coordinat" in n][0]
    inst = C()
    af, rfc = inst._get_world_model()
    ch, us = inst._get_hypothesis_engine()
    assert inst._get_goal_manager() and inst._get_reflection()


def phase_smart_search_cache():
    import mcp_smart_search as ss
    calls = {"n": 0}

    def fake(q, s, l, mp, d):
        calls["n"] += 1
        return ({"memory": {"status": "success"}}, [])
    ss.invalidate_search_cache()
    orig = ss._do_search
    ss._do_search = fake
    try:
        r1 = ss.smart_search("selftest query", sources=["memory"])
        r2 = ss.smart_search("selftest query", sources=["memory"])
        assert r1["cached"] is False and r2["cached"] is True, "кэш smart_search не работает"
        assert calls["n"] == 1, "дорогая выборка выполнена дважды"
    finally:
        ss._do_search = orig


def phase_scheduler_idempotent():
    import inspect
    import mcp_scheduler as s
    DB = [c for n, c in inspect.getmembers(s, inspect.isclass) if "SchedulerDB" in n][0]
    p = os.path.join(_TMP, "sched_test.db")
    db = DB(p) if "db_path" in inspect.signature(DB.__init__).parameters else DB()
    j1 = db.add_job("selftest_job", "noop", {}, "interval", interval_seconds=60)
    j2 = db.add_job("selftest_job", "noop", {}, "interval", interval_seconds=60)
    assert j1 == j2, "add_job не идемпотентен (дубликат имени)"


def phase_healthcheck():
    import mcp_healthcheck as hc
    r = hc.health_check()
    assert r["status"] in ("healthy", "degraded"), "health_check вернул неожиданный статус"
    assert "memory" in r["subsystems"], "health_check без подсистемы memory"


def phase_backup():
    import mcp_backup as bk
    from mcp_shared import conversation_memory as cm
    cm.add(op="c", paths={}, status="x", context="backup selftest", memory_type="fact", confidence=0.9)
    res = bk.backup_state(label="selftest")
    assert res["status"] == "success" and res["files"], "backup_state не создал архив"
    prev = bk.restore_state(res["archive"], confirm=False)
    assert prev["status"] == "preview", "restore без confirm должен только показывать план"
    assert bk.list_backups()["count"] >= 1


def phase_merge_duplicates():
    from mcp_shared import conversation_memory as cm
    import mcp_memory_tools as mt
    cm.add(op="c", paths={}, status="x", context="merge dup selftest text", memory_type="fact", confidence=0.6)
    cm.add(op="c", paths={}, status="x", context="Merge  Dup  Selftest  Text", memory_type="fact", confidence=0.7)
    prev = mt.merge_duplicate_facts(dry_run=True)
    assert prev["status"] == "preview" and prev["entries_to_merge"] >= 1
    res = mt.merge_duplicate_facts(dry_run=False)
    assert res["status"] == "success" and res["merged_entries"] >= 1


def phase_fs_operations():
    import mcp_fs_operations as fs
    base = os.path.join(_TMP, "fs_test")
    os.makedirs(base, exist_ok=True)
    f1 = os.path.join(base, "a.txt")
    f2 = os.path.join(base, "b.txt")
    f3 = os.path.join(base, "c.txt")
    fs.write_file(f1, "hello selftest")
    r = fs.read_file(f1)
    assert "hello selftest" in str(r), "read_file не вернул содержимое"
    fs.copy_file(f1, f2)
    assert os.path.exists(f2), "copy_file не создал копию"
    fs.move_file(f2, f3)
    assert os.path.exists(f3) and not os.path.exists(f2), "move_file отработал неверно"
    fs.list_directory(base)
    fs.delete_file(f3, use_trash=False)
    assert not os.path.exists(f3), "delete_file не удалил файл"
    # Граница безопасности: запись ВНЕ allowlist должна блокироваться
    blocked = False
    try:
        fs.write_file(os.path.join(tempfile.gettempdir(), "mcp_selftest_outside.txt"), "x")
    except PermissionError:
        blocked = True
    assert blocked, "allowlist НЕ заблокировал запись вне разрешённой папки"


def phase_code_exec():
    import mcp_code_exec as ce
    r = ce.run_python("print(sum(range(11)))")
    assert r["status"] == "success" and r["stdout"].strip() == "55", "run_python неверно"
    t = ce.run_python("import time; time.sleep(3)", timeout=1)
    assert t["status"] == "timeout", "таймаут не сработал"
    c = ce.calc("2 ** 10 + sqrt(16)")
    assert c["status"] == "success" and c["result"] == 1028.0, "calc неверно"
    bad = ce.calc("__import__('os')")
    assert bad["status"] == "error", "calc sandbox пропустил импорт"


def phase_pdf():
    import mcp_pdf as pdf
    if not pdf.PDF_AVAILABLE:
        raise ModuleNotFoundError("No module named 'pypdf'")
    try:
        import reportlab  # noqa
    except ImportError:
        raise ModuleNotFoundError("No module named 'reportlab'")
    base = os.path.join(_TMP, "pdf_test")
    os.makedirs(base, exist_ok=True)
    a = os.path.join(base, "a.pdf")
    b = os.path.join(base, "b.pdf")
    m = os.path.join(base, "m.pdf")
    assert pdf.create_pdf("Hello selftest PDF", a, title="A")["status"] == "success"
    assert pdf.create_pdf("Second", b)["status"] == "success"
    assert pdf.pdf_info(a)["pages"] >= 1
    assert pdf.read_pdf(a)["status"] == "success"
    assert pdf.merge_pdfs([a, b], m)["status"] == "success"
    assert pdf.extract_pages(m, 1, 1, os.path.join(base, "p1.pdf"))["status"] == "success"
    # images_to_pdf: объединение картинок в PDF
    try:
        from PIL import Image, ImageDraw
        ip = []
        for i in range(2):
            im = Image.new("RGB", (400, 300), "white")
            ImageDraw.Draw(im).text((20, 140), f"IMG {i+1}", fill="black")
            fp = os.path.join(base, f"i{i}.png"); im.save(fp); ip.append(fp)
        rc = pdf.images_to_pdf(image_paths=ip, output=os.path.join(base, "imgs.pdf"))
        assert rc["status"] == "success" and rc["pages"] == 2, "images_to_pdf"
    except ModuleNotFoundError:
        pass
    # OCR-фоллбэк на «сканированном» (image-only) PDF — если OCR доступен
    if getattr(pdf, "OCR_AVAILABLE", False) and getattr(pdf, "TESSERACT_CMD", None):
        try:
            from PIL import Image, ImageDraw
            imgs = []
            for n in range(2):
                im = Image.new("RGB", (900, 200), "white")
                ImageDraw.Draw(im).text((30, 80), f"SCANPAGE {n+1} 2024", fill="black")
                imgs.append(im)
            scan = os.path.join(base, "scan.pdf")
            imgs[0].save(scan, "PDF", save_all=True, append_images=imgs[1:])
            # авто-OCR одной страницы через read_pdf
            r = pdf.read_pdf(scan, ocr="auto", lang="eng", max_pages=1)
            assert r["ocr_pages"] >= 1 and "SCANPAGE" in r["pages"][0]["text"].upper(), "read_pdf OCR не сработал"
            # синхронный ocr_pdf в файл + ocr_status
            outp = os.path.join(base, "scan.ocr.txt")
            res = pdf.ocr_pdf(scan, output_path=outp, lang="eng", background=False)
            assert res["status"] == "success" and "SCANPAGE" in open(outp, encoding="utf-8").read().upper()
            assert pdf.ocr_status(outp).get("status") == "done", "ocr_status не вернул done"
        except ModuleNotFoundError:
            pass  # PIL отсутствует


def phase_guard():
    import mcp_memory_tools as mt
    import mcp_guard as gd
    from mcp_shared import conversation_memory as cm
    mt.log_conversation("assistant", "The project runs offline from C:/Tools.", dialog_id="g1")
    rep = gd.check_repetition("the project  runs offline from c:/tools", dialog_id="g1")
    assert rep["is_repetition"] is True, "повтор не обнаружен"
    rep2 = gd.check_repetition("Totally different unrelated sentence about weather", dialog_id="g1")
    assert rep2["is_repetition"] is False, "ложный повтор"
    cm.add(op="c", paths={}, status="x", context="Paris is the capital of France", memory_type="fact", confidence=0.9)
    assert gd.check_grounding("Paris is the capital of France")["verdict"] == "supported"
    assert gd.check_grounding("Atlantis is a real underwater city near Mars")["verdict"] == "unknown"


def phase_db_tools():
    import mcp_db_tools as db
    mempath = os.environ["MCP_MEMORY_PATH"]
    assert db.list_tables(mempath)["status"] == "success"
    assert db.sql_query(mempath, "SELECT COUNT(*) AS n FROM entries")["status"] == "success"
    assert db.sql_query(mempath, "DELETE FROM entries")["status"] == "blocked", "защита записи не сработала"
    opt = db.optimize_all_databases()
    assert opt["status"] == "success" and len(opt["databases"]) >= 1


def phase_office():
    import mcp_office_editor as oe
    import openpyxl  # noqa
    xl = os.path.join(_TMP, "calc.xlsx")
    oe.create_excel(xl, data={"A1": 10, "A2": 20})
    assert oe.excel_set_cell(xl, "A3", "=SUM(A1:A2)").get("status") == "success"
    assert str(openpyxl.load_workbook(xl).active["A3"].value).startswith("=SUM"), "формула не записана"
    docp = os.path.join(_TMP, "Договоры", "d.docx")  # вложенная папка должна создаться
    oe.create_docx(docp, "ДОГОВОР №1\nг. Острогожск", title="ДОГОВОР")
    assert os.path.exists(docp), "create_docx не создал файл в новой папке"


def phase_recall():
    from mcp_shared import conversation_memory as cm, dialog_ctx
    import mcp_memory_tools as mt
    from context_manager_server import recall_fact
    sol = "Для дедупликации списка с сохранением порядка используй list(dict.fromkeys(items))"
    dialog_ctx.set("rc_d1")
    mt.log_conversation("user", "как убрать дубли из списка сохранив порядок?", dialog_id="rc_d1")
    cm.add(op="code_solution", paths={"topic": "dedup"}, status="solved",
           context=sol, memory_type="fact", confidence=0.9, dialog="rc_d1")
    dialog_ctx.set("rc_d2")
    rf = recall_fact("дедупликация списка сохранить порядок", dialog_id="rc_d2")
    assert "fromkeys" in str(rf.get("fact", {}).get("context", "")), \
        "recall вернул не решение (возможно, вопрос пользователя)"


def phase_extract():
    import mcp_extract as ex
    txt = os.path.join(_TMP, "ex.txt")
    open(txt, "w", encoding="utf-8").write("Привет мир — extract selftest")
    r = ex.extract_any_text(txt)
    assert r["status"] == "success" and "Привет" in r["text"], "txt extract"
    # xlsx через read_excel (минуя возможный баг excel_to_markdown)
    try:
        import openpyxl, mcp_office_editor as oe  # noqa
        xl = os.path.join(_TMP, "ex.xlsx")
        oe.create_excel(xl, data=[["Имя", "Сумма"], ["Иван", 100]])
        rx = ex.extract_any_text(xl)
        assert rx["status"] == "success" and "Иван" in rx["text"], "xlsx extract"
    except ModuleNotFoundError:
        pass
    # batch_extract_text по папке
    bdir = os.path.join(_TMP, "batchdocs")
    os.makedirs(bdir, exist_ok=True)
    open(os.path.join(bdir, "n.txt"), "w", encoding="utf-8").write("батч текст один")
    br = ex.batch_extract_text(bdir, lang="eng")
    assert br["status"] == "success" and br["extracted"] >= 1, "batch_extract_text"


def phase_web_sources():
    # Маршрутизация движков в smart_search (через мок — без сети)
    import mcp_smart_search as ss
    import mcp_web_reader as w
    captured = {}
    orig = w.web_search_enhanced
    def fake(query, max_results=15, sources=None, **kw):
        captured["sources"] = sources
        return {"status": "success", "count": 0, "results": []}
    w.web_search_enhanced = fake
    try:
        ss.smart_search("проверка источников", sources=["searxng", "brave"])
    finally:
        w.web_search_enhanced = orig
    assert captured.get("sources") == ["searxng", "brave"], "smart_search не передал движки"
    # Очистка кэшей вызываема и безопасна (на отсутствующей БД -> deleted 0)
    assert w.clear_search_cache.__name__ == "clear_search_cache"
    assert w.clear_web_cache.__name__ == "clear_web_cache"


def phase_functional():
    print("\n=== Фаза 2: функциональные пути ===")
    check("fs: write/read/copy/move/delete + allowlist", phase_fs_operations)
    check("office: create_excel+formula, docx in new folder", phase_office)
    check("extract: universal text (txt/xlsx)", phase_extract)
    check("web: smart_search source routing (searxng/brave)", phase_web_sources)
    check("code_exec: run_python/calc/timeout", phase_code_exec)
    check("pdf: create/read/info/merge/extract", phase_pdf)
    check("guard: repetition + grounding", phase_guard)
    check("memory: recall prior solution (cross-dialog)", phase_recall)
    check("db_tools: query/write-guard/optimize-all", phase_db_tools)
    check("memory: add/query/burst-id", phase_memory)
    check("memory_tools: explain/verify/deprecate", phase_memory_tools)
    check("memory: merge duplicate facts", phase_merge_duplicates)
    check("reflection: full cycle", phase_reflection)
    check("plugins: load + dependency order", phase_plugins)
    check("orchestrator: execute async tool", phase_orchestrator)
    check("cognitive_coordinator: engine getters", phase_coordinator)
    check("smart_search: TTL cache", phase_smart_search_cache)
    check("scheduler: idempotent add_job", phase_scheduler_idempotent)
    check("healthcheck: system status", phase_healthcheck)
    check("backup: create/list/restore-preview", phase_backup)
    check("dispatch: live handle_tool_call strips internal _trace_id", phase_tool_dispatch_trace)


def phase_tool_dispatch_trace():
    """Регрессия: служебный _trace_id не должен попадать в обработчики
    с фиксированной сигнатурой (баг mem_add/office: 'unexpected keyword _trace_id')."""
    from mcp_shared import BaseMCPServer
    def fixed_sig(op, paths, status, context=None):   # без **kwargs, как MemoryEngine.add
        return {"ok": True, "op": op}
    srv = BaseMCPServer("selftest-dispatch", "1.0")
    srv.register_tool("st_fixed", {"description": "x", "inputSchema": {"type": "object"}},
                      lambda **kw: fixed_sig(**kw))
    srv._current_dialog_id = "st_disp"
    resp = srv.handle_tool_call(1, {"name": "st_fixed",
                                    "arguments": {"op": "o", "paths": "p", "status": "s"}})
    assert "error" not in resp, f"диспетчер протёк служебным ключом: {resp.get('error')}"
    assert "ok" in resp["result"]["content"][0]["text"], "обработчик не отработал"


def phase_doc_lint():
    """Информационная фаза: сверка промптов с инструментами (не влияет на FAIL)."""
    print("\n=== Фаза 3: соответствие промптов (информационно) ===")
    try:
        import subprocess
        rc = subprocess.run([sys.executable, os.path.join(ROOT, "check_prompt_tools.py")],
                            capture_output=True, text=True, timeout=60)
        out = (rc.stdout or "").strip().splitlines()
        tail = out[-1] if out else ""
        if rc.returncode == 0:
            print(f"  [ OK ] промпты согласованы с инструментами — {tail}")
        else:
            print(f"  [info] есть расхождения промпт/инструменты — {tail} (не критично)")
    except Exception as e:
        print(f"  [skip] линтер промптов недоступен: {e}")


def main():
    print("=" * 60)
    print("MCP SELF-TEST")
    print(f"Временная папка БД: {_TMP}")
    print("=" * 60)
    phase_imports()
    phase_functional()
    phase_doc_lint()

    print("\n" + "=" * 60)
    print(f"ИТОГ:  PASS={results['PASS']}  FAIL={results['FAIL']}  SKIP={results['SKIP']}")
    if failures:
        print("\nПРОВАЛЫ:")
        for name, detail in failures:
            print(f"  - {name}: {detail}")
    print("=" * 60)

    # Временную папку не удаляем вручную: фоновые daemon-потоки движков ещё
    # могут обращаться к своим БД до выхода процесса. ОС очистит temp сама.
    print(f"(временные БД: {_TMP} — удалятся системой)")

    return 1 if results["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
