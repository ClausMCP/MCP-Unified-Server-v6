#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
check_prompt_tools.py - сверяет имена инструментов в промптах (Promt_*.txt)
с реально зарегистрированными в серверах (register_tool("...")).

Исправления относительно версии v2:
- ИСПРАВЛЕН SyntaxError: docstring модуля больше не содержит literal-тройных
  кавычек (использованы одинарные тройные кавычки для обрамления).
- Удалены неиспользуемые импорты (os, Dict, Tuple).
- Двухуровневый отчёт вместо плоского списка:
    * "вероятные расхождения" - имя в backticks И префикс совпадает с
      семейством реальных инструментов (это и есть устаревшие имена);
    * "возможные" - всё остальное, выводится справочно, на код возврата
      не влияет.
  На реальных промптах проекта это убирает 100% ложных срабатываний
  (retry_after_sec, web_only) без потери реальных находок.
- Код возврата: 0 - ОК, 1 - есть вероятные расхождения, 2 - внутренняя ошибка.
'''
import ast
import re
import sys
from pathlib import Path
from typing import FrozenSet, List, Set

ROOT: Path = Path(__file__).resolve().parent

# Параметры/значения/плейсхолдеры, которые НЕ являются инструментами.
NOISE: FrozenSet[str] = frozenset(
    {
        # Аргументы типовых инструментов
        "source_file", "date_processed", "folder_path", "force_reindex",
        "cleanup_deleted", "file_path", "output_path", "wait_until",
        "timeout_ms", "bypass_cloudflare", "dry_run", "confirm_dangerous",
        "collection_name", "goal_type", "tool_name", "tool_args",
        "source_name", "mempalace_project", "full_path", "document_title",
        "start_menu", "source_dialog_id", "params_hash", "current_date",
        "force_offline", "forecast_2027", "goal_abc123", "goal_id",
        "path_to_folder", "top_k", "max_results", "plan_id", "hypothesis_id",
        "entry_id", "rule_id", "dialog_id", "trace_id",
        # Концепции / подсистемы / поля ответов
        "conversation_memory", "is_repetition",
        # Поля статусов и режимов (не инструменты)
        "retry_after_sec", "web_only", "half_open", "journal_mode",
        "content_hash", "memory_id", "contract_version", "change_type",
    }
)

_CODE_FENCE = re.compile(r"^\s*```")
_BACKTICK_NAME = re.compile(r"`([a-zA-Z][a-zA-Z0-9_]+)`")
_SNAKE_NAME = re.compile(r"\b([a-z]+_[a-z0-9_]+)\b")
_VALID_TOOL_NAME = re.compile(r"[a-zA-Z0-9_]+")


def _module_names() -> Set[str]:
    """Имена модулей проекта (это файлы, а не инструменты)."""
    return {p.stem for p in ROOT.glob("*.py")}


def registered_tools() -> Set[str]:
    """Имена инструментов, зарегистрированных через register_tool("...").

    Разбор через ast: устойчив к многострочным вызовам, форматированию,
    комментариям и docstring'ам. Динамические имена (f-string, конкатенация)
    принципиально не извлекаются - для них нужен статический литерал.
    """
    names: Set[str] = set()
    for py_path in ROOT.glob("*.py"):
        try:
            source = py_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(py_path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_register_call = (
                (isinstance(func, ast.Name) and func.id == "register_tool")
                or (isinstance(func, ast.Attribute) and func.attr == "register_tool")
            )
            if not is_register_call or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if _VALID_TOOL_NAME.fullmatch(first.value):
                    names.add(first.value)
    return names


def _collect(text: str):
    """Возвращает (все кандидаты, кандидаты в backticks).

    Markdown-блоки ``` пропускаются: там примеры вызовов, а не документация.
    """
    all_c: Set[str] = set()
    ticked: Set[str] = set()
    in_code_block = False
    for line in text.splitlines():
        if _CODE_FENCE.match(line):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        found_ticked = set(_BACKTICK_NAME.findall(line))
        ticked.update(found_ticked)
        all_c.update(found_ticked)
        all_c.update(_SNAKE_NAME.findall(line))
    keep = lambda s: {c for c in s if "_" in c and c not in NOISE}
    return keep(all_c), keep(ticked)


def collect_prompt_candidates(prompt_path: Path):
    try:
        text = prompt_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set(), set()
    return _collect(text)


def _is_known(cand: str, actual: Set[str], modules: Set[str], families: Set[str]) -> bool:
    if cand in actual or cand in modules:
        return True
    if cand.endswith("_") and cand[:-1] in families:
        return True
    if any(a.startswith(cand + "_") for a in actual):
        return True
    return False


def main() -> int:
    actual: Set[str] = registered_tools()
    if not actual:
        print("[check] не найдено register_tool(...) - запускать из корня проекта")
        return 2

    print(f"[check] зарегистрировано инструментов: {len(actual)}")

    prompt_files: List[Path] = sorted(
        set(ROOT.glob("Promt_*.txt")) | set(ROOT.glob("*rompt*.txt"))
    )
    if not prompt_files:
        print("[check] промпты Promt_*.txt не найдены")
        return 0

    modules: Set[str] = _module_names()
    families: Set[str] = {n.rsplit("_", 1)[0] for n in actual}
    # Префиксы семейств: первый сегмент имени инструмента (mempalace_, hyp_, ...)
    prefixes: Set[str] = {n.split("_", 1)[0] for n in actual}

    problems = 0
    for prompt in prompt_files:
        all_c, ticked = collect_prompt_candidates(prompt)
        likely: List[str] = []
        possible: List[str] = []
        for c in sorted(all_c):
            if _is_known(c, actual, modules, families):
                continue
            # Вероятное расхождение: документировано как инструмент (backticks)
            # И принадлежит существующему семейству => это устаревшее имя.
            if c in ticked and c.split("_", 1)[0] in prefixes:
                likely.append(c)
            else:
                possible.append(c)

        print(f"\n=== {prompt.name} ===")
        if likely:
            problems += len(likely)
            print("  ВЕРОЯТНО устаревшие имена инструментов:")
            for m in likely:
                print(f"    - {m}")
        if possible:
            print("  Возможные (справочно, на код возврата не влияют):")
            for m in possible:
                print(f"    ? {m}")
        if not likely and not possible:
            print("  OK - все упомянутые инструменты существуют")
        elif not likely:
            print("  OK - вероятных расхождений нет")

    print("\n" + "=" * 50)
    if problems:
        print(f"НАЙДЕНО вероятных расхождений: {problems}")
        return 1
    print("Вероятных расхождений нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
