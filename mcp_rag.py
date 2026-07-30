# mcp_rag.py
"""
Мост для обратной совместимости: перенаправляет вызовы add_document в rag_engine.
Используется модулями, которые ожидают функцию add_document (например, mcp_web_reader).
"""

from mcp_rag_engine import rag_add_document as add_document

__all__ = ["add_document"]