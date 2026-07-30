#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cognitive Bus – простая in-memory шина событий для когнитивных модулей.
Не требует SQLite, работает через прямые вызовы колбэков в том же процессе.
"""
import threading
from typing import Dict, Any, Callable, List
from mcp_shared import _log

_subscribers: Dict[str, List[Callable]] = {}
_lock = threading.Lock()

# Типы когнитивных событий (для документации)
COGNITIVE_EVENTS = {
    "fact_added": "Новый факт добавлен в граф",
    "hypothesis_created": "Создана новая гипотеза",
    "hypothesis_updated": "Статус гипотезы изменён",
    "hypothesis_verified": "Гипотеза подтверждена",
    "hypothesis_rejected": "Гипотеза опровергнута",
    "hypothesis_promoted": "Гипотеза продвинута до факта",
    "rule_added": "Добавлено новое правило",
    "prediction_made": "Сделано предсказание",
    "goal_created": "Создана новая цель",
    "goal_updated": "Статус цели изменён",
    "goal_completed": "Цель достигнута",
    "plan_created": "Создан план",
    "plan_step_completed": "Завершён шаг плана",
    "plan_failed": "План провален",
    "task_submitted": "Задача отправлена",
    "task_completed": "Задача выполнена",
    "reflection_triggered": "Запущен цикл рефлексии",
}

def publish(event_type: str, payload: Dict, source: str = "unknown"):
    """
    Опубликовать событие. Вызывает все зарегистрированные колбэки для event_type.
    """
    if event_type not in COGNITIVE_EVENTS:
        raise ValueError(f"Unknown cognitive event: {event_type}")
    with _lock:
        callbacks = _subscribers.get(event_type, []).copy()
    if not callbacks:
        return
    full_payload = {
        "event_type": event_type,
        "source": source,
        "data": payload
    }
    _log(f"[CognitiveBus] Published {event_type} from {source}")
    for cb in callbacks:
        try:
            cb(full_payload)
        except Exception as e:
            _log(f"[CognitiveBus] Error in callback for {event_type}: {e}")

def subscribe(event_type: str, callback: Callable):
    """Подписаться на события определённого типа."""
    with _lock:
        if event_type not in _subscribers:
            _subscribers[event_type] = []
        _subscribers[event_type].append(callback)
    _log(f"[CognitiveBus] Subscribed to {event_type}")

def unsubscribe(event_type: str, callback: Callable):
    """Отписаться от события."""
    with _lock:
        if event_type in _subscribers:
            try:
                _subscribers[event_type].remove(callback)
            except ValueError:
                pass

def subscribe_all(callback: Callable):
    """Подписаться на все когнитивные события (удобно для логирования)."""
    for event_type in COGNITIVE_EVENTS:
        subscribe(event_type, callback)

def get_event_types() -> Dict:
    """Вернуть словарь известных типов событий с описаниями."""
    return COGNITIVE_EVENTS.copy()