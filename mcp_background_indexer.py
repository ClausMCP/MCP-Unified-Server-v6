#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Background Indexer v1.0 – исправленная версия
Фоновый поток для периодической индексации файлов и веб-кэша.
Запускается автоматически при импорте.
"""
import os
import time
import threading
import sqlite3          # FIXED: добавлен импорт
from pathlib import Path
from typing import List

from mcp_shared import _log, is_online, AUTO_INDEX_FOLDERS, OFFLINE_MODE
from mcp_fs_advanced import index_all_files_content

# Конфигурация
INDEX_INTERVAL_HOURS = int(os.environ.get("MCP_INDEX_INTERVAL_HOURS", "6"))
WEB_CACHE_INDEX_INTERVAL_HOURS = int(os.environ.get("MCP_WEB_CACHE_INDEX_HOURS", "24"))

class BackgroundIndexer:
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None
        self._online_was = is_online()
        
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _log("[BackgroundIndexer] Started")
        
    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _log("[BackgroundIndexer] Stopped")
        
    def _run(self):
        last_index_time = 0
        last_web_index_time = 0
        while not self._stop_event.is_set():
            now = time.time()
            # Периодическая индексация папок
            if AUTO_INDEX_FOLDERS and (now - last_index_time) > INDEX_INTERVAL_HOURS * 3600:
                self._index_folders()
                last_index_time = now
            # Индексация веб-кэша при появлении интернета
            online_now = is_online()
            if online_now and not self._online_was:
                _log("[BackgroundIndexer] Internet became online, indexing web cache...")
                self._index_web_cache()
                self._online_was = True
            elif not online_now:
                self._online_was = False
            # Периодическая индексация веб-кэша (раз в сутки)
            if (now - last_web_index_time) > WEB_CACHE_INDEX_INTERVAL_HOURS * 3600:
                if is_online():
                    self._index_web_cache()
                    last_web_index_time = now
            time.sleep(60)
            
    def _index_folders(self):
        folders = [f.strip() for f in AUTO_INDEX_FOLDERS.split(';') if f.strip()]
        for folder in folders:
            try:
                _log(f"[BackgroundIndexer] Indexing folder: {folder}")
                result = index_all_files_content(folder, force_reindex=False, max_files=10000)
                _log(f"[BackgroundIndexer] Indexed {result.get('indexed',0)} files in {folder}")
            except Exception as e:
                _log(f"[BackgroundIndexer] Error indexing {folder}: {e}")
                
    def _index_web_cache(self):
        # FIXED: проверяем существование WEB_CACHE_DB и импортируем только при необходимости
        try:
            from mcp_web_reader import WEB_CACHE_DB
        except ImportError:
            _log("[BackgroundIndexer] mcp_web_reader not available, skipping web cache indexing")
            return
        if not WEB_CACHE_DB or not Path(WEB_CACHE_DB).exists():
            _log("[BackgroundIndexer] WEB_CACHE_DB not found, skipping")
            return
            
        try:
            conn = sqlite3.connect(str(WEB_CACHE_DB))
            cur = conn.execute("SELECT url, content, title FROM web_cache WHERE content IS NOT NULL")
            rows = cur.fetchall()
            # Добавляем в RAG (опционально)
            try:
                from mcp_rag import add_document
                for url, content, title in rows:
                    add_document(content, metadata={"url": url, "title": title})
                _log(f"[BackgroundIndexer] Added {len(rows)} web pages to RAG")
            except ImportError:
                _log("[BackgroundIndexer] RAG not available, skipping web cache indexing")
            conn.close()
        except Exception as e:
            _log(f"[BackgroundIndexer] Error indexing web cache: {e}")

# Глобальный экземпляр, запускающийся при импорте модуля
_indexer = BackgroundIndexer()
_indexer.start()