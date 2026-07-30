#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Skills Loader v1.0 — слой "скилов-знаний" (Agent Skills, формат SKILL.md).

Идея: скилы — это НЕ код, а методички (context, not code). Каждая лежит в своей
папке в каталоге skills/ с файлом SKILL.md (YAML-фронтматтер: name, description +
тело-инструкция). Сервер их индексирует и отдаёт модели В НУЖНЫЙ МОМЕНТ.

Автоматизм без промта:
  Ваш системный промпт уже заставляет ассистента вызывать `smart_search` перед
  ответом. При регистрации этот модуль патчит smart_search так, что на КАЖДЫЙ
  локальный поиск он дополнительно подбирает релевантный скил и — если совпадение
  уверенное — подмешивает его тело прямо в результаты. Модель читает результат
  поиска и следует методичке сама. Никаких новых правил в промпт добавлять не надо.

Инструменты:
  skill_list()                     — список всех скилов
  skill_search(query, limit)       — ранжированный подбор (без тел, дёшево)
  skill_load(name)                 — полное тело скила (progressive disclosure)
  skill_reload()                   — принудительная переиндексация
  skill_stats()                    — статистика каталога

Каталог скилов (в порядке приоритета):
  1) переменная окружения  MCP_SKILLS_DIR
  2) <папка этого файла>/skills
  3) C:\\Tools\\skills   (в стиле остальных путей проекта)

Новые папки со скилами подхватываются автоматически (по mtime), перезапуск не нужен.

Env-настройки:
  MCP_SKILLS_DIR         путь к каталогу скилов
  MCP_SKILLS_AUTO        "true"/"false" — авто-подмешивание в smart_search (по умолч. true)
  MCP_SKILL_THRESHOLD    порог уверенной автоактивации 0..1 (по умолч. 0.35)
  MCP_SKILL_MAX_CHARS    макс. длина тела, подмешиваемого в поиск (по умолч. 8000)
"""

import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    # используем общий сервер/логгер/контекст проекта
    from mcp_shared import BaseMCPServer, _log, dialog_ctx
except Exception:  # автономный запуск/отладка вне проекта
    import contextvars
    dialog_ctx = contextvars.ContextVar("dialog_id", default="default")

    def _log(msg: str):
        print(f"[skills] {msg}", flush=True)

    class BaseMCPServer:  # минимальная заглушка
        def __init__(self, name, version):
            self.name, self.version = name, version
            self.tools, self._handlers = [], {}

        def register_tool(self, name, schema, handler):
            self.tools.append({"name": name,
                               "description": schema.get("description", ""),
                               "inputSchema": schema.get("inputSchema", {})})
            self._handlers[name] = handler

        def run(self):
            _log("stub server: no stdio loop")


# ──────────────────────────────────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────────────────────────────────
def _resolve_skills_dir() -> Path:
    env = os.environ.get("MCP_SKILLS_DIR", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent / "skills"
    if here.exists():
        return here
    tools = Path(r"C:\Tools\skills") if os.name == "nt" else Path.home() / ".mcp" / "skills"
    if tools.exists():
        return tools
    # если ничего нет — вернём путь рядом с файлом (его же и создадим при reload)
    return here


SKILLS_DIR = _resolve_skills_dir()
AUTO_INJECT = os.environ.get("MCP_SKILLS_AUTO", "true").lower() == "true"
AUTO_THRESHOLD = float(os.environ.get("MCP_SKILL_THRESHOLD", "0.35"))
MAX_BODY_CHARS = int(os.environ.get("MCP_SKILL_MAX_CHARS", "8000"))
MAX_SCAN_DEPTH = 6  # насколько глубоко ищем SKILL.md (репозитории иногда вкладывают)
# Семантика: "auto" — включить, если эмбеддер проекта загрузился; "on"/"off" — явно.
# Важно: для кросс-язычного (RU-запрос → EN-скил) нужна МУЛЬТИЯЗЫЧНАЯ модель
# в MCP_RAG_EMBEDDING_MODEL (например paraphrase-multilingual-MiniLM-L12-v2).
SEMANTIC_MODE = os.environ.get("MCP_SKILLS_SEMANTIC", "auto").lower()
SEMANTIC_WEIGHT = float(os.environ.get("MCP_SKILL_SEMANTIC_WEIGHT", "0.9"))

# Метаданные для авто-загрузчика mcp_fs_server (__mcp_plugin__).
# dependencies пуст: yaml/numpy/эмбеддер — опциональны и обёрнуты в try,
# поэтому модуль грузится всегда, даже на «голой» системе.
__mcp_plugin__ = {
    "name": "skills",
    "version": "1.0",
    "description": ("Слой скилов-знаний (Agent Skills / SKILL.md): индексация папки "
                    "skills/, поиск и загрузка методичек, авто-активация через smart_search"),
    "dependencies": [],
    "on_load": lambda: _log("[skills] v1.0 loaded. Drop skill folders into the skills/ dir."),
    "on_unload": lambda: _log("[skills] Unloaded."),
}

_STOPWORDS = {
    # en
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
    "are", "be", "this", "that", "it", "as", "at", "by", "from", "use", "using",
    "when", "how", "what", "your", "you", "can", "will", "not", "do", "does",
    # ru
    "и", "в", "во", "не", "на", "с", "со", "по", "как", "что", "это", "для",
    "или", "к", "ко", "о", "об", "из", "у", "а", "но", "же", "бы", "ли", "the",
    "когда", "чтобы", "если", "их", "его", "её", "мой", "мне", "меня",
}

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)

# фразы-триггеры извлекаем из фрагмента описания после этих маркеров
_TRIGGER_MARKERS = re.compile(
    r"(?:use\s+when|use\s+this\s+when|используйте?\s+когда|применять?\s+когда|when\s+to\s+use)\s*:?",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────
# Разбор SKILL.md
# ──────────────────────────────────────────────────────────────────────────
def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Возвращает (meta, body). Поддерживает --- YAML ---; без PyYAML."""
    meta: Dict[str, Any] = {}
    body = text
    if text.lstrip().startswith("---"):
        # найдём границы фронтматтера
        stripped = text.lstrip("\ufeff")
        first = stripped.find("---")
        rest = stripped[first + 3:]
        end = rest.find("\n---")
        if end != -1:
            fm = rest[:end]
            body = rest[end + 4:].lstrip("\n")
            # пробуем PyYAML, если есть
            try:
                import yaml  # type: ignore
                loaded = yaml.safe_load(fm) or {}
                if isinstance(loaded, dict):
                    meta = {str(k).lower(): v for k, v in loaded.items()}
                    return meta, body
            except Exception:
                pass
            # минимальный парсер key: value
            for line in fm.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                m = re.match(r"\s*([\w\-]+)\s*:\s*(.*)$", line)
                if not m:
                    continue
                key = m.group(1).lower()
                val = m.group(2).strip()
                if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                    val = val[1:-1]
                meta[key] = val
    return meta, body


def _tokenize(text: str) -> List[str]:
    return [w for w in (t.lower() for t in _WORD_RE.findall(text or ""))
            if len(w) >= 2 and w not in _STOPWORDS]


def _extract_triggers(description: str) -> List[str]:
    """Фразы после 'Use when:' — самый сильный сигнал релевантности."""
    if not description:
        return []
    m = _TRIGGER_MARKERS.search(description)
    tail = description[m.end():] if m else ""
    if not tail:
        return []
    parts = re.split(r"[;,\.\n]| — | – ", tail)
    out = []
    for p in parts:
        p = p.strip(" -–—\t")
        if len(p) >= 4:
            out.append(p.lower())
    return out[:12]


# ──────────────────────────────────────────────────────────────────────────
# Опциональный семантический слой (переиспользует эмбеддер RAG проекта)
# ──────────────────────────────────────────────────────────────────────────
_embedder_cache: Any = "unset"


def _get_embedder():
    """Возвращает эмбеддер проекта (SentenceTransformer) или None. Кэшируется.
    В работающей системе singleton RAG уже загружен, поэтому доступ дешёвый."""
    global _embedder_cache
    if _embedder_cache != "unset":
        return _embedder_cache
    if SEMANTIC_MODE == "off":
        _embedder_cache = None
        return None
    try:
        from mcp_rag_engine import _get_embedder as rag_embedder
        _embedder_cache = rag_embedder()
        _log("skills semantic layer: ON (reusing RAG embedder)")
    except Exception as e:
        _embedder_cache = None
        if SEMANTIC_MODE == "on":
            _log(f"skills semantic requested but embedder unavailable: {e}")
    return _embedder_cache


def _cos(a, b) -> float:
    try:
        import numpy as np
        a = np.asarray(a, dtype="float32")
        b = np.asarray(b, dtype="float32")
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
    except Exception:
        return 0.0


class Skill:
    __slots__ = ("name", "description", "body", "path", "category",
                 "keywords", "license", "version", "_bag", "_triggers", "vec")

    def __init__(self, name, description, body, path, category,
                 keywords, license_, version):
        self.name = name
        self.description = description
        self.body = body
        self.path = path
        self.category = category
        self.keywords = keywords
        self.license = license_
        self.version = version
        self._triggers = _extract_triggers(description)
        self._bag = self._build_bag()
        self.vec = None  # эмбеддинг сигнатуры (заполняется индексом, если семантика вкл.)

    def signature_text(self) -> str:
        """Компактный текст для эмбеддинга: имя + описание + ключевые слова."""
        return " ".join(filter(None, [
            self.name.replace("-", " ").replace("_", " "),
            self.description,
            " ".join(self.keywords),
        ]))

    def _build_bag(self) -> Dict[str, float]:
        bag: Dict[str, float] = {}

        def add(tokens, weight):
            for t in tokens:
                if bag.get(t, 0.0) < weight:
                    bag[t] = weight

        add(_tokenize(self.name.replace("-", " ").replace("_", " ")), 3.0)
        for phrase in self._triggers:
            add(_tokenize(phrase), 2.5)
        add(_tokenize(" ".join(self.keywords)), 2.0)
        add(_tokenize(self.description), 1.0)
        return bag

    def score(self, query: str) -> float:
        q_tokens = set(_tokenize(query))
        if not q_tokens:
            return 0.0
        raw = sum(self._bag.get(t, 0.0) for t in q_tokens)
        score = raw / (raw + 4.0)  # мягкое сжатие в 0..1
        # бонус за фразовое совпадение триггера
        ql = " " + query.lower() + " "
        for phrase in self._triggers:
            core = phrase.strip()
            if len(core) >= 6 and core in ql:
                score += 0.15
                break
        return min(score, 0.99)

    def brief(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": (self.description[:220] +
                            ("…" if len(self.description) > 220 else "")),
            "category": self.category,
            "path": self.path,
        }


# ──────────────────────────────────────────────────────────────────────────
# Индекс (с авто-переиндексацией по mtime)
# ──────────────────────────────────────────────────────────────────────────
class _SkillIndex:
    def __init__(self):
        self._lock = threading.Lock()
        self._skills: Dict[str, Skill] = {}
        self._signature: Optional[tuple] = None
        self._last_error: Optional[str] = None

    def _scan_files(self) -> List[Path]:
        if not SKILLS_DIR.exists():
            return []
        found = []
        base_depth = len(SKILLS_DIR.resolve().parts)
        for root, dirs, files in os.walk(SKILLS_DIR):
            depth = len(Path(root).resolve().parts) - base_depth
            if depth > MAX_SCAN_DEPTH:
                dirs[:] = []
                continue
            # пропускаем служебные каталоги
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("node_modules", "__pycache__", "scripts",
                                     "templates", "spec", ".github")]
            for fn in files:
                if fn.lower() == "skill.md":
                    found.append(Path(root) / fn)
        return found

    def _signature_of(self, files: List[Path]) -> tuple:
        sig = []
        for f in files:
            try:
                st = f.stat()
                sig.append((str(f), int(st.st_mtime), st.st_size))
            except OSError:
                continue
        return tuple(sorted(sig))

    def ensure_fresh(self, force: bool = False):
        files = self._scan_files()
        sig = self._signature_of(files)
        with self._lock:
            if not force and sig == self._signature and self._skills:
                return
            skills: Dict[str, Skill] = {}
            errors = 0
            for f in files:
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    errors += 1
                    continue
                meta, body = _parse_frontmatter(text)
                folder = f.parent.name
                name = str(meta.get("name") or folder).strip()
                if not name:
                    continue
                desc = str(meta.get("description") or "").strip()
                kw = meta.get("keywords") or meta.get("tags") or []
                if isinstance(kw, str):
                    kw = [k.strip() for k in re.split(r"[,\s]+", kw) if k.strip()]
                elif not isinstance(kw, list):
                    kw = []
                try:
                    rel = f.parent.relative_to(SKILLS_DIR)
                    category = str(rel.parent) if str(rel.parent) != "." else "root"
                except Exception:
                    category = "root"
                skill = Skill(
                    name=name,
                    description=desc,
                    body=body.strip(),
                    path=str(f),
                    category=category,
                    keywords=[str(k) for k in kw],
                    license_=str(meta.get("license") or ""),
                    version=str(meta.get("version") or ""),
                )
                # при коллизии имён берём более глубокий/последний, но не молча
                skills[name.lower()] = skill
            self._skills = skills
            self._signature = sig
            self._last_error = f"{errors} file(s) unreadable" if errors else None
            _log(f"skills indexed: {len(skills)} from {SKILLS_DIR}"
                 + (f" ({self._last_error})" if self._last_error else ""))
            self._embed_all(skills)

    def _embed_all(self, skills: Dict[str, Skill]):
        """Пакетно считает эмбеддинги сигнатур скилов (если семантика доступна)."""
        if not skills:
            return
        embedder = _get_embedder()
        if embedder is None:
            return
        try:
            names = list(skills.keys())
            texts = [skills[n].signature_text() for n in names]
            vecs = embedder.encode(texts)
            for n, v in zip(names, vecs):
                try:
                    skills[n].vec = v.tolist() if hasattr(v, "tolist") else list(v)
                except Exception:
                    skills[n].vec = None
        except Exception as e:
            _log(f"skills embedding skipped: {e}")

    def all(self) -> List[Skill]:
        self.ensure_fresh()
        return list(self._skills.values())

    def get(self, name: str) -> Optional[Skill]:
        self.ensure_fresh()
        if not name:
            return None
        key = name.lower().strip()
        if key in self._skills:
            return self._skills[key]
        # частичное совпадение
        cands = [s for k, s in self._skills.items() if key in k]
        return cands[0] if len(cands) == 1 else None

    def search(self, query: str, limit: int = 5) -> List[Tuple[Skill, float]]:
        self.ensure_fresh()
        skills = list(self._skills.values())

        # семантика (если есть эмбеддер и посчитаны векторы скилов)
        q_vec = None
        if any(s.vec is not None for s in skills):
            embedder = _get_embedder()
            if embedder is not None:
                try:
                    qv = embedder.encode(query)
                    q_vec = qv.tolist() if hasattr(qv, "tolist") else list(qv)
                except Exception:
                    q_vec = None

        scored = []
        for s in skills:
            lex = s.score(query)
            sem = 0.0
            if q_vec is not None and s.vec is not None:
                sem = max(0.0, _cos(q_vec, s.vec)) * SEMANTIC_WEIGHT
            final = max(lex, sem)
            if final > 0.05:
                scored.append((s, final))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:max(1, limit)]


_INDEX = _SkillIndex()


# ──────────────────────────────────────────────────────────────────────────
# Логика инструментов
# ──────────────────────────────────────────────────────────────────────────
def _remember(op: str, info: Dict[str, Any]):
    """Мягкая запись в общую память проекта (не критично при отсутствии)."""
    try:
        from mcp_shared import conversation_memory
        conversation_memory.add(
            op=op, paths=info, status="success",
            dialog=dialog_ctx.get(),
            context=f"skills:{op} {info.get('skill', '')}".strip(),
        )
    except Exception:
        pass


def skill_list() -> Dict[str, Any]:
    skills = sorted(_INDEX.all(), key=lambda s: (s.category, s.name.lower()))
    grouped: Dict[str, List[Dict]] = {}
    for s in skills:
        grouped.setdefault(s.category, []).append(s.brief())
    return {
        "status": "success",
        "skills_dir": str(SKILLS_DIR),
        "count": len(skills),
        "by_category": grouped,
    }


def skill_search(query: str, limit: int = 5) -> Dict[str, Any]:
    if not query or not query.strip():
        return {"status": "error", "message": "query is required"}
    hits = _INDEX.search(query, limit)
    return {
        "status": "success",
        "query": query,
        "count": len(hits),
        "results": [{**s.brief(), "score": round(sc, 3)} for s, sc in hits],
        "hint": "Вызови skill_load(name), чтобы получить полную методичку.",
    }


def skill_load(name: str) -> Dict[str, Any]:
    if not name or not name.strip():
        return {"status": "error", "message": "name is required"}
    skill = _INDEX.get(name)
    if not skill:
        near = _INDEX.search(name, 5)
        return {
            "status": "not_found",
            "message": f"Скил '{name}' не найден.",
            "did_you_mean": [s.name for s, _ in near],
        }
    _remember("skill_load", {"skill": skill.name})
    return {
        "status": "success",
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "license": skill.license,
        "version": skill.version,
        "path": skill.path,
        "instructions": skill.body,
    }


def skill_reload() -> Dict[str, Any]:
    # гарантируем существование каталога, чтобы пользователю было куда класть папки
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _INDEX.ensure_fresh(force=True)
    return {"status": "success", "skills_dir": str(SKILLS_DIR),
            "count": len(_INDEX.all())}


def skill_stats() -> Dict[str, Any]:
    skills = _INDEX.all()
    cats: Dict[str, int] = {}
    for s in skills:
        cats[s.category] = cats.get(s.category, 0) + 1
    return {
        "status": "success",
        "skills_dir": str(SKILLS_DIR),
        "exists": SKILLS_DIR.exists(),
        "count": len(skills),
        "categories": cats,
        "auto_inject": AUTO_INJECT,
        "threshold": AUTO_THRESHOLD,
        "semantic": _get_embedder() is not None,
        "semantic_mode": SEMANTIC_MODE,
    }


def skills_for_search(query: str, limit: int = 3) -> Optional[Dict[str, Any]]:
    """
    Возвращает блок для подмешивания в smart_search:
      - activated: тело самого релевантного скила (если score >= порога)
      - suggestions: короткий список остальных кандидатов
    Возвращает None, если ничего похожего нет (чтобы не засорять результат).
    """
    hits = _INDEX.search(query, max(limit, 3))
    if not hits:
        return None
    top, top_score = hits[0]
    suggestions = [{"name": s.name, "score": round(sc, 3),
                    "description": s.description[:140]}
                   for s, sc in hits[1:limit + 1]]
    block: Dict[str, Any] = {"status": "success", "suggestions": suggestions}
    if top_score >= AUTO_THRESHOLD:
        body = top.body
        truncated = False
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS]
            truncated = True
        block["activated"] = {
            "name": top.name,
            "score": round(top_score, 3),
            "description": top.description,
            "instructions": body,
            "truncated": truncated,
            "note": ("Активирован релевантный скил. Следуй этой методичке при "
                     "выполнении задачи." + (f" Полный текст: skill_load('{top.name}')."
                                             if truncated else "")),
        }
        _remember("skill_auto", {"skill": top.name, "score": round(top_score, 3)})
    else:
        block["activated"] = None
        block["note"] = ("Точного скила не найдено; при желании подгрузи один из "
                         "suggestions через skill_load(name).")
    return block


# ──────────────────────────────────────────────────────────────────────────
# Автоинтеграция в smart_search (без изменений в промте)
# ──────────────────────────────────────────────────────────────────────────
_smart_patched = False


def _patch_smart_search() -> bool:
    """Оборачивает mcp_smart_search._do_search, добавляя источник 'skills'.
    Скилы подбираются на КАЖДЫЙ локальный поиск (если MCP_SKILLS_AUTO=true),
    либо только когда 'skills' явно передан в sources."""
    global _smart_patched
    if _smart_patched:
        return True
    try:
        import mcp_smart_search as sss
    except Exception as e:
        _log(f"smart_search patch skipped: {e}")
        return False

    if getattr(sss, "_skills_patched", False):
        _smart_patched = True
        return True

    orig_do_search = sss._do_search

    def _do_search_with_skills(query, sources, limit, mempalace_project, d_id):
        results, errors = orig_do_search(query, sources, limit,
                                         mempalace_project, d_id)
        want = AUTO_INJECT or ("skills" in [str(s).lower() for s in (sources or [])])
        if want:
            try:
                block = skills_for_search(query, limit)
                if block:
                    results["skills"] = block
            except Exception as e:
                (errors if isinstance(errors, list) else []).append(f"skills: {e}")
        return results, errors

    sss._do_search = _do_search_with_skills
    sss._skills_patched = True
    _smart_patched = True
    _log("smart_search patched: skills auto-activation ON"
         if AUTO_INJECT else "smart_search patched: skills available as source")
    return True


# ──────────────────────────────────────────────────────────────────────────
# Регистрация
# ──────────────────────────────────────────────────────────────────────────
def register_tools(server: "BaseMCPServer"):
    server.register_tool("skill_list", {
        "description": "List all available knowledge skills (Agent Skills / SKILL.md), grouped by category.",
        "inputSchema": {"type": "object", "properties": {}},
    }, lambda **kw: skill_list())

    server.register_tool("skill_search", {
        "description": "Find the most relevant knowledge skills for a task. Returns names + scores (no bodies). Example: skill_search query=\"secure code review\"",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 5},
        }, "required": ["query"]},
    }, lambda **kw: skill_search(kw.get("query", ""), kw.get("limit", 5)))

    server.register_tool("skill_load", {
        "description": "Load the full instructions (methodology) of a knowledge skill by name. Use after skill_search. Example: skill_load name=positioning",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string"},
        }, "required": ["name"]},
    }, lambda **kw: skill_load(kw.get("name", "")))

    server.register_tool("skill_reload", {
        "description": "Re-scan the skills directory (pick up newly added skill folders). Also creates the directory if missing.",
        "inputSchema": {"type": "object", "properties": {}},
    }, lambda **kw: skill_reload())

    server.register_tool("skill_stats", {
        "description": "Show skills directory path, counts per category, and auto-activation settings.",
        "inputSchema": {"type": "object", "properties": {}},
    }, lambda **kw: skill_stats())

    # первичная индексация + патч smart_search
    try:
        _INDEX.ensure_fresh(force=True)
    except Exception as e:
        _log(f"initial index error: {e}")
    _patch_smart_search()


# алиас на случай, если основной загрузчик ищет register_skills_tool(server)
def register_skills_tool(server: "BaseMCPServer"):
    register_tools(server)


if __name__ == "__main__":
    server = BaseMCPServer("skills", "1.0")
    register_tools(server)
    # автономный самотест, если запущено напрямую
    import json
    print(json.dumps(skill_stats(), ensure_ascii=False, indent=2))
    print(json.dumps(skill_list(), ensure_ascii=False, indent=2))
    if hasattr(server, "run"):
        try:
            server.run()
        except Exception as e:
            _log(f"run() unavailable in standalone mode: {e}")