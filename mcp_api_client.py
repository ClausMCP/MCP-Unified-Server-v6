#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP API Client v1.0 – HTTP-клиент для внешних API
Поддерживает GET, POST, PUT, DELETE, PATCH.
Управление токенами (Bearer, OAuth2 client credentials, refresh).
Повторные попытки при ошибках (retry), таймауты, логирование.
Хранит токены в keyring или в памяти.
"""
import os
import sys
import json
import time
import re
import threading
from typing import Dict, List, Optional, Any, Union
from urllib.parse import urlencode, urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Конфигурация ─────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = int(os.environ.get("MCP_API_TIMEOUT", "30"))
MAX_RETRIES = int(os.environ.get("MCP_API_MAX_RETRIES", "3"))
RETRY_BACKOFF_FACTOR = float(os.environ.get("MCP_API_RETRY_BACKOFF", "1.0"))
DEFAULT_USER_AGENT = "MCP-APIClient/1.0"

# Попытка импорта keyring для безопасного хранения токенов
try:
    import keyring
    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False

# ─── Глобальное хранилище токенов в памяти (запасной вариант) ────────────
_token_store = {}
_token_lock = threading.Lock()

# ─── Вспомогательные функции для работы с токенами ───────────────────────
def _get_service_name(service: str) -> str:
    """Возвращает имя сервиса для keyring (с префиксом)."""
    return f"MCP_API_{service}"

def _save_token_to_keyring(service: str, token: str):
    """Сохраняет токен в keyring."""
    if HAS_KEYRING:
        try:
            keyring.set_password(_get_service_name(service), "token", token)
            return True
        except Exception as e:
            _log(f"[APIClient] Failed to save token to keyring: {e}")
    return False

def _load_token_from_keyring(service: str) -> Optional[str]:
    """Загружает токен из keyring."""
    if HAS_KEYRING:
        try:
            return keyring.get_password(_get_service_name(service), "token")
        except Exception:
            pass
    return None

def _delete_token_from_keyring(service: str):
    """Удаляет токен из keyring."""
    if HAS_KEYRING:
        try:
            keyring.delete_password(_get_service_name(service), "token")
        except Exception:
            pass

def set_token(service: str, token: str, use_keyring: bool = True) -> Dict:
    """Сохранить токен для сервиса (Bearer)."""
    saved = False
    if use_keyring and HAS_KEYRING:
        saved = _save_token_to_keyring(service, token)
    if not saved:
        with _token_lock:
            _token_store[service] = token
    return {"status": "saved", "service": service, "storage": "keyring" if saved else "memory"}

def get_token(service: str, use_keyring: bool = True) -> Optional[str]:
    """Получить токен для сервиса."""
    if use_keyring and HAS_KEYRING:
        token = _load_token_from_keyring(service)
        if token:
            return token
    with _token_lock:
        return _token_store.get(service)

def delete_token(service: str) -> Dict:
    """Удалить токен для сервиса."""
    if HAS_KEYRING:
        _delete_token_from_keyring(service)
    with _token_lock:
        if service in _token_store:
            del _token_store[service]
    return {"status": "deleted", "service": service}

# ─── OAuth2 Client Credentials (получение токена по client_id/secret) ────
def oauth2_client_credentials(token_url: str, client_id: str, client_secret: str,
                              scope: str = "", timeout: int = DEFAULT_TIMEOUT) -> Dict:
    """
    Получает токен по схеме OAuth2 Client Credentials.
    Возвращает {"access_token": "...", "expires_in": 3600, "token_type": "Bearer"}
    """
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    if scope:
        data["scope"] = scope
    try:
        resp = requests.post(token_url, data=data, timeout=timeout)
        resp.raise_for_status()
        token_data = resp.json()
        # Сохраняем токен автоматически, если есть access_token
        if "access_token" in token_data:
            set_token(f"oauth2_{client_id}", token_data["access_token"])
        return token_data
    except Exception as e:
        return {"error": str(e)}

def oauth2_refresh_token(token_url: str, refresh_token: str, client_id: str = None,
                         client_secret: str = None, timeout: int = DEFAULT_TIMEOUT) -> Dict:
    """Обновляет токен с использованием refresh_token."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    if client_id:
        data["client_id"] = client_id
    if client_secret:
        data["client_secret"] = client_secret
    try:
        resp = requests.post(token_url, data=data, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

# ─── Основной клиент с повторными попытками ──────────────────────────────
def _create_session(headers: Dict = None, retries: int = MAX_RETRIES) -> requests.Session:
    """Создаёт сессию с настроенными повторными попытками."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json"
    })
    if headers:
        session.headers.update(headers)
    
    retry_strategy = Retry(
        total=retries,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE", "PATCH"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def _add_auth_headers(headers: Dict, service: str = None, token: str = None) -> Dict:
    """Добавляет Bearer-токен в заголовки, если указан service или token."""
    headers = headers or {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif service:
        token = get_token(service)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers

def api_request(method: str, url: str, params: Dict = None, json_data: Any = None,
                data: Any = None, headers: Dict = None, service: str = None,
                token: str = None, timeout: int = DEFAULT_TIMEOUT,
                retries: int = MAX_RETRIES) -> Dict:
    """
    Универсальный HTTP-запрос с поддержкой токенов и повторных попыток.
    """
    dialog_id = dialog_ctx.get()
    start_time = time.time()
    
    # Подготовка заголовков
    req_headers = _add_auth_headers(headers, service, token)
    
    # Создаём сессию
    session = _create_session(req_headers, retries)
    
    try:
        response = session.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_data,
            data=data,
            timeout=timeout
        )
        elapsed = time.time() - start_time
        
        # Пытаемся распарсить JSON
        try:
            response_json = response.json()
        except Exception:
            response_json = None
        
        result = {
            "status_code": response.status_code,
            "ok": response.ok,
            "elapsed_sec": round(elapsed, 2),
            "url": response.url,
            "headers": dict(response.headers),
            "body": response.text[:10000] if response.text else "",  # ограничение
            "json": response_json,
            "size_bytes": len(response.content)
        }
        
        conversation_memory.add(
            op="api_request",
            paths={"url": url, "method": method},
            status="success" if response.ok else "error",
            dialog=dialog_id,
            context=f"API {method} {url} → {response.status_code} in {elapsed:.2f}s"
        )
        return result
    except requests.exceptions.Timeout:
        return {"error": f"Request timeout after {timeout}s", "url": url, "method": method}
    except requests.exceptions.ConnectionError as e:
        return {"error": f"Connection error: {e}", "url": url, "method": method}
    except Exception as e:
        return {"error": str(e), "url": url, "method": method}
    finally:
        session.close()

# ─── Удобные обёртки ──────────────────────────────────────────────────────
def api_get(url: str, params: Dict = None, headers: Dict = None,
            service: str = None, token: str = None, timeout: int = DEFAULT_TIMEOUT) -> Dict:
    return api_request("GET", url, params=params, headers=headers, service=service,
                       token=token, timeout=timeout)

def api_post(url: str, json_data: Any = None, data: Any = None, headers: Dict = None,
             service: str = None, token: str = None, timeout: int = DEFAULT_TIMEOUT) -> Dict:
    return api_request("POST", url, json_data=json_data, data=data, headers=headers,
                       service=service, token=token, timeout=timeout)

def api_put(url: str, json_data: Any = None, data: Any = None, headers: Dict = None,
            service: str = None, token: str = None, timeout: int = DEFAULT_TIMEOUT) -> Dict:
    return api_request("PUT", url, json_data=json_data, data=data, headers=headers,
                       service=service, token=token, timeout=timeout)

def api_delete(url: str, headers: Dict = None, service: str = None,
               token: str = None, timeout: int = DEFAULT_TIMEOUT) -> Dict:
    return api_request("DELETE", url, headers=headers, service=service,
                       token=token, timeout=timeout)

def api_patch(url: str, json_data: Any = None, data: Any = None, headers: Dict = None,
              service: str = None, token: str = None, timeout: int = DEFAULT_TIMEOUT) -> Dict:
    return api_request("PATCH", url, json_data=json_data, data=data, headers=headers,
                       service=service, token=token, timeout=timeout)

# ─── Регистрация инструментов MCP ────────────────────────────────────────
def register_tools(server: BaseMCPServer):
    server.register_tool("api_get", {
        "description": "Выполнить GET запрос к внешнему API (поддерживает Bearer токены)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный URL запроса"},
                "params": {"type": "object", "description": "Параметры query string"},
                "headers": {"type": "object", "description": "Дополнительные заголовки"},
                "service": {"type": "string", "description": "Имя сервиса для автоматической подстановки токена (если сохранён)"},
                "token": {"type": "string", "description": "Bearer токен (если указан, переопределяет service)"},
                "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT}
            },
            "required": ["url"]
        }
    }, lambda **kw: api_get(kw["url"], kw.get("params"), kw.get("headers"),
                            kw.get("service"), kw.get("token"), kw.get("timeout", DEFAULT_TIMEOUT)))
    
    server.register_tool("api_post", {
        "description": "Выполнить POST запрос к внешнему API",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "json_data": {"type": "object", "description": "JSON тело запроса"},
                "data": {"type": ["object", "string"], "description": "Form data или строка"},
                "headers": {"type": "object"},
                "service": {"type": "string"},
                "token": {"type": "string"},
                "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT}
            },
            "required": ["url"]
        }
    }, lambda **kw: api_post(kw["url"], kw.get("json_data"), kw.get("data"),
                             kw.get("headers"), kw.get("service"), kw.get("token"),
                             kw.get("timeout", DEFAULT_TIMEOUT)))
    
    server.register_tool("api_put", {
        "description": "Выполнить PUT запрос к внешнему API",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "json_data": {"type": "object"},
                "data": {"type": ["object", "string"]},
                "headers": {"type": "object"},
                "service": {"type": "string"},
                "token": {"type": "string"},
                "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT}
            },
            "required": ["url"]
        }
    }, lambda **kw: api_put(kw["url"], kw.get("json_data"), kw.get("data"),
                            kw.get("headers"), kw.get("service"), kw.get("token"),
                            kw.get("timeout", DEFAULT_TIMEOUT)))
    
    server.register_tool("api_delete", {
        "description": "Выполнить DELETE запрос к внешнему API",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "headers": {"type": "object"},
                "service": {"type": "string"},
                "token": {"type": "string"},
                "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT}
            },
            "required": ["url"]
        }
    }, lambda **kw: api_delete(kw["url"], kw.get("headers"), kw.get("service"),
                               kw.get("token"), kw.get("timeout", DEFAULT_TIMEOUT)))
    
    server.register_tool("api_patch", {
        "description": "Выполнить PATCH запрос к внешнему API",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "json_data": {"type": "object"},
                "data": {"type": ["object", "string"]},
                "headers": {"type": "object"},
                "service": {"type": "string"},
                "token": {"type": "string"},
                "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT}
            },
            "required": ["url"]
        }
    }, lambda **kw: api_patch(kw["url"], kw.get("json_data"), kw.get("data"),
                              kw.get("headers"), kw.get("service"), kw.get("token"),
                              kw.get("timeout", DEFAULT_TIMEOUT)))
    
    server.register_tool("api_set_token", {
        "description": "Сохранить Bearer токен для сервиса (в keyring или памяти)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Имя сервиса (например, 'github', 'openweather')"},
                "token": {"type": "string"},
                "use_keyring": {"type": "boolean", "default": True}
            },
            "required": ["service", "token"]
        }
    }, lambda **kw: set_token(kw["service"], kw["token"], kw.get("use_keyring", True)))
    
    server.register_tool("api_get_token", {
        "description": "Получить сохранённый токен для сервиса",
        "inputSchema": {
            "type": "object",
            "properties": {"service": {"type": "string"}, "use_keyring": {"type": "boolean", "default": True}},
            "required": ["service"]
        }
    }, lambda **kw: {"token": get_token(kw["service"], kw.get("use_keyring", True))})
    
    server.register_tool("api_delete_token", {
        "description": "Удалить токен для сервиса",
        "inputSchema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"]
        }
    }, lambda **kw: delete_token(kw["service"]))
    
    server.register_tool("api_oauth2_client_credentials", {
        "description": "Получить токен через OAuth2 Client Credentials и сохранить его",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token_url": {"type": "string"},
                "client_id": {"type": "string"},
                "client_secret": {"type": "string"},
                "scope": {"type": "string", "default": ""},
                "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT}
            },
            "required": ["token_url", "client_id", "client_secret"]
        }
    }, lambda **kw: oauth2_client_credentials(
        kw["token_url"], kw["client_id"], kw["client_secret"],
        kw.get("scope", ""), kw.get("timeout", DEFAULT_TIMEOUT)
    ))
    
    server.register_tool("api_oauth2_refresh", {
        "description": "Обновить токен по refresh_token",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token_url": {"type": "string"},
                "refresh_token": {"type": "string"},
                "client_id": {"type": "string"},
                "client_secret": {"type": "string"},
                "timeout": {"type": "integer", "default": DEFAULT_TIMEOUT}
            },
            "required": ["token_url", "refresh_token"]
        }
    }, lambda **kw: oauth2_refresh_token(
        kw["token_url"], kw["refresh_token"], kw.get("client_id"),
        kw.get("client_secret"), kw.get("timeout", DEFAULT_TIMEOUT)
    ))

__mcp_plugin__ = {
    "name": "api-client",
    "version": "1.0",
    "description": "HTTP-клиент для внешних API с поддержкой токенов и OAuth2",
    "dependencies": ["requests"],
    "on_load": lambda: _log("[APIClient] Loaded. Use api_set_token() to store credentials."),
    "on_unload": lambda: _log("[APIClient] Unloaded.")
}

if __name__ == "__main__":
    server = BaseMCPServer("api-client", "1.0")
    register_tools(server)
    server.run()