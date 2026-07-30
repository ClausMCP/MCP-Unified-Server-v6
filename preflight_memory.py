#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пример «preflight»-обогащения запроса памятью ПЕРЕД отправкой в LLM.

Раньше файл импортировал несуществующий модуль `mcp_client`, поэтому не запускался.
Теперь он использует реальный слой проекта: conversation_memory из mcp_shared
(поиск по истории диалогов) + локальный LLM через query_llm.
Это автономный скрипт-демо, его можно запускать напрямую: python preflight_memory.py
"""
from mcp_shared import get_global_memory, query_llm


def build_enriched_prompt(user_input: str, limit: int = 5) -> str:
    """Ищет релевантные записи в памяти диалогов и формирует расширенный контекст."""
    memory = get_global_memory()
    # Поиск по контексту прошлых записей (op/context содержат текст диалога)
    hits = memory.query(path=user_input, limit=limit, include_context=True)
    mem_block = "\n".join(
        f"- [{h.get('ts', '')}] {h.get('context') or h.get('op', '')}"
        for h in hits
    ) or "(совпадений в памяти не найдено)"

    return (
        "[ПАМЯТЬ ИЗ ПРОШЛЫХ ДИАЛОГОВ]:\n"
        f"{mem_block}\n\n"
        "[НОВЫЙ ВОПРОС ПОЛЬЗОВАТЕЛЯ]:\n"
        f"{user_input}\n"
    )


def handle_user_input(user_input: str) -> str:
    """Собирает контекст из памяти и отправляет обогащённый промпт в локальную LLM."""
    enriched_prompt = build_enriched_prompt(user_input)
    return query_llm(enriched_prompt)


if __name__ == "__main__":
    q = input("Ваш вопрос: ").strip()
    if q:
        print(handle_user_input(q))
