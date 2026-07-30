# Изменения и новые файлы (документация к набору)

Набор для офлайн MCP-ассистента. Файлы кладутся в папку проекта (`C:\Tools`)
поверх существующих — имена совпадают, unified-сервер сам подхватывает все
`mcp_*.py`. После замены запустите `setup.bat` → пункт **H** (авто-починка +
самопроверка). Полное руководство — в `README.md`.

Состояние: компиляция чистая, самопроверка **PASS=93, FAIL=0**, линтер промптов
без расхождений, все файлы в CRLF.

## Новые файлы

| Файл | Назначение | Ключевые инструменты |
|------|------------|----------------------|
| `mcp_guard.py` | Защита от повторов и галлюцинаций (офлайн) | `check_repetition`, `check_grounding` |
| `mcp_db_tools.py` | Работа с БД и сжатие всех баз | `sql_query`, `list_tables`, `db_info`, `optimize_all_databases` |
| `mcp_extract.py` | Универсальное извлечение текста (PDF/изобр./docx/xlsx/текст) | `extract_any_text`, `batch_extract_text` |
| `install_ocr.py` | Установка Tesseract и языковых пакетов | `--check`, `--langs`, `--all`, `--installer` |
| `doctor.py` | Авто-проверка/починка окружения и офлайн-готовности | `--fix`, `--install`, `--offline` |
| `README.md` | Руководство оператора | — |

## Изменённые файлы

| Файл | Что изменено |
|------|--------------|
| `mcp_pdf.py` | OCR сканов; фоновый `ocr_pdf` + `ocr_status`; `images_to_pdf`; автопоиск tesseract; лимит авто-OCR в `read_pdf` |
| `mcp_web_reader.py` | Источник SearXNG; ротация User-Agent + эмуляция браузера (без обхода капч); прокси `MCP_PROXY`; `clear_search_cache`/`clear_web_cache`; фикс `search_web_cache` |
| `mcp_smart_search.py` | Передаёт веб-движки (`searxng`/`brave`/`duckduckgo`) в `web_search_enhanced` |
| `context_manager_server.py` | Умный `recall_fact`: возвращает решение, а не вопрос; стем-поиск между диалогами |
| `mcp_office_editor.py` | `create_excel`; создание Excel/формул на новом файле; родительские папки для docx |
| `mcp_office_reader.py` | Фикс `excel_to_markdown` (первый лист, фоллбэк без tabulate) |
| `mcp_code_exec.py` | UTF-8 для подпроцесса (кириллица без искажений) |
| `mcp_fs_media.py` | Автопоиск tesseract для OCR изображений |
| `mcp_shared.py` | Общий `locate_tesseract` |
| `mcp_setup.py` | Добавлен `pypdfium2` |
| `selftest.py` | Расширен до 93 проверок (office, extract, pdf+OCR, guard, db, recall, web-маршрутизация) |
| `check_prompt_tools.py` | Шумовой список линтера |
| `setup.bat` | Пункт **I** (Setup OCR); пункт **H** теперь чинит (`doctor.py --fix`) + проверяет; CRLF |
| `Promt_Eng.txt`, `Promt_Rus.txt` | Описаны новые возможности, правила надёжности/OCR/офиса, новые инструменты |

## Что требует интернета (один раз или при использовании)

- **Один раз (настройка):** установка зависимостей; Tesseract + язык `rus`
  (пункт I); модель эмбеддингов `all-MiniLM-L6-v2` (первый запуск RAG).
- **При использовании:** веб-поиск (SearXNG/Brave/DuckDuckGo), загрузка
  страниц/файлов, RSS, email (`send_email`/`fetch_emails`), облачная синхронизация.
- **Всё остальное** (файлы, офис, PDF/OCR, код, память/граф/recall, БД, guard,
  бэкап, извлечение текста, поиск по своим данным) — офлайн.

Проверить, что собрано для офлайна: `python doctor.py --offline`.

## Граница (что НЕ реализовано осознанно)

Обход Cloudflare/капч, решение капч (2captcha/CapSolver), парсинг Google в обход
защиты — НЕ поддерживаются. `fetch_dynamic_js(bypass_cloudflare=True)` только
*ожидает* прохождения JS-челленджа и эмулирует обычный браузер; капчи не решает,
авто-переключения при блокировке нет. Для стабильного поиска используйте
SearXNG/Brave.
