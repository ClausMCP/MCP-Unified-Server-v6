#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Help System v1.0 – встроенная справка по инструментам
Вызов: help() – список всех инструментов
       help(tool_name) – подробное описание
       !help tool_name – прямой вызов
"""
import json
import re
from typing import Dict, Any, List, Optional

from mcp_shared import BaseMCPServer, _log, dialog_ctx

# Глобальная ссылка на unified сервер (устанавливается при регистрации)
_unified_server = None

def set_unified_server(server: BaseMCPServer):
    global _unified_server
    _unified_server = server

def _get_tools_info() -> Dict[str, Dict]:
    """Извлекает информацию о всех зарегистрированных инструментах."""
    if not _unified_server:
        return {}
    tools = _unified_server.tools
    handlers = _unified_server._handlers
    result = {}
    for tool in tools:
        name = tool.get("name")
        if not name:
            continue
        # Получаем схему и описание
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        
        # Формируем описание аргументов
        args_info = []
        for prop_name, prop_info in properties.items():
            prop_type = prop_info.get("type", "any")
            prop_desc = prop_info.get("description", "")
            is_required = prop_name in required
            default = prop_info.get("default")
            arg_line = f"  {prop_name} ({prop_type})"
            if is_required:
                arg_line += " [required]"
            if default is not None:
                arg_line += f" = {json.dumps(default, ensure_ascii=False)}"
            if prop_desc:
                arg_line += f"\n      {prop_desc}"
            args_info.append(arg_line)
        
        # Формируем пример вызова (если есть в описании или генерируем простой)
        desc = tool.get("description", "")
        example = _extract_example(desc, name, properties)
        
        result[name] = {
            "name": name,
            "description": desc,
            "args": args_info,
            "example": example,
            "module": _get_handler_module(handlers.get(name))
        }
    return result

def _get_handler_module(handler) -> str:
    """Пытается определить модуль, из которого зарегистрирован инструмент."""
    if handler is None:
        return "unknown"
    module = getattr(handler, '__module__', 'unknown')
    return module.split('.')[-1] if '.' in module else module

def _extract_example(description: str, tool_name: str, properties: Dict) -> str:
    """Извлекает пример из описания или генерирует простой."""
    # Ищем в описании "Example:" или "Пример:"
    ex_match = re.search(r'(?:Example|Пример):\s*(.+?)(?:\n|$)', description, re.IGNORECASE)
    if ex_match:
        return ex_match.group(1).strip()
    # Генерируем простой пример
    required_args = [k for k, v in properties.items() if k in ['path', 'query', 'source', 'target']]
    if required_args:
        sample_args = {k: f"<{k}>" for k in required_args[:2]}
        args_str = " ".join(f"{k}={v}" for k, v in sample_args.items())
        return f"!{tool_name} {args_str}"
    elif properties:
        first_arg = next(iter(properties.keys()))
        return f"!{tool_name} {first_arg}=<value>"
    else:
        return f"!{tool_name}"

def help_command(topic: Optional[str] = None, dialog_id: Optional[str] = None) -> Dict:
    """Основная функция справки."""
    d_id = dialog_id or dialog_ctx.get()
    tools_info = _get_tools_info()
    
    if not tools_info:
        return {"status": "error", "message": "No tools registered or unified server not set"}
    
    if topic:
        # Подробная справка по конкретному инструменту
        topic_lower = topic.lower()
        # Ищем инструмент по точному совпадению или частичному
        tool = None
        for name, info in tools_info.items():
            if name.lower() == topic_lower:
                tool = info
                break
        if not tool:
            # Поиск по частичному совпадению
            matches = [name for name in tools_info.keys() if topic_lower in name.lower()]
            if len(matches) == 1:
                tool = tools_info[matches[0]]
            elif len(matches) > 1:
                return {
                    "status": "multiple_matches",
                    "topic": topic,
                    "matches": matches,
                    "message": f"Multiple tools match '{topic}'. Use exact name: {', '.join(matches)}"
                }
        if not tool:
            return {"status": "error", "message": f"Tool '{topic}' not found. Use help() to list all tools."}
        
        # Формируем подробный ответ
        lines = [
            f"📘 **{tool['name']}**",
            f"📝 {tool['description']}",
            f"📦 Module: {tool['module']}",
            "",
            "**Arguments:**"
        ]
        if tool['args']:
            lines.extend(tool['args'])
        else:
            lines.append("  (no arguments)")
        lines.append("")
        lines.append(f"**Example:** `{tool['example']}`")
        
        return {
            "status": "success",
            "tool": tool['name'],
            "help_text": "\n".join(lines),
            "dialog_id": d_id
        }
    else:
        # Список всех инструментов, сгруппированных по модулям
        grouped = {}
        for name, info in tools_info.items():
            module = info['module']
            grouped.setdefault(module, []).append((name, info['description'][:80]))
        
        lines = ["**Available MCP Tools**", "", "Use `help(tool_name)` for details.", ""]
        for module, tools in sorted(grouped.items()):
            lines.append(f"## {module} ({len(tools)} tools)")
            for name, desc in sorted(tools):
                lines.append(f"  • `{name}` – {desc}")
            lines.append("")
        
        return {
            "status": "success",
            "tools_count": len(tools_info),
            "help_text": "\n".join(lines),
            "dialog_id": d_id
        }

def register_help_tool(server: BaseMCPServer):
    """Регистрирует инструмент help в MCP-сервере."""
    set_unified_server(server)
    server.register_tool("help", {
        "description": "Show help for MCP tools. Call with no args to list all tools, or with tool_name for details. Example: help(search_files)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Tool name (optional)"},
                "dialog_id": {"type": "string"}
            }
        }
    }, lambda **kw: help_command(kw.get("topic"), kw.get("dialog_id")))

if __name__ == "__main__":
    # Тестовый запуск (для отладки)
    _log("Help module loaded. Use register_help_tool(server) to integrate.")