#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Web Reader v4.3.1 – улучшенное извлечение: текст (trafilatura/readability),
таблицы (pandas / html-table-takeout), изображения, скриншоты, управление сессией,
обход Cloudflare, параллельная загрузка, кэширование поиска, фильтрация по дате,
улучшенный summary, автоиндексация RAG.
Интегрирован Rate Limiter и Circuit Breaker.
Добавлено (v4.3.1): Асинхронная индексация результатов поиска в RAG, force_refresh.
"""
import os
import re
import time
import json
import csv
import socket
import ipaddress
import threading
import pickle
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import requests
import random
from requests.exceptions import RequestException
from urllib.robotparser import RobotFileParser

# В самом верху файла (после импортов)
try:
    from mcp_background_indexer import _indexer  # запускает фоновый поток
except ImportError:
    pass
# Улучшенные библиотеки для извлечения контента (опционально)
try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

try:
    from readability import Document
    READABILITY_AVAILABLE = True
except ImportError:
    READABILITY_AVAILABLE = False

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

try:
    from html_table_takeout import parse_html
    HTML_TABLE_TAKEOUT = True
except ImportError:
    HTML_TABLE_TAKEOUT = False

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from playwright.sync_api import sync_playwright
    PW_AVAILABLE = True
except ImportError:
    sync_playwright = None
    PW_AVAILABLE = False

from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

try:
    from mcp_shared import is_online
except ImportError:
    def is_online(): return True

from mcp_rate_limiter import safe_call, circuit_breaker, rate_limiter

__mcp_plugin__ = {
    "name": "web-reader",
    "version": "4.3.1",
    "description": "Расширенный веб-ридер: текст, таблицы, изображения, скриншоты, сессии, Cloudflare bypass, параллельная загрузка, кэш поиска, локальный FTS-кэш страниц, RAG-индексация",
    "dependencies": ["requests", "bs4", "feedparser", "playwright", "trafilatura", "readability", "pandas", "html_table_takeout"],
    "on_load": lambda: _log("[web-reader] v4.3.1 loaded. Local FTS cache, search cache, RAG auto-indexing, date filters active."),
    "on_unload": lambda: _log("[web-reader] Unloaded.")
}

# Конфигурация
USER_AGENT = "MCP-WebReader/4.3.1 (Educational; +https://mcp.local)"

# Реалистичные User-Agent для ротации при рендеринге страниц через Playwright.
# Это эмуляция обычного браузера (НЕ обход капчи) — снижает тривиальное
# детектирование автоматизации на JS-страницах.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


def _get_random_ua() -> str:
    """Случайный реалистичный User-Agent (или из MCP_USER_AGENT, если задан)."""
    forced = os.environ.get("MCP_USER_AGENT")
    if forced:
        return forced
    try:
        return random.choice(USER_AGENTS)
    except Exception:
        return USER_AGENTS[0]


def _get_proxies():
    """Прокси из MCP_PROXY (http(s)://[user:pass@]host:port) — или None."""
    proxy = os.environ.get("MCP_PROXY")
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


# Лёгкий init-скрипт «обычного браузера»: убирает navigator.webdriver и
# выставляет языки/плагины. Не решает капчи и не обходит челленджи —
# только маскирует очевидные признаки headless-автоматизации.
_STEALTH_INIT_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'languages',{get:()=>['ru-RU','ru','en-US','en']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
)
ROBOTS_TTL = 3600
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 30
MAX_BODY_SIZE_MB = 10
MAX_CRAWL_PAGES = 50
MAX_CRAWL_DEPTH = 3
ALLOWED_SCHEMES = ("http://", "https://")
_BLOCKED_NETS = [
    ipaddress.IPv4Network("127.0.0.0/8"), ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"), ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"), ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"), ipaddress.IPv6Network("fe80::/10")
]

_robots_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()

# ====================== Кэширование поиска и утилиты ======================
def _get_cache_db_path() -> Path:
    """Возвращает путь к базе кэша поиска в домашней директории."""
    return Path.home() / ".mcp_search_cache.db"

def _init_search_cache():
    """Инициализирует таблицу кэша поиска."""
    cache_db = _get_cache_db_path()
    conn = None
    try:
        conn = sqlite3.connect(str(cache_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                query TEXT PRIMARY KEY,
                results TEXT NOT NULL,
                ts REAL NOT NULL,
                params_hash TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON search_cache(ts)")
        conn.commit()
    except Exception as e:
        _log(f"Failed to init search cache: {e}")
    finally:
        if conn:
            conn.close()

def _hash_params(kwargs: Dict) -> str:
    """Создаёт хэш параметров для учёта в кэше."""
    import hashlib
    param_str = json.dumps(kwargs, sort_keys=True, default=str)
    return hashlib.md5(param_str.encode()).hexdigest()

def cached_search(query: str, max_age_hours: int = 24, use_cache: bool = True, **kwargs) -> Dict:
    """Выполняет поиск с кэшированием результатов в SQLite."""
    if not use_cache:
        return web_search_enhanced(query, **kwargs)

    _init_search_cache()
    cache_db = _get_cache_db_path()
    params_hash = _hash_params(kwargs)
    conn = None
    try:
        conn = sqlite3.connect(str(cache_db))
        row = conn.execute(
            "SELECT results, ts FROM search_cache WHERE query = ? AND params_hash = ?",
            (query, params_hash)
        ).fetchone()

        if row and (time.time() - row[1]) < max_age_hours * 3600:
            _log(f"[cached_search] Cache hit for query: {query[:50]}...")
            return json.loads(row[0])

        _log(f"[cached_search] Cache miss, fetching: {query[:50]}...")
        results = web_search_enhanced(query, **kwargs)
        if results.get("status") == "success":
            conn.execute(
                "INSERT OR REPLACE INTO search_cache VALUES (?, ?, ?, ?)",
                (query, json.dumps(results, default=str), time.time(), params_hash)
            )
            conn.commit()
            _log(f"[cached_search] Cached {len(results.get('results', []))} results")
        return results
    except Exception as e:
        _log(f"[cached_search] Error: {e}")
        return web_search_enhanced(query, **kwargs)
    finally:
        if conn:
            conn.close()

def _ddg_date_filter(days_back: int) -> Optional[str]:
    if days_back <= 1: return "d"
    elif days_back <= 7: return "w"
    elif days_back <= 30: return "m"
    elif days_back <= 365: return "y"
    else: return None

# ====================== Фильтрация по дате ======================
def web_search_dated(query: str, days_back: int = 7, max_results: int = 10,
                     sources: List[str] = None, timeout: int = 30) -> Dict:
    d_id = dialog_ctx.get()
    if sources is None: sources = ["duckduckgo"]

    def _search_dated():
        all_results = []
        seen_urls = set()
        date_filter = _ddg_date_filter(days_back)

        if "duckduckgo" in sources and BeautifulSoup:
            encoded_query = urllib.parse.quote(query)
            search_url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
            if date_filter: search_url += f"&df={date_filter}"

            try:
                session = requests.Session()
                session.headers.update({"User-Agent": USER_AGENT})
                resp = session.get(search_url, timeout=timeout)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                for result in soup.select(".result"):
                    title_elem = result.select_one(".result__a")
                    snippet_elem = result.select_one(".result__snippet")
                    if not title_elem: continue

                    raw_link = title_elem.get("href", "")
                    real_url = raw_link
                    if raw_link and raw_link.startswith("/"):
                        from urllib.parse import urlparse, parse_qs
                        parsed = urlparse(raw_link)
                        if parsed.path == "/l/":
                            qs = parse_qs(parsed.query)
                            if "uddg" in qs: real_url = urllib.parse.unquote(qs["uddg"][0])
                            else: real_url = "https://duckduckgo.com" + raw_link
                        else: real_url = "https://duckduckgo.com" + raw_link

                    title = title_elem.get_text(strip=True)
                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

                    if real_url not in seen_urls:
                        seen_urls.add(real_url)
                        all_results.append({
                            "title": title, "snippet": summarize_result(snippet),
                            "url": real_url, "source": "duckduckgo",
                            "date_filter": f"{days_back}d" if date_filter else None
                        })
                    if len(all_results) >= max_results: break
            except Exception as e:
                _log(f"DuckDuckGo dated search error: {e}")

        final_results = all_results[:max_results]
        conversation_memory.add(op="web_search_dated", paths={"query": query}, status="success", dialog=d_id,
                                context=f"Dated search: {len(final_results)} results, filter={days_back}d")
        return {
            "status": "success", "query": query, "date_filter_days": days_back,
            "count": len(final_results), "results": final_results,
            "markdown_results": [{"markdown": f"[{r['title']}]({r['url']})", "snippet": r['snippet']} for r in final_results]
        }

    return safe_call("web_search_dated", _search_dated)

# ====================== Улучшенный summary ======================
def summarize_result(snippet: str, max_len: int = 200, sentences: int = 2) -> str:
    if not snippet: return ""
    cleaned = re.sub(r'\s+', ' ', snippet).strip()
    sentences_list = re.split(r'(?<=[.!?])\s+', cleaned)
    selected = sentences_list[:sentences]
    result = ' '.join(selected)
    if len(result) > max_len:
        result = result[:max_len].rsplit(' ', 1)[0] + '…'
    return result.strip()

def summarize_search_results(results: List[Dict], max_snippet_len: int = 150) -> List[Dict]:
    summarized = []
    for r in results:
        new_r = r.copy()
        if "snippet" in new_r:
            new_r["snippet"] = summarize_result(new_r["snippet"], max_len=max_snippet_len)
        summarized.append(new_r)
    return summarized

# ====================== Базовые функции ======================
def _get_domain(url: str) -> Optional[str]:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme + "://" + parsed.hostname if parsed.hostname else None
    except Exception:
        return None

def _get_robots_parser(base_url: str) -> RobotFileParser:
    with _cache_lock:
        cached = _robots_cache.get(base_url)
        if cached and time.time() < cached["expires"]: return cached["parser"]
    
    parser = RobotFileParser()
    robots_url = urllib.parse.urljoin(base_url, "/robots.txt")
    try:
        resp = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=10, allow_redirects=True)
        parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    except Exception:
        parser.parse([])
    
    with _cache_lock:
        _robots_cache[base_url] = {"parser": parser, "expires": time.time() + ROBOTS_TTL}
    return parser

def _is_allowed_by_robots(url: str) -> bool:
    base = _get_domain(url)
    if not base: return False
    return _get_robots_parser(base).can_fetch(USER_AGENT, url)

def _is_safe_host(host: str) -> bool:
    try:
        if host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "metadata.google.internal"):
            return False
        for ip in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            if any(ipaddress.ip_address(ip[4][0]) in net for net in _BLOCKED_NETS):
                return False
        return True
    except Exception:
        return False

def _extract_main_text(html: str, url: str) -> str:
    if TRAFILATURA_AVAILABLE:
        text = trafilatura.extract(html, include_comments=False, include_tables=False)
        if text: return text
    if READABILITY_AVAILABLE:
        try:
            doc = Document(html)
            return doc.summary()
        except: pass
    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "iframe", "noscript", "form", "meta", "link", "head"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    return re.sub(r"<[^>]+>", "", html)

def _extract_tables(html: str, url: str) -> List[Dict]:
    tables = []
    if PANDAS_AVAILABLE:
        try:
            dfs = pd.read_html(html)
            for i, df in enumerate(dfs):
                tables.append({"index": i, "rows": len(df), "columns": list(df.columns),
                               "data": df.fillna("").to_dict(orient="records")[:100]})
            return tables
        except Exception as e:
            _log(f"pandas read_html failed: {e}")
    
    if HTML_TABLE_TAKEOUT:
        try:
            parsed = parse_html(html)
            for i, table in enumerate(parsed):
                tables.append({"index": i, "data": table})
            return tables
        except Exception as e:
            _log(f"html-table-takeout failed: {e}")
            
    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for i, table in enumerate(soup.find_all("table")):
            rows = []
            for tr in table.find_all("tr"):
                row = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if row: rows.append(row)
            tables.append({"index": i, "rows": len(rows), "data": rows[:50]})
    return tables

def _extract_images(html: str, base_url: str) -> List[Dict]:
    images = []
    if not BeautifulSoup: return images
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src: continue
        if src.startswith("//"): src = "https:" + src
        elif src.startswith("/"): src = urllib.parse.urljoin(base_url, src)
        images.append({"src": src, "alt": img.get("alt", ""), "title": img.get("title", ""),
                       "width": img.get("width", ""), "height": img.get("height", "")})
    return images[:50]

# ====================== Web Cache (FTS5) ======================
def _get_web_cache_db_path() -> Path:
    """Возвращает путь к базе локального кэша страниц."""
    return Path.home() / ".mcp_web_cache.db"

def _init_web_cache():
    """Инициализирует таблицы для локального кэша страниц и FTS5."""
    cache_db = _get_web_cache_db_path()
    conn = None
    try:
        conn = sqlite3.connect(str(cache_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_cache (
                url TEXT PRIMARY KEY, text TEXT NOT NULL, title TEXT,
                metadata TEXT, ts REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS web_cache_fts USING fts5(
                url, text, content='web_cache', content_rowid='rowid'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS web_cache_ai AFTER INSERT ON web_cache BEGIN
                INSERT INTO web_cache_fts(rowid, url, text) VALUES (new.rowid, new.url, new.text);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS web_cache_ad AFTER DELETE ON web_cache BEGIN
                INSERT INTO web_cache_fts(web_cache_fts, rowid, url, text) VALUES('delete', old.rowid, old.url, old.text);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS web_cache_au AFTER UPDATE ON web_cache BEGIN
                INSERT INTO web_cache_fts(web_cache_fts, rowid, url, text) VALUES('delete', old.rowid, old.url, old.text);
                INSERT INTO web_cache_fts(rowid, url, text) VALUES (new.rowid, new.url, new.text);
            END;
        """)
        conn.commit()
    except Exception as e:
        _log(f"Failed to init web cache: {e}")
    finally:
        if conn: conn.close()

def _get_from_web_cache(url: str, ttl_hours: int = 24) -> Optional[Dict]:
    _init_web_cache()
    cache_db = _get_web_cache_db_path()
    conn = None
    try:
        conn = sqlite3.connect(str(cache_db))
        row = conn.execute("SELECT text, title, metadata, ts FROM web_cache WHERE url = ?", (url,)).fetchone()
        if row and (time.time() - row[3]) < ttl_hours * 3600:
            metadata = json.loads(row[2]) if row[2] else {}
            return {
                "url": url, "status": 200, "content_type": metadata.get("content_type", "text/html"),
                "text": row[0], "length_chars": metadata.get("length_chars", len(row[0])),
                "title": row[1], "images": metadata.get("images", []),
                "tables": metadata.get("tables", []), "cached": True
            }
    except Exception as e:
        _log(f"[web_cache] Read error: {e}")
    finally:
        if conn: conn.close()
    return None

def _save_to_web_cache(url: str, text: str, title: str, metadata: Dict):
    _init_web_cache()
    cache_db = _get_web_cache_db_path()
    conn = None
    try:
        conn = sqlite3.connect(str(cache_db))
        conn.execute("""
            INSERT OR REPLACE INTO web_cache (url, text, title, metadata, ts)
            VALUES (?, ?, ?, ?, ?)
        """, (url, text, title, json.dumps(metadata, default=str), time.time()))
        conn.commit()
    except Exception as e:
        _log(f"[web_cache] Write error: {e}")
    finally:
        if conn: conn.close()

def search_web_cache(query: str, limit: int = 10) -> Dict:
    """Ищет по локальному кэшу с использованием FTS5."""
    _init_web_cache()
    cache_db = _get_web_cache_db_path()
    conn = None
    try:
        conn = sqlite3.connect(str(cache_db))
        rows = conn.execute("""
            SELECT c.url,
                   snippet(web_cache_fts, 1, '<b>', '</b>', '...', 32) as snippet,
                   c.title
            FROM web_cache_fts f
            JOIN web_cache c ON f.rowid = c.rowid
            WHERE web_cache_fts MATCH ? LIMIT ?
        """, (query, limit)).fetchall()
        return {"status": "success", "count": len(rows),
                "results": [{"url": r[0], "snippet": r[1], "title": r[2]} for r in rows]}
    except Exception as e:
        _log(f"[search_web_cache] Error: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if conn: conn.close()

# ====================== Основные функции загрузки ======================
def fetch_url_enhanced(url: str, extract_tables: bool = True, extract_images: bool = True,
                       timeout: int = DEFAULT_TIMEOUT, max_size_mb: int = MAX_BODY_SIZE_MB,
                       use_cache: bool = True, ttl_hours: int = 24, force_refresh: bool = False) -> Dict:
    d_id = dialog_ctx.get()
    if not url.startswith(ALLOWED_SCHEMES):
        return {"error": "Only HTTP/HTTPS allowed."}
    if not _is_safe_host(urllib.parse.urlparse(url).hostname):
        return {"error": "SSRF prevention: Host blocked."}
    if not _is_allowed_by_robots(url):
        return {"error": "robots.txt denies access."}

    # 1. Проверка локального кэша
    if use_cache and not force_refresh:
        cached_result = _get_from_web_cache(url, ttl_hours)
        if cached_result:
            _log(f"[fetch_url_enhanced] Cache hit for URL: {url[:50]}...")
            conversation_memory.add(op="fetch_url_enhanced", paths={"url": url}, status="success", dialog=d_id,
                                    context="Served from local FTS cache")
            return cached_result

    def _fetch():
        session = None
        try:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            session.max_redirects = MAX_REDIRECTS
            resp = session.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "").lower()
            
            max_bytes = max_size_mb * 1024 * 1024
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=8192):
                total += len(chunk)
                if total > max_bytes:
                    return {"error": "Content exceeds size limit."}
                chunks.append(chunk)
            
            body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
            text = _extract_main_text(body, url) if "html" in ct else body
            
            result = {
                "url": url, "status": resp.status_code, "content_type": ct,
                "text": text, "length_chars": len(text),
                "title": "", "images": [], "tables": [], "cached": False
            }
            
            if BeautifulSoup and "html" in ct:
                soup = BeautifulSoup(body, "html.parser")
                title_tag = soup.find("title")
                if title_tag: result["title"] = title_tag.get_text(strip=True)
            if extract_tables and "html" in ct:
                result["tables"] = _extract_tables(body, url)
            if extract_images and "html" in ct:
                result["images"] = _extract_images(body, url)

            # 2. Сохранение в локальный кэш
            if use_cache and resp.status_code == 200:
                metadata = {
                    "tables": result.get("tables", []),
                    "images": result.get("images", []),
                    "length_chars": len(text),
                    "content_type": ct
                }
                _save_to_web_cache(url, text, result.get("title", ""), metadata)

            conversation_memory.add(op="fetch_url_enhanced", paths={"url": url}, status="success", dialog=d_id,
                                    context=f"Extracted {len(text)} chars, {len(result['tables'])} tables, {len(result['images'])} images")
            return result
        except RequestException as e:
            return {"error": f"Request failed: {e}"}
        finally:
            if session: session.close()

    return safe_call("web_fetch_enhanced", _fetch)

def index_search_results(query: str, results: List[Dict], max_pages: int = 3):
    """Фоновое индексирование найденных страниц в локальную базу знаний (RAG)."""
    for res in results[:max_pages]:
        url = res.get("url")
        if not url:
            continue
        try:
            # Получаем текст через fetch_url_enhanced (использует локальный FTS кэш или загружает)
            page = fetch_url_enhanced(url, extract_tables=False, extract_images=False, use_cache=True)
            if "error" not in page and page.get("text"):
                try:
                    from mcp_rag import add_document
                    add_document(page["text"], metadata={"url": url, "title": page.get("title", ""), "query": query})
                except ImportError:
                    _log(f"[index_search] mcp_rag module not available, skipping RAG indexing for {url}")
                except Exception as e:
                    _log(f"[index_search] Error adding to RAG for {url}: {e}")
        except Exception as e:
            _log(f"[index_search] Error fetching {url} for indexing: {e}")

def web_search_enhanced(query: str, max_results: int = 15, sources: List[str] = None,
                        timeout: int = 30, summarize: bool = False) -> Dict:
    d_id = dialog_ctx.get()
    if sources is None: sources = ["duckduckgo"]

    def _search():
        all_results = []
        seen_urls = set()

        if "duckduckgo" in sources:
            encoded_query = urllib.parse.quote(query)
            search_url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
            if BeautifulSoup:
                try:
                    session = requests.Session()
                    session.headers.update({"User-Agent": USER_AGENT})
                    resp = session.get(search_url, timeout=timeout)
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "html.parser")

                    for result in soup.select(".result"):
                        title_elem = result.select_one(".result__a")
                        snippet_elem = result.select_one(".result__snippet")
                        if not title_elem: continue

                        raw_link = title_elem.get("href", "")
                        real_url = raw_link
                        if raw_link and raw_link.startswith("/"):
                            from urllib.parse import urlparse, parse_qs
                            parsed = urlparse(raw_link)
                            if parsed.path == "/l/":
                                qs = parse_qs(parsed.query)
                                if "uddg" in qs: real_url = urllib.parse.unquote(qs["uddg"][0])
                                else: real_url = "https://duckduckgo.com" + raw_link
                            else: real_url = "https://duckduckgo.com" + raw_link

                        title = title_elem.get_text(strip=True)
                        snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

                        if real_url not in seen_urls:
                            seen_urls.add(real_url)
                            processed_snippet = summarize_result(snippet) if summarize else snippet[:500]
                            all_results.append({"title": title, "snippet": processed_snippet, "url": real_url, "source": "duckduckgo"})
                        if len(all_results) >= max_results: break
                except Exception as e:
                    _log(f"DuckDuckGo search error: {e}")

        if "brave" in sources:
            api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
            if api_key:
                try:
                    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
                    params = {"q": query, "count": max_results}
                    resp = requests.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params=params, timeout=timeout, proxies=_get_proxies())
                    if resp.status_code == 200:
                        data = resp.json()
                        for web in data.get("web", {}).get("results", []):
                            url = web.get("url")
                            if url and url not in seen_urls:
                                seen_urls.add(url)
                                processed_snippet = summarize_result(web.get("description", "")) if summarize else web.get("description", "")[:500]
                                all_results.append({"title": web.get("title", ""), "snippet": processed_snippet, "url": url, "source": "brave"})
                            if len(all_results) >= max_results: break
                except Exception as e:
                    _log(f"Brave search error: {e}")

        if "searxng" in sources or "searx" in sources:
            # Локальный (или свой) инстанс SearXNG — без ключей и без капч.
            base = os.environ.get("MCP_SEARXNG_URL", "http://localhost:8888").rstrip("/")
            try:
                params = {"q": query, "format": "json"}
                lang = os.environ.get("MCP_SEARXNG_LANG")
                if lang:
                    params["language"] = lang
                headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
                resp = requests.get(f"{base}/search", params=params, headers=headers, timeout=timeout, proxies=_get_proxies())
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("results", []):
                        url = item.get("url")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            snip = item.get("content", "") or ""
                            processed_snippet = summarize_result(snip) if summarize else snip[:500]
                            all_results.append({"title": item.get("title", ""), "snippet": processed_snippet,
                                                "url": url, "source": "searxng"})
                        if len(all_results) >= max_results:
                            break
                else:
                    _log(f"SearXNG returned HTTP {resp.status_code} from {base}")
            except Exception as e:
                _log(f"SearXNG search error ({base}): {e}")

        final_results = all_results[:max_results]
        
        # Асинхронное индексирование результатов поиска в RAG
        if os.environ.get("MCP_AUTO_INDEX_SEARCH", "true").lower() == "true":
            threading.Thread(target=index_search_results, args=(query, final_results), daemon=True).start()

        conversation_memory.add(op="web_search_enhanced", paths={"query": query}, status="success",
                                dialog=d_id, context=f"Found {len(final_results)} results from {len(sources)} sources")
        return {
            "status": "success", "query": query, "count": len(final_results), "results": final_results,
            "markdown_results": [{"markdown": f"[{r['title']}]({r['url']})", "snippet": r['snippet']} for r in final_results]
        }

    return safe_call("web_search", _search)

def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT, max_size_mb: int = MAX_BODY_SIZE_MB, 
              use_cache: bool = True, force_refresh: bool = False) -> Dict:
    return fetch_url_enhanced(url, extract_tables=False, extract_images=False,
                              timeout=timeout, max_size_mb=max_size_mb, use_cache=use_cache,
                              force_refresh=force_refresh)

def web_search(query: str, max_results: int = None, timeout: int = 30) -> Dict:
    if max_results is None:
        max_results = int(os.environ.get("MCP_WEB_RESULTS", "15"))
    return web_search_enhanced(query, max_results=max_results, sources=["duckduckgo"], timeout=timeout)

def read_rss(feed_url: str) -> Dict:
    d_id = dialog_ctx.get()
    if not feed_url.startswith(ALLOWED_SCHEMES): return {"error": "Invalid scheme."}
    if not _is_safe_host(urllib.parse.urlparse(feed_url).hostname): return {"error": "SSRF prevention."}
    if not feedparser: return {"error": "feedparser not installed. pip install feedparser"}

    def _read():
        session = None
        try:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            resp = session.get(feed_url, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            entries = [{"title": e.get("title", ""), "link": e.get("link", ""), "published": e.get("published", ""),
                        "summary": summarize_result(e.get("summary", ""), max_len=300)} for e in parsed.entries[:20]]
            conversation_memory.add(op="read_rss", paths={"url": feed_url}, status="success", dialog=d_id, context=f"Parsed {len(entries)} entries")
            return {"url": feed_url, "title": parsed.feed.get("title", ""), "entries": entries}
        except Exception as e:
            return {"error": str(e)}
        finally:
            if session: session.close()

    return safe_call("rss_feed", _read)

def download_file(url: str, destination: str) -> Dict:
    d_id = dialog_ctx.get()
    if not url.startswith(ALLOWED_SCHEMES): return {"error": "Invalid scheme."}
    if not _is_safe_host(urllib.parse.urlparse(url).hostname): return {"error": "SSRF prevention."}
    
    dest = Path(normalize_path(destination))
    _ensure_allowed(dest.parent, "download_file")

    def _download():
        session = None
        try:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            resp = session.get(url, stream=True, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            written = 0
            max_bytes = MAX_BODY_SIZE_MB * 1024 * 1024
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
                        if written > max_bytes:
                            dest.unlink(missing_ok=True)
                            return {"error": "Exceeded size limit."}
            conversation_memory.add(op="download_file", paths={"src": url, "dst": str(dest)}, status="success", dialog=d_id, context=f"Downloaded {written:,} bytes")
            return {"status": "success", "url": url, "destination": str(dest), "size_bytes": written}
        except Exception as e:
            if dest.exists(): dest.unlink(missing_ok=True)
            return {"error": str(e)}
        finally:
            if session: session.close()

    return safe_call("download", _download)

def crawl_deep_links(start_url: str, max_depth: int = MAX_CRAWL_DEPTH, max_pages: int = MAX_CRAWL_PAGES, same_domain: bool = True) -> Dict:
    d_id = dialog_ctx.get()
    if not start_url.startswith(ALLOWED_SCHEMES): return {"error": "Invalid scheme."}
    if not _is_safe_host(urllib.parse.urlparse(start_url).hostname): return {"error": "SSRF prevention."}
    if not BeautifulSoup: return {"error": "BeautifulSoup not installed. pip install beautifulsoup4"}

    def _crawl():
        start_domain = _get_domain(start_url)
        visited: Set[str] = set()
        results = []
        queue = [(start_url, 0)]

        while queue and len(visited) < max_pages:
            url, depth = queue.pop(0)
            if url in visited: continue
            if not _is_allowed_by_robots(url): continue
            visited.add(url)

            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
                if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", "").lower(): continue
                
                soup = BeautifulSoup(resp.text, "html.parser")
                title = soup.find("title")
                title = title.text.strip() if title else ""
                text = summarize_result(soup.get_text(separator=" ", strip=True)[:500])
                results.append({"url": url, "title": title, "depth": depth, "preview": text, "status": 200})

                if depth < max_depth:
                    for a in soup.find_all("a", href=True):
                        href = urllib.parse.urljoin(url, a["href"])
                        clean = href.split("#")[0]
                        if clean.startswith(ALLOWED_SCHEMES) and clean not in visited:
                            if same_domain and _get_domain(clean) != start_domain: continue
                            queue.append((clean, depth + 1))
            except Exception:
                results.append({"url": url, "title": "Error", "depth": depth, "preview": "Fetch failed", "status": 0})

        conversation_memory.add(op="crawl_deep_links", paths={"url": start_url}, status="success", dialog=d_id, context=f"Crawled {len(results)} pages, depth {max_depth}")
        return {"start_url": start_url, "pages_found": len(results), "max_depth_reached": max_depth, "data": results}

    return safe_call("crawl", _crawl)

def fetch_dynamic_js(url: str, wait_for_selector: Optional[str] = None, js_eval: Optional[str] = None,
                     timeout: int = 30000, bypass_cloudflare: bool = False) -> Dict:
    d_id = dialog_ctx.get()
    if not PW_AVAILABLE: return {"error": "Playwright not installed. pip install playwright && playwright install chromium"}
    if not url.startswith(ALLOWED_SCHEMES): return {"error": "Invalid scheme."}
    if not _is_safe_host(urllib.parse.urlparse(url).hostname): return {"error": "SSRF prevention."}
    if not _is_allowed_by_robots(url): return {"error": "robots.txt denies access."}

    def _fetch():
        p = None
        browser = None
        try:
            p = sync_playwright().start()
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_get_random_ua(),
                viewport={"width": 1920, "height": 1080},
                locale="ru-RU",
            )
            # эмуляция обычного браузера (не обход капчи) — снять очевидные
            # признаки headless-автоматизации перед загрузкой страницы
            try:
                context.add_init_script(_STEALTH_INIT_JS)
            except Exception:
                pass
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout)
            if bypass_cloudflare:
                try:
                    page.wait_for_selector("#challenge-running", state="detached", timeout=15000)
                    page.wait_for_selector(".cf-browser-verification", state="detached", timeout=15000)
                except Exception: pass
                page.wait_for_timeout(5000)
                if "cf-challenge" in page.content().lower() or "just a moment" in page.title().lower():
                    page.wait_for_timeout(10000)
            
            if wait_for_selector: page.wait_for_selector(wait_for_selector, timeout=timeout)
            if js_eval: page.evaluate(js_eval)
            time.sleep(1)
            
            content = page.content()
            title = page.title()
            text = _extract_main_text(content, url)
            conversation_memory.add(op="fetch_dynamic_js", paths={"url": url}, status="success", dialog=d_id, context="Rendered JS content successfully")
            return {"url": url, "title": title, "text": text, "rendered": True, "length_chars": len(text), "cloudflare_bypassed": bypass_cloudflare}
        except Exception as e:
            return {"error": f"Playwright failed: {e}"}
        finally:
            try:
                if browser: browser.close()
                if p: p.stop()
            except: pass

    return safe_call("js_render", _fetch)

def export_scraped_data(data: List[Dict], output_path: str, format: str = "json", delimiter: str = ",") -> Dict:
    d_id = dialog_ctx.get()
    if not data: return {"error": "No data provided for export."}
    dest = Path(normalize_path(output_path))
    _ensure_allowed(dest.parent, "export_scraped_data")
    _ensure_allowed(dest, "export_scraped_data")

    fmt = format.lower()
    if fmt not in ("json", "csv"): return {"error": "Format must be 'json' or 'csv'."}

    try:
        if fmt == "json":
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        else:
            keys = set()
            for d in data: keys.update(d.keys())
            headers = sorted(keys)
            with open(dest, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers, delimiter=delimiter, extrasaction="ignore")
                writer.writeheader()
                for row in data: writer.writerow(row)
        conversation_memory.add(op="export_scraped_data", paths={"dst": str(dest)}, status="success", dialog=d_id, context=f"Exported {len(data)} records to {fmt.upper()}")
        return {"status": "success", "path": str(dest), "records": len(data), "format": fmt}
    except Exception as e:
        return {"error": str(e)}

# ====================== Функции v4.1 (улучшенные) ======================
def capture_screenshot(url: str, output_path: str, full_page: bool = True,
                       wait_ms: int = 2000, width: int = 1920, height: int = 1080,
                       wait_until: str = "load", timeout_ms: int = 60000,
                       retries: int = 2) -> Dict:
    """
    Делает скриншот веб-страницы через Playwright с автоматическими повторами.

    Args:
        url: адрес страницы
        output_path: куда сохранить скриншот (PNG)
        full_page: скриншот всей страницы или только видимой области
        wait_ms: дополнительная задержка в миллисекундах после загрузки
        width, height: размер окна браузера
        wait_until: условие ожидания: "load", "domcontentloaded", "networkidle"
        timeout_ms: таймаут в миллисекундах (по умолч. 60 секунд)
        retries: количество повторных попыток при ошибке
    """
    d_id = dialog_ctx.get()
    if not PW_AVAILABLE:
        return {"error": "Playwright not installed. pip install playwright && playwright install chromium"}
    if not url.startswith(ALLOWED_SCHEMES):
        return {"error": "Invalid scheme."}
    if not _is_safe_host(urllib.parse.urlparse(url).hostname):
        return {"error": "SSRF prevention."}
    
    dest = Path(normalize_path(output_path))
    _ensure_allowed(dest.parent, "capture_screenshot")
    _ensure_allowed(dest, "capture_screenshot")

    for attempt in range(retries + 1):
        p = None
        browser = None
        try:
            p = sync_playwright().start()
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": width, "height": height}
            )
            page = context.new_page()
            # Используем указанное условие ожидания
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            # Дополнительная пауза, если нужно
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            page.screenshot(path=str(dest), full_page=full_page)
            file_size = dest.stat().st_size if dest.exists() else 0
            conversation_memory.add(
                op="capture_screenshot",
                paths={"url": url, "dst": str(dest)},
                status="success", dialog=d_id,
                context=f"Captured screenshot of {url} ({file_size:,} bytes), attempt {attempt+1}"
            )
            return {
                "status": "success", "url": url, "screenshot": str(dest),
                "full_page": full_page, "size_bytes": file_size,
                "dimensions": f"{width}x{height}", "attempts": attempt+1,
                "wait_until": wait_until, "timeout_ms": timeout_ms
            }
        except Exception as e:
            _log(f"Screenshot attempt {attempt+1} failed: {e}")
            if attempt == retries:
                return {"error": f"Screenshot failed after {retries+1} attempts: {e}"}
            time.sleep(2)  # пауза перед повтором
        finally:
            try:
                if browser: browser.close()
                if p: p.stop()
            except: pass
    return {"error": "Unexpected error in capture_screenshot"}

def webpage_to_pdf(url: str, output_path: str, timeout_ms: int = 60000,
                   retries: int = 2) -> Dict:
    """
    Сохраняет веб-страницу в PDF через Playwright.
    """
    d_id = dialog_ctx.get()
    if not PW_AVAILABLE:
        return {"error": "Playwright not installed."}
    if not url.startswith(ALLOWED_SCHEMES):
        return {"error": "Invalid scheme."}
    if not _is_safe_host(urllib.parse.urlparse(url).hostname):
        return {"error": "SSRF prevention."}
    
    dest = Path(normalize_path(output_path))
    _ensure_allowed(dest.parent, "webpage_to_pdf")
    _ensure_allowed(dest, "webpage_to_pdf")
    
    for attempt in range(retries + 1):
        p = None
        browser = None
        try:
            p = sync_playwright().start()
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(url, wait_until="load", timeout=timeout_ms)
            page.pdf(path=str(dest), format="A4", print_background=True)
            file_size = dest.stat().st_size if dest.exists() else 0
            conversation_memory.add(
                op="webpage_to_pdf",
                paths={"url": url, "dst": str(dest)},
                status="success", dialog=d_id,
                context=f"Saved PDF of {url} ({file_size:,} bytes)"
            )
            return {
                "status": "success", "url": url, "pdf": str(dest),
                "size_bytes": file_size, "attempts": attempt+1
            }
        except Exception as e:
            _log(f"PDF conversion attempt {attempt+1} failed: {e}")
            if attempt == retries:
                return {"error": f"Failed after {retries+1} attempts: {e}"}
            time.sleep(2)
        finally:
            try:
                if browser: browser.close()
                if p: p.stop()
            except: pass
    return {"error": "Unexpected error in webpage_to_pdf"}

def batch_fetch_with_retry(urls: List[str], max_concurrent: int = 2,
                           timeout: int = 60, extract_tables: bool = False,
                           extract_images: bool = False) -> Dict:
    """
    Параллельно пробует загрузить несколько URL, возвращает первый успешный результат.
    """
    d_id = dialog_ctx.get()
    if not urls:
        return {"error": "No URLs provided"}
    max_concurrent = min(max_concurrent, len(urls))
    
    results = []
    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        futures = {ex.submit(fetch_url_enhanced, url, extract_tables, extract_images,
                             timeout, 10, True, 24, False): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                res = future.result()
                if "error" not in res:
                    conversation_memory.add(
                        op="batch_fetch_with_retry",
                        paths={"urls": urls},
                        status="success", dialog=d_id,
                        context=f"Successful fetch from {url}"
                    )
                    res["successful_url"] = url
                    return res
                else:
                    results.append({"url": url, "error": res["error"]})
            except Exception as e:
                results.append({"url": url, "error": str(e)})
    # Все попытки неудачны
    return {"status": "error", "errors": results, "message": "All URLs failed"}

def capture_screenshot_with_fallback(url: str, output_path: str,
                                     retries: int = 3,
                                     timeout_ms: int = 60000) -> Dict:
    """
    Пытается сделать скриншот, меняя стратегии: сначала "load", потом "networkidle",
    а затем пробует через fetch_dynamic_js с обходом Cloudflare.
    """
    d_id = dialog_ctx.get()
    # Попытка 1: wait_until="load"
    res = capture_screenshot(
        url=url, output_path=output_path,
        full_page=True, wait_ms=2000,
        wait_until="load", timeout_ms=timeout_ms,
        retries=retries
    )
    if "error" not in res:
        return res
    
    # Попытка 2: wait_until="networkidle" (более долгая)
    res2 = capture_screenshot(
        url=url, output_path=output_path,
        full_page=True, wait_ms=3000,
        wait_until="networkidle", timeout_ms=timeout_ms,
        retries=retries
    )
    if "error" not in res2:
        return res2
    
    # Попытка 3: через fetch_dynamic_js (рендер с Playwright)
    try:
        js_res = fetch_dynamic_js(url, wait_for_selector="body",
                                  js_eval=None, timeout=timeout_ms,
                                  bypass_cloudflare=True)
        if "error" not in js_res and js_res.get("text"):
            # Сохраняем текст в файл, если не получился скриншот
            txt_path = Path(output_path).with_suffix(".txt")
            txt_path.write_text(js_res["text"], encoding="utf-8")
            return {
                "status": "partial", "url": url,
                "message": "Screenshot failed, but text extracted",
                "text_file": str(txt_path),
                "text_length": len(js_res["text"])
            }
    except Exception as e:
        _log(f"JS fallback failed: {e}")
    
    return {"error": f"All screenshot attempts failed for {url}", "details": [res, res2]}

# ====================== Регистрация инструментов ======================
def clear_search_cache(older_than_days: Optional[int] = None) -> Dict:
    """Очистка кэша поиска (~/.mcp_search_cache.db). older_than_days=None — весь кэш."""
    db = _get_search_cache_path() if "_get_search_cache_path" in globals() else (Path.home() / ".mcp_search_cache.db")
    if not Path(db).exists():
        return {"status": "success", "deleted": 0, "note": "кэш поиска пуст"}
    try:
        import sqlite3
        conn = sqlite3.connect(str(db))
        try:
            if older_than_days is not None:
                cutoff = time.time() - int(older_than_days) * 86400
                cur = conn.execute("DELETE FROM search_cache WHERE ts < ?", (cutoff,))
            else:
                cur = conn.execute("DELETE FROM search_cache")
            conn.commit()
            deleted = cur.rowcount
            conn.execute("VACUUM")
            return {"status": "success", "deleted": deleted,
                    "scope": f"старше {older_than_days} дн." if older_than_days else "весь кэш"}
        finally:
            conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def clear_web_cache(older_than_days: Optional[int] = None) -> Dict:
    """Очистка кэша веб-страниц (~/.mcp_web_cache.db, FTS5). older_than_days=None — весь кэш."""
    db = _get_web_cache_db_path()
    if not Path(db).exists():
        return {"status": "success", "deleted": 0, "note": "веб-кэш пуст"}
    try:
        import sqlite3
        conn = sqlite3.connect(str(db))
        try:
            if older_than_days is not None:
                cutoff = time.time() - int(older_than_days) * 86400
                cur = conn.execute("DELETE FROM web_cache WHERE ts < ?", (cutoff,))
            else:
                cur = conn.execute("DELETE FROM web_cache")
            conn.commit()
            deleted = cur.rowcount
            conn.execute("VACUUM")
            return {"status": "success", "deleted": deleted,
                    "scope": f"старше {older_than_days} дн." if older_than_days else "весь кэш"}
        finally:
            conn.close()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def register_tools(server: BaseMCPServer):
    server.register_tool("fetch_url", {
        "description": "Fetch and sanitize text from web page (robots/SSRF protected) with local caching",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
                "max_size_mb": {"type": "integer", "default": 10},
                "use_cache": {"type": "boolean", "default": True},
                "ttl_hours": {"type": "integer", "default": 24},
                "force_refresh": {"type": "boolean", "default": False}
            },
            "required": ["url"]
        }
    }, lambda **kw: fetch_url(kw["url"], kw.get("timeout", 30), kw.get("max_size_mb", 10), kw.get("use_cache", True), kw.get("force_refresh", False)))

    server.register_tool("fetch_url_enhanced", {
        "description": "Fetch webpage with tables, images, improved text extraction and local FTS caching",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "extract_tables": {"type": "boolean", "default": True},
                "extract_images": {"type": "boolean", "default": True},
                "timeout": {"type": "integer", "default": 30},
                "max_size_mb": {"type": "integer", "default": 10},
                "use_cache": {"type": "boolean", "default": True},
                "ttl_hours": {"type": "integer", "default": 24},
                "force_refresh": {"type": "boolean", "default": False}
            },
            "required": ["url"]
        }
    }, lambda **kw: fetch_url_enhanced(kw["url"], kw.get("extract_tables", True), kw.get("extract_images", True),
                                       kw.get("timeout", 30), kw.get("max_size_mb", 10),
                                       kw.get("use_cache", True), kw.get("ttl_hours", 24), kw.get("force_refresh", False)))

    server.register_tool("web_search", {
        "description": "Search the web using DuckDuckGo. Returns real URLs and snippets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 15},
                "timeout": {"type": "integer", "default": 30}
            },
            "required": ["query"]
        }
    }, lambda **kw: web_search(kw["query"], kw.get("max_results", 15), kw.get("timeout", 30)))

    server.register_tool("web_search_enhanced", {
        "description": "Search web using multiple sources with optional summarization. Sources: 'duckduckgo' "
                       "(no key), 'searxng' (local instance via MCP_SEARXNG_URL, no key, no captcha), "
                       "'brave' (needs BRAVE_SEARCH_API_KEY).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 15},
                "sources": {"type": "array", "items": {"type": "string", "enum": ["duckduckgo", "searxng", "brave"]}, "default": ["duckduckgo"]},
                "timeout": {"type": "integer", "default": 30},
                "summarize": {"type": "boolean", "default": False}
            },
            "required": ["query"]
        }
    }, lambda **kw: web_search_enhanced(kw["query"], kw.get("max_results", 15), kw.get("sources", ["duckduckgo"]),
                                        kw.get("timeout", 30), kw.get("summarize", False)))

    server.register_tool("cached_search", {
        "description": "Search with SQLite caching (results cached for max_age_hours)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_age_hours": {"type": "integer", "default": 24},
                "use_cache": {"type": "boolean", "default": True},
                "max_results": {"type": "integer", "default": 15},
                "sources": {"type": "array", "items": {"type": "string"}, "default": ["duckduckgo"]},
                "timeout": {"type": "integer", "default": 30}
            },
            "required": ["query"]
        }
    }, lambda **kw: cached_search(kw["query"], kw.get("max_age_hours", 24), kw.get("use_cache", True),
                                  max_results=kw.get("max_results", 15),
                                  sources=kw.get("sources", ["duckduckgo"]),
                                  timeout=kw.get("timeout", 30)))

    server.register_tool("web_search_dated", {
        "description": "Search with date filter (DuckDuckGo df parameter: 1d, 7d, 30d, 1y)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "days_back": {"type": "integer", "default": 7, "enum": [1, 7, 30, 365]},
                "max_results": {"type": "integer", "default": 10},
                "sources": {"type": "array", "items": {"type": "string"}, "default": ["duckduckgo"]},
                "timeout": {"type": "integer", "default": 30}
            },
            "required": ["query"]
        }
    }, lambda **kw: web_search_dated(kw["query"], kw.get("days_back", 7), kw.get("max_results", 10),
                                     kw.get("sources", ["duckduckgo"]), kw.get("timeout", 30)))

    server.register_tool("read_rss", {
        "description": "Parse RSS/Atom feed into structured JSON",
        "inputSchema": {"type": "object", "properties": {"feed_url": {"type": "string"}}, "required": ["feed_url"]}
    }, lambda **kw: read_rss(kw["feed_url"]))

    server.register_tool("download_file", {
        "description": "Safely download file from URL to allowed local path",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "destination": {"type": "string"}}, "required": ["url", "destination"]}
    }, lambda **kw: download_file(kw["url"], kw["destination"]))

    server.register_tool("crawl_deep_links", {
        "description": "Crawl links from start URL up to max depth/pages (same-domain lock)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_url": {"type": "string"},
                "max_depth": {"type": "integer", "default": 3},
                "max_pages": {"type": "integer", "default": 50},
                "same_domain": {"type": "boolean", "default": True}
            },
            "required": ["start_url"]
        }
    }, lambda **kw: crawl_deep_links(kw["start_url"], kw.get("max_depth", 3), kw.get("max_pages", 50), kw.get("same_domain", True)))

    server.register_tool("fetch_dynamic_js", {
        "description": "Render JS-heavy pages via Playwright with optional Cloudflare bypass",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "wait_for_selector": {"type": "string"},
                "js_eval": {"type": "string"},
                "timeout": {"type": "integer", "default": 30000},
                "bypass_cloudflare": {"type": "boolean", "default": False}
            },
            "required": ["url"]
        }
    }, lambda **kw: fetch_dynamic_js(kw["url"], kw.get("wait_for_selector"), kw.get("js_eval"),
                                     kw.get("timeout", 30000), kw.get("bypass_cloudflare", False)))

    server.register_tool("export_scraped_data", {
        "description": "Export structured web data to JSON or CSV",
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
                "output_path": {"type": "string"},
                "format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
                "delimiter": {"type": "string", "default": ","}
            },
            "required": ["data", "output_path"]
        }
    }, lambda **kw: export_scraped_data(kw["data"], kw["output_path"], kw.get("format", "json"), kw.get("delimiter", ",")))

    # Обновлённая регистрация capture_screenshot с новыми параметрами
    server.register_tool("capture_screenshot", {
        "description": "Делает скриншот веб-страницы через Playwright (поддерживает JS, повторные попытки, выбор стратегии ожидания)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output_path": {"type": "string"},
                "full_page": {"type": "boolean", "default": True},
                "wait_ms": {"type": "integer", "default": 2000},
                "width": {"type": "integer", "default": 1920},
                "height": {"type": "integer", "default": 1080},
                "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"], "default": "load"},
                "timeout_ms": {"type": "integer", "default": 60000},
                "retries": {"type": "integer", "default": 2}
            },
            "required": ["url", "output_path"]
        }
    }, lambda **kw: capture_screenshot(
        kw["url"], kw["output_path"], kw.get("full_page", True),
        kw.get("wait_ms", 2000), kw.get("width", 1920), kw.get("height", 1080),
        kw.get("wait_until", "load"), kw.get("timeout_ms", 60000),
        kw.get("retries", 2)
    ))

    server.register_tool("webpage_to_pdf", {
        "description": "Save webpage as PDF using Playwright (supports retries)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output_path": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 60000},
                "retries": {"type": "integer", "default": 2}
            },
            "required": ["url", "output_path"]
        }
    }, lambda **kw: webpage_to_pdf(kw["url"], kw["output_path"],
                                   kw.get("timeout_ms", 60000),
                                   kw.get("retries", 2)))

    server.register_tool("batch_fetch_with_retry", {
        "description": "Try multiple URLs in parallel, return first successful content",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}},
                "max_concurrent": {"type": "integer", "default": 2},
                "timeout": {"type": "integer", "default": 60},
                "extract_tables": {"type": "boolean", "default": False},
                "extract_images": {"type": "boolean", "default": False}
            },
            "required": ["urls"]
        }
    }, lambda **kw: batch_fetch_with_retry(
        kw["urls"], kw.get("max_concurrent", 2), kw.get("timeout", 60),
        kw.get("extract_tables", False), kw.get("extract_images", False)
    ))

    server.register_tool("capture_screenshot_with_fallback", {
        "description": "Attempt screenshot with multiple strategies (load, networkidle, JS rendering)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output_path": {"type": "string"},
                "retries": {"type": "integer", "default": 3},
                "timeout_ms": {"type": "integer", "default": 60000}
            },
            "required": ["url", "output_path"]
        }
    }, lambda **kw: capture_screenshot_with_fallback(
        kw["url"], kw["output_path"], kw.get("retries", 3), kw.get("timeout_ms", 60000)
    ))

    server.register_tool("search_web_cache", {
        "description": "Search locally cached web pages using Full-Text Search (FTS5)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        }
    }, lambda **kw: search_web_cache(kw["query"], kw.get("limit", 10)))

    server.register_tool("clear_search_cache", {
        "description": "Clear the web search results cache (~/.mcp_search_cache.db). "
                       "older_than_days: keep recent, delete older; omit to clear all.",
        "inputSchema": {"type": "object", "properties": {
            "older_than_days": {"type": "integer", "description": "Delete entries older than N days (omit = all)"}
        }}
    }, lambda **kw: clear_search_cache(kw.get("older_than_days")))

    server.register_tool("clear_web_cache", {
        "description": "Clear the cached web pages store (~/.mcp_web_cache.db, FTS5). "
                       "older_than_days: keep recent, delete older; omit to clear all.",
        "inputSchema": {"type": "object", "properties": {
            "older_than_days": {"type": "integer", "description": "Delete entries older than N days (omit = all)"}
        }}
    }, lambda **kw: clear_web_cache(kw.get("older_than_days")))

if __name__ == "__main__":
    server = BaseMCPServer("web-reader", "4.3.1")
    register_tools(server)
    server.run()