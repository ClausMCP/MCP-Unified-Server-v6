#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пример «preflight»-обогащения запроса памятью ПЕРЕД отправкой в LLM.

Раньше скрипт открывал захардкоженную "mcp_memory.db" (относительный путь —
не находил реальную БД в C:\\Tools), вызывал input() на верхнем уровне (срабатывал
при импорте) и падал на записях с NULL-контекстом. Теперь использует реальный
слой проекта: conversation_memory (правильный путь к БД) + query_llm.

Запуск:  python preflight.py
"""
from mcp_shared import get_global_memory, query_llm


def search_memory(query: str, limit: int = 5) -> str:
    """Ищет релевантные записи в памяти проекта (использует общий путь к БД)."""
    memory = get_global_memory()
    hits = memory.query(path=query, limit=limit, include_context=True)
    lines = [h.get("context") or h.get("op", "") for h in hits]
    return "\n".join(x for x in lines if x)


def build_enriched_prompt(user_query: str) -> str:
    memory_context = search_memory(user_query)
    if memory_context:
        return (
            "[ИНФОРМАЦИЯ ИЗ ПАМЯТИ]:\n"
            f"{memory_context}\n\n"
            "[ВОПРОС]:\n"
            f"{user_query}"
        )
    return user_query


def handle(user_query: str) -> str:
    """Обогащает запрос памятью и отправляет в локальную LLM."""
    return query_llm(build_enriched_prompt(user_query))


if __name__ == "__main__":
    q = input("Ваш вопрос: ").strip()
    if q:
        print(handle(q))
