#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Export LM Studio Chats v1.0
Находит и конвертирует все диалоги LM Studio в Markdown.
"""
import os
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

from mcp_shared import BaseMCPServer, _log, dialog_ctx, normalize_path, _ensure_allowed

# Путь к папке с чатами LM Studio
LM_STUDIO_CHATS_PATH = Path.home() / ".lmstudio" / "conversations"

def _safe_filename(text: str) -> str:
    """Преобразует строку в безопасное имя файла."""
    # Удаляем недопустимые для имени файла символы
    return re.sub(r'[\\/*?:"<>|]', "", text).strip()

def _find_chat_files() -> List[Path]:
    """Рекурсивно ищет все .conversation.json файлы в папке с чатами LM Studio."""
    chat_files = []
    if not LM_STUDIO_CHATS_PATH.exists():
        _log(f"[ExportLM] Папка с чатами не найдена: {LM_STUDIO_CHATS_PATH}")
        return chat_files

    for file in LM_STUDIO_CHATS_PATH.rglob("*.conversation.json"):
        chat_files.append(file)
    return chat_files

def _parse_chat_json(file_path: Path) -> Optional[Dict]:
    """
    Парсит JSON-файл чата LM Studio в читаемый словарь.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        _log(f"[ExportLM] Ошибка чтения {file_path}: {e}")
        return None

    # Извлекаем метаданные
    title = data.get('name', Path(file_path).stem)
    created_timestamp = data.get('createdAt', 0)
    created_at = datetime.fromtimestamp(created_timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')
    model = "Unknown"
    # Пытаемся извлечь название модели
    if 'lastUsedModel' in data and 'indexedModelIdentifier' in data['lastUsedModel']:
        model = data['lastUsedModel']['indexedModelIdentifier']
    elif 'genInfo' in data and 'indexedModelIdentifier' in data['genInfo']:
        model = data['genInfo']['indexedModelIdentifier']
    token_count = data.get('tokenCount', 0)

    # Извлекаем сообщения
    messages = []
    for msg in data.get('messages', []):
        versions = msg.get('versions', [])
        # Находим выбранную версию сообщения
        selected_version = None
        for version in versions:
            if version.get('currentlySelected'):
                selected_version = version
                break
        if not selected_version and versions:
            selected_version = versions[0]

        if selected_version:
            msg_type = selected_version.get('type')
            role = msg.get('role', 'unknown')
            text = ""

            if msg_type == 'singleStep':
                # Для singleStep текст находится в content первого элемента массива
                content_blocks = selected_version.get('content', [])
                if content_blocks:
                    text = content_blocks[0].get('text', '')
            elif msg_type == 'multiStep':
                # Для multiStep текст берется из последнего шага
                steps = selected_version.get('steps', [])
                if steps:
                    last_step = steps[-1]
                    content_blocks = last_step.get('content', [])
                    if content_blocks:
                        text = content_blocks[0].get('text', '')
            else:
                # Обработка сообщений неизвестного типа или старых форматов
                content_blocks = selected_version.get('content', [])
                if content_blocks:
                    text = content_blocks[0].get('text', '')

            # Добавляем сообщение, только если есть текст
            if text:
                messages.append({"role": role, "text": text})

    return {
        "title": title,
        "created_at": created_at,
        "model": model,
        "token_count": token_count,
        "messages": messages,
        "source_file": str(file_path)
    }

def _convert_to_markdown(chat_data: Dict) -> str:
    """Преобразует словарь с данными чата в формат Markdown."""
    md_lines = [
        "---",
        f"title: \"{chat_data['title']}\"",
        f"created: \"{chat_data['created_at']}\"",
        f"model: {chat_data['model']}",
        f"tokens: {chat_data['token_count']}",
        "---",
        "",
        f"# {chat_data['title']}",
        "",
    ]

    for msg in chat_data['messages']:
        role = "**User:**" if msg['role'] == 'user' else "**Assistant:**"
        md_lines.append(role)
        md_lines.append(msg['text'])
        md_lines.append("")  # Пустая строка для разделения

    return "\n".join(md_lines)

def export_all_chats(output_dir: Optional[str] = None, format: str = "md") -> Dict:
    """
    Главная функция: находит все чаты LM Studio и экспортирует их в Markdown (и, опционально, в PDF).
    """
    d_id = dialog_ctx.get()
    chat_files = _find_chat_files()
    if not chat_files:
        return {"status": "error", "message": f"Не найдено файлов чатов в {LM_STUDIO_CHATS_PATH}"}

    # Определяем директорию для сохранения
    if output_dir is None:
        output_dir = "lmstudio_chats_export"
    output_path = Path(normalize_path(output_dir)).resolve()
    try:
        _ensure_allowed(output_path, "export_all_chats")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}

    output_path.mkdir(parents=True, exist_ok=True)
    
    exported_files = []
    for file in chat_files:
        chat_data = _parse_chat_json(file)
        if chat_data and chat_data['messages']:
            md_content = _convert_to_markdown(chat_data)
            safe_title = _safe_filename(chat_data['title'])
            base_filename = output_path / f"{safe_title}_{chat_data['source_file'].stem}"
            md_filename = base_filename.with_suffix(".md")
            
            with open(md_filename, 'w', encoding='utf-8') as f:
                f.write(md_content)
            exported_files.append(str(md_filename))
            _log(f"[ExportLM] Экспортирован чат: {md_filename}")

    if not exported_files:
        return {"status": "success", "message": "Не найдено чатов с сообщениями для экспорта.", "exported_files": []}

    # Если запрошен PDF, конвертируем все созданные MD-файлы с помощью внешней команды
    if format.lower() == "pdf":
        # Пытаемся найти Pandoc
        pandoc_path = None
        for cmd in ["pandoc", "pandoc.exe"]:
            from shutil import which
            pandoc_path = which(cmd)
            if pandoc_path:
                break
        if not pandoc_path:
            return {
                "status": "pdf_unavailable",
                "message": "PDF-конвертация невозможна: Pandoc не установлен. Файлы сохранены в Markdown.",
                "markdown_paths": exported_files
            }

        import subprocess
        pdf_files = []
        for md_file in exported_files:
            pdf_file = Path(md_file).with_suffix(".pdf")
            try:
                subprocess.run([pandoc_path, str(md_file), "-o", str(pdf_file)], capture_output=True, check=True)
                pdf_files.append(str(pdf_file))
                _log(f"[ExportLM] Конвертирован в PDF: {pdf_file}")
            except Exception as e:
                _log(f"[ExportLM] Ошибка конвертации {md_file} в PDF: {e}")
        
        if pdf_files:
            return {
                "status": "success",
                "format": "pdf",
                "output_paths": pdf_files,
                "markdown_paths": exported_files,
                "count": len(pdf_files),
                "dialog_id": d_id
            }
        else:
            return {
                "status": "partial",
                "message": "PDF-конвертация не удалась. Markdown-файлы сохранены.",
                "markdown_paths": exported_files
            }

    return {
        "status": "success",
        "format": "md",
        "output_paths": exported_files,
        "count": len(exported_files),
        "dialog_id": d_id
    }

def register_export_lmstudio_tool(server: BaseMCPServer):
    """Регистрирует инструмент export_lmstudio_chats в MCP-сервере."""
    server.register_tool("export_lmstudio_chats", {
        "description": "Находит и экспортирует все диалоги LM Studio в Markdown (или PDF).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string", "description": "Папка для сохранения файлов (по умолчанию 'lmstudio_chats_export')"},
                "format": {"type": "string", "enum": ["md", "pdf"], "default": "md", "description": "Формат экспорта (md или pdf)"}
            }
        }
    }, lambda **kw: export_all_chats(kw.get("output_dir"), kw.get("format", "md")))

if __name__ == "__main__":
    # Тестовый запуск для отладки
    _log("Module 'mcp_export_lmstudio' loaded.")