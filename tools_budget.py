#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools_budget.py — измеряет, сколько токенов контекста съедает список инструментов.

Запускает mcp_fs_server.py как подпроцесс, отправляет initialize + tools/list
и считает размер ответа для каждого профиля MCP_TOOLS_PROFILE.

Использование:
    python tools_budget.py                 # все профили
    python tools_budget.py fs memory       # только указанные
    python tools_budget.py --list all      # вывести имена инструментов профиля
"""
import os
import sys
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent
SERVER = ROOT / "mcp_fs_server.py"
PROFILES = ["minimal", "fs", "memory", "web", "office", "dev", "all"]


def query_tools(profile: str, slim: bool = False, max_desc: int = 0):
    """Поднимает сервер с заданным профилем и забирает tools/list."""
    env = os.environ.copy()
    env["MCP_TOOLS_PROFILE"] = profile
    env["MCP_TOOLS_SLIM"] = "true" if slim else "false"
    env["MCP_TOOLS_MAX_DESC"] = str(max_desc)
    env["PYTHONIOENCODING"] = "utf-8"

    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
    )

    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=requests, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=180,
    )

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 2 and "result" in msg:
            return msg["result"].get("tools", [])

    sys.stderr.write(f"[!] Профиль {profile}: ответ tools/list не получен\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr[-1500:] + "\n")
    return None


def report(tools, label: str):
    payload = json.dumps(tools, ensure_ascii=False)
    tokens = len(payload) // 4  # грубая оценка: ~4 символа на токен
    print(f"{label:<28} {len(tools):>4} инстр.  ~{tokens:>6} токенов  ({len(payload):>7} символов)")
    return tokens


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_names = "--list" in sys.argv
    profiles = args or PROFILES

    if not SERVER.exists():
        sys.exit(f"Не найден {SERVER}")

    print(f"Сервер: {SERVER}")
    print("=" * 74)
    for profile in profiles:
        tools = query_tools(profile)
        if tools is None:
            continue
        report(tools, f"profile={profile}")
        slim = query_tools(profile, slim=True, max_desc=120)
        if slim is not None:
            report(slim, f"  + SLIM, MAX_DESC=120")
        if show_names:
            for t in sorted(tools, key=lambda x: x["name"]):
                print(f"      {t['name']}")
        print("-" * 74)

    print("\nОриентир: оставьте запас минимум 4000 токенов на системный промпт")
    print("и историю диалога. При окне 32768 бюджет на инструменты — до ~20000.")


if __name__ == "__main__":
    main()
