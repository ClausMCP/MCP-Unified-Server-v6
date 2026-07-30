#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генерирует конфигурацию для LM Studio с портативными путями.
Всегда использует mcp_fs_server.py (unified) для доступа ко всем инструментам.
"""
import json
import os
import shutil
from pathlib import Path
from datetime import datetime

def generate_correct_config():
    script_dir = Path(__file__).parent

    # 1. Найти Python (сначала .venv, потом портативный tools)
    venv_python = script_dir / ".venv" / "Scripts" / "python.exe"
    tools_python = script_dir / "tools" / "python" / "python.exe"

    if venv_python.exists():
        python_path = str(venv_python)
    elif tools_python.exists():
        python_path = str(tools_python)
    else:
        raise FileNotFoundError("Python not found in .venv or tools/python")

    # 2. Unified сервер
    server_script = script_dir / "mcp_fs_server.py"
    if not server_script.exists():
        raise FileNotFoundError(f"Server script not found: {server_script}")

    # 3. Построить PATH для портативных инструментов
    tools_dir = script_dir / "tools"
    path_parts = []
    for sub in ["python", "tesseract", "ffmpeg", "pandoc", "wkhtmltopdf"]:
        p = tools_dir / sub
        if p.exists():
            path_parts.append(str(p))
    portable_path = os.pathsep.join(path_parts)
    if portable_path:
        portable_path += os.pathsep + os.environ.get("PATH", "")
    else:
        portable_path = os.environ.get("PATH", "")

    # 4. Конфиг
    config = {
        "mcpServers": {
            "mcp_unified": {
                "command": str(python_path).replace("\\", "\\\\"),
                "args": [str(server_script).replace("\\", "\\\\")],
                "env": {
                    "PYTHONIOENCODING": "utf-8",
                    "PATH": portable_path.replace("\\", "\\\\"),
                    "MCP_MEMORY_PATH": str(script_dir / "mcp_memory.db").replace("\\", "\\\\"),
                    "MCP_OFFLINE_MODE": "auto",
                    "MCP_AUTO_INDEX_SEARCH": "true",
                    # Бюджет контекста: 300+ инструментов не влезают в окно 32k.
                    # Профили: minimal | fs | memory | web | office | dev | all
                    "MCP_TOOLS_PROFILE": os.environ.get("MCP_TOOLS_PROFILE", "fs"),
                    "MCP_TOOLS_INCLUDE": os.environ.get("MCP_TOOLS_INCLUDE", "smart_search,help,mempalace_*"),
                    "MCP_TOOLS_EXCLUDE": os.environ.get("MCP_TOOLS_EXCLUDE", ""),
                    "MCP_TOOLS_SLIM": os.environ.get("MCP_TOOLS_SLIM", "true"),
                    "MCP_TOOLS_MAX_DESC": os.environ.get("MCP_TOOLS_MAX_DESC", "120")
                }
            }
        }
    }
    return config

def write_lmstudio_config(config):
    lm_studio_dir = Path.home() / ".lmstudio"
    lm_studio_dir.mkdir(parents=True, exist_ok=True)
    config_path = lm_studio_dir / "mcp.json"

    if config_path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = config_path.with_suffix(config_path.suffix + f".backup_{ts}")
        shutil.copy2(config_path, backup_path)
        print(f"💾 Backup created: {backup_path}")

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"✅ LM Studio config written to: {config_path}")
    print("   Restart LM Studio and select profile 'mcp_unified' to see all tools.")

if __name__ == "__main__":
    try:
        cfg = generate_correct_config()
        print("Generated config (using mcp_fs_server.py):")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        write_lmstudio_config(cfg)
    except Exception as e:
        print(f"❌ Error: {e}")
        input("\nPress Enter to exit...")