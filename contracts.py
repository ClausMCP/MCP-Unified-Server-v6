"""
MCP Contracts v1.1 — единственный разделяемый интерфейс между
когнитивным слоем (Фаза 2) и файловым/аналитическим слоем (Фаза 3).

Исправления относительно исходной версии:
- `validate_payload` различает `None` и `""` / `0` (использует `is None`,
  а не `not payload.get(field)`).
- Добавлена явная проверка совместимости major-версии с понятным сообщением.
- Добавлен `CONTRACT_COMPAT_MAJOR` (для совместимости при росте до 2.x).
- Добавлен `is_compatible_version` — переиспользуемая проверка semver.
- SUMMARY_MAX_CHARS аннотирован `Final[int]`.
"""
from typing import Any, Dict, Final, List, Optional, TypedDict

CONTRACT_VERSION: Final[str] = "1.0"
CONTRACT_COMPAT_MAJOR: Final[int] = 1

# ─── Топики event_bus ────────────────────────────────────────────────────────
# Файловый слой → всем
TOPIC_FILE_ANALYZED: Final[str] = "file.analyzed"        # анализ файла завершён
TOPIC_FILE_FAILED: Final[str] = "file.analyze_failed"    # анализ не удался
TOPIC_FILE_CHANGED: Final[str] = "file.changed"         # watcher: файл изменился

# Когнитивный слой → файловому
TOPIC_TASK_ANALYZE: Final[str] = "task.file.analyze"    # запрос на анализ файла
TOPIC_TASK_BATCH: Final[str] = "task.file.batch"        # массовый анализ

ALL_TOPICS: Final[List[str]] = [
    TOPIC_FILE_ANALYZED,
    TOPIC_FILE_FAILED,
    TOPIC_FILE_CHANGED,
    TOPIC_TASK_ANALYZE,
    TOPIC_TASK_BATCH,
]

# ─── Типы записей в памяти ───────────────────────────────────────────────────
MEMORY_TYPE_FILE_ANALYSIS: Final[str] = "file_analysis"   # полный результат analyze_file
MEMORY_TYPE_FILE_SUMMARY: Final[str] = "file_summary"     # краткая выжимка для RAG

SUMMARY_MAX_CHARS: Final[int] = 500

# ─── Схемы payload ───────────────────────────────────────────────────────────
class FileAnalyzeRequest(TypedDict, total=False):
    """task.file.analyze — когнитивный слой просит проанализировать файл."""
    request_id: str            # uuid, для сопоставления ответа
    path: str                  # абсолютный путь (будет провалидирован файловым слоем)
    dialog: str                # id диалога (dialog_ctx)
    depth: str                 # "quick" | "full" — глубина анализа
    reason: str                # зачем (для логов и приоритизации)
    contract_version: str


class FileAnalyzedPayload(TypedDict, total=False):
    """file.analyzed — файловый слой сообщает о готовом анализе."""
    request_id: Optional[str]  # None, если анализ инициирован watcher'ом
    path: str
    content_hash: str          # sha256 содержимого — ключ дедупликации
    memory_id: str             # id записи с ПОЛНЫМ результатом в памяти
    summary: str               # <= SUMMARY_MAX_CHARS
    file_type: str             # "excel" | "csv" | "pdf" | "docx" | "json" | ...
    dialog: str
    contract_version: str


class FileFailedPayload(TypedDict, total=False):
    """file.analyze_failed."""
    request_id: Optional[str]
    path: str
    error: str
    dialog: str
    contract_version: str


class FileChangedPayload(TypedDict, total=False):
    """file.changed — от watcher'а. Когнитивный слой сам решает, нужен ли реанализ."""
    path: str
    change_type: str           # "created" | "modified" | "deleted" | "moved"
    contract_version: str


# ─── Конструкторы (гарантируют contract_version и обрезку summary) ───────────
def make_analyze_request(
    request_id: str,
    path: str,
    dialog: str,
    depth: str = "quick",
    reason: str = "",
) -> FileAnalyzeRequest:
    return FileAnalyzeRequest(
        request_id=request_id,
        path=path,
        dialog=dialog,
        depth=depth if depth in ("quick", "full") else "quick",
        reason=reason,
        contract_version=CONTRACT_VERSION,
    )


def make_analyzed_payload(
    path: str,
    content_hash: str,
    memory_id: str,
    summary: str,
    file_type: str,
    dialog: str,
    request_id: Optional[str] = None,
) -> FileAnalyzedPayload:
    return FileAnalyzedPayload(
        request_id=request_id,
        path=path,
        content_hash=content_hash,
        memory_id=memory_id,
        summary=(summary or "")[:SUMMARY_MAX_CHARS],
        file_type=file_type,
        dialog=dialog,
        contract_version=CONTRACT_VERSION,
    )


def make_failed_payload(
    path: str,
    error: str,
    dialog: str,
    request_id: Optional[str] = None,
) -> FileFailedPayload:
    return FileFailedPayload(
        request_id=request_id,
        path=path,
        error=str(error)[:1000],
        dialog=dialog,
        contract_version=CONTRACT_VERSION,
    )


# ─── Валидация входящих событий ──────────────────────────────────────────────
_REQUIRED: Dict[str, List[str]] = {
    TOPIC_TASK_ANALYZE: ["request_id", "path", "dialog"],
    TOPIC_FILE_ANALYZED: ["path", "content_hash", "memory_id", "summary"],
    TOPIC_FILE_FAILED: ["path", "error"],
    TOPIC_FILE_CHANGED: ["path", "change_type"],
}


def _is_missing(payload: Dict[str, Any], field: str) -> bool:
    """Поле считается отсутствующим, только если его нет или значение None.

    Исправление: в исходной версии `if not payload.get(field)` ловило и пустую
    строку, и 0 — что некорректно для семантики `Optional`."""
    return field not in payload or payload[field] is None


def is_compatible_version(version: str) -> bool:
    """Совместима ли указанная версия с текущим контрактом (major match)."""
    if not version or not isinstance(version, str):
        return False
    try:
        major = int(version.split(".", 1)[0])
    except (ValueError, IndexError):
        return False
    return major == CONTRACT_COMPAT_MAJOR


def validate_payload(topic: str, payload: Dict[str, Any]) -> List[str]:
    """Вернуть список ошибок (пустой = ок). Неизвестный топик — не ошибка."""
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a dict"]

    for field in _REQUIRED.get(topic, []):
        if _is_missing(payload, field):
            errors.append(f"missing required field '{field}' for topic '{topic}'")

    ver = payload.get("contract_version")
    if ver is not None and not is_compatible_version(ver):
        errors.append(
            f"incompatible contract_version {ver!r} "
            f"(expected major {CONTRACT_COMPAT_MAJOR}, current {CONTRACT_VERSION})"
        )
    return errors
