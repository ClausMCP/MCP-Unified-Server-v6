#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Calendar & Tasks v3.1
Secure .ics parsing, Outlook integration (Windows), and JSON task storage.
Features event reminding, date filtering, and conversation memory logging.
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

DEFAULT_ICS = os.path.expanduser("~/.mcp_calendar.ics")
DEFAULT_TASKS = os.path.expanduser("~/.mcp_tasks.json")

# ─── Helpers ─────────────────────────────────────────────────────────────────
def _parse_dt(dt_val) -> Optional[datetime]:
    """Safely extract datetime from icalendar vDDDTypes objects."""
    if hasattr(dt_val, 'dt'):
        dt_val = dt_val.dt
    if isinstance(dt_val, datetime):
        return dt_val
    elif hasattr(dt_val, 'strftime'):  # date object
        return datetime.combine(dt_val, datetime.min.time())
    return None

# ─── Core Tools ──────────────────────────────────────────────────────────────
def parse_ics(ics_path: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict:
    """Parse .ics file and extract events within optional date range."""
    p = Path(normalize_path(ics_path))
    _ensure_allowed(p, "parse_ics")
    if not p.is_file():
        return {"error": f"ICS file not found: {ics_path}"}

    try:
        from icalendar import Calendar
    except ImportError:
        return {"error": "icalendar not installed. Install with: pip install icalendar"}

    try:
        cal = Calendar.from_ical(p.read_bytes())
        events = []
        
        s_limit = datetime.fromisoformat(start_date) if start_date else None
        e_limit = datetime.fromisoformat(end_date) if end_date else None

        for component in cal.walk():
            if component.name != "VEVENT":
                continue
                
            start = _parse_dt(component.get("DTSTART"))
            if not start:
                continue
                
            # Apply filters
            if s_limit and start < s_limit:
                continue
            if e_limit and start > e_limit:
                continue

            end = _parse_dt(component.get("DTEND"))
            events.append({
                "uid": str(component.get("UID", "")),
                "summary": str(component.get("SUMMARY", "Untitled")),
                "start": start.isoformat(),
                "end": end.isoformat() if end else start.isoformat(),
                "location": str(component.get("LOCATION", "")),
                "description": str(component.get("DESCRIPTION", "")),
                "status": str(component.get("STATUS", "CONFIRMED"))
            })

        conversation_memory.add(
            op="parse_ics", paths={"file": str(p)}, status="success", dialog=dialog_ctx.get(),
            context=f"Parsed {len(events)} events from {p.name}"
        )
        return {"path": str(p), "total_events": len(events), "events": events}
    except Exception as e:
        return {"error": str(e)}

def add_calendar_event(summary: str, start: str, end: str, description: str = "", location: str = "", ics_path: str = DEFAULT_ICS) -> Dict:
    """Add a new event to an .ics calendar file."""
    p = Path(normalize_path(ics_path))
    _ensure_allowed(p.parent, "add_calendar_event")
    _ensure_allowed(p, "add_calendar_event")

    try:
        from icalendar import Calendar, Event
        import uuid
    except ImportError:
        return {"error": "icalendar not installed. Install with: pip install icalendar"}

    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return {"error": "Invalid date format. Use ISO 8601 (e.g., 2026-05-07T10:00:00)"}

    try:
        cal = Calendar.from_ical(p.read_bytes()) if p.exists() else Calendar()
        if not p.exists():
            cal.add('prodid', '-//MCP//Calendar//EN')
            cal.add('version', '2.0')

        event = Event()
        event.add('summary', summary)
        event.add('dtstart', start_dt)
        event.add('dtend', end_dt)
        event.add('dtstamp', datetime.now())
        event['uid'] = str(uuid.uuid4())
        event.add('priority', 5)
        if description: event.add('description', description)
        if location: event.add('location', location)

        cal.add_component(event)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(cal.to_ical())

        conversation_memory.add(
            op="add_calendar_event", paths={"file": str(p)}, status="success", dialog=dialog_ctx.get(),
            context=f"Added event '{summary}' ({start_dt.strftime('%Y-%m-%d %H:%M')})"
        )
        return {"status": "success", "uid": str(event['uid']), "path": str(p)}
    except Exception as e:
        return {"error": str(e)}

def get_tasks(source: str = "json", path: Optional[str] = None) -> Dict:
    """Fetch tasks from JSON storage or Outlook."""
    p = Path(normalize_path(path or DEFAULT_TASKS))
    _ensure_allowed(p, "get_tasks")

    if source == "json":
        if not p.exists():
            return {"status": "empty", "source": "json", "tasks": []}
        try:
            with open(p, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            return {"source": "json", "path": str(p), "tasks": tasks, "count": len(tasks)}
        except Exception as e:
            return {"error": f"Failed to read tasks.json: {e}"}
            
    elif source == "outlook":
        try:
            import win32com.client
            outlook = win32com.client.Dispatch("Outlook.Application")
            mapi = outlook.GetNamespace("MAPI")
            tasks_folder = mapi.GetDefaultFolder(13)  # olFolderTasks
            
            tasks = []
            for task in tasks_folder.Items:
                status_map = ["Not Started", "In Progress", "Completed", "Waiting", "Deferred"]
                tasks.append({
                    "subject": str(task.Subject),
                    "status": status_map[task.Status] if 0 <= task.Status <= 4 else "Unknown",
                    "due_date": task.DueDate.isoformat() if task.DueDate else None,
                    "priority": ["Low", "Normal", "High"][task.Importance] if 0 <= task.Importance <= 2 else "Normal",
                    "body": str(task.Body)[:200]
                })
                
            conversation_memory.add(
                op="get_tasks", paths={"source": "outlook"}, status="success", dialog=dialog_ctx.get(),
                context=f"Fetched {len(tasks)} tasks from Outlook"
            )
            return {"source": "outlook", "tasks": tasks, "count": len(tasks)}
        except ImportError:
            return {"error": "win32com not available. Requires Windows + pywin32."}
        except Exception as e:
            return {"error": f"Outlook task fetch failed: {e}"}
    else:
        return {"error": "Unsupported task source. Use 'json' or 'outlook'."}

def add_task(summary: str, due_date: str = "", priority: str = "Normal", path: Optional[str] = None) -> Dict:
    """Add a task to JSON task storage."""
    p = Path(normalize_path(path or DEFAULT_TASKS))
    _ensure_allowed(p.parent, "add_task")
    _ensure_allowed(p, "add_task")

    tasks = []
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                tasks = json.load(f)
        except json.JSONDecodeError:
            tasks = []

    new_task = {
        "id": len(tasks) + 1,
        "summary": summary,
        "due_date": due_date or "",
        "priority": priority,
        "status": "Not Started",
        "created": datetime.now().isoformat()
    }
    tasks.append(new_task)

    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)

    conversation_memory.add(
        op="add_task", paths={"file": str(p)}, status="success", dialog=dialog_ctx.get(),
        context=f"Added task '{summary}'"
    )
    return {"status": "success", "task": new_task}

def remind(hours: int = 24, ics_path: str = DEFAULT_ICS) -> Dict:
    """Check calendar for upcoming events within the specified hours."""
    p = Path(normalize_path(ics_path))
    _ensure_allowed(p, "remind")
    if not p.exists():
        return {"error": "Calendar file not found."}

    try:
        from icalendar import Calendar
    except ImportError:
        return {"error": "icalendar not installed."}

    try:
        cal = Calendar.from_ical(p.read_bytes())
        now = datetime.now()
        limit = now + timedelta(hours=hours)
        upcoming = []

        for component in cal.walk():
            if component.name == "VEVENT":
                start = _parse_dt(component.get("DTSTART"))
                if start and now <= start <= limit:
                    upcoming.append({
                        "summary": str(component.get("SUMMARY", "")),
                        "start": start.isoformat(),
                        "location": str(component.get("LOCATION", "")),
                        "time_until_hours": round((start - now).total_seconds() / 3600, 1)
                    })

        conversation_memory.add(
            op="remind", paths={"file": str(p)}, status="success", dialog=dialog_ctx.get(),
            context=f"Found {len(upcoming)} upcoming events in next {hours}h"
        )
        return {"checked_hours": hours, "upcoming_count": len(upcoming), "events": upcoming}
    except Exception as e:
        return {"error": str(e)}

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("calendar-tasks", "3.1")

server.register_tool("parse_ics", {
    "description": "Parse .ics calendar file and extract events",
    "inputSchema": {
        "type": "object",
        "properties": {
            "ics_path": {"type": "string"},
            "start_date": {"type": "string", "description": "ISO 8601 filter start"},
            "end_date": {"type": "string", "description": "ISO 8601 filter end"}
        },
        "required": ["ics_path"]
    }
}, lambda **kw: parse_ics(kw["ics_path"], kw.get("start_date"), kw.get("end_date")))

server.register_tool("add_calendar_event", {
    "description": "Create a new calendar event in .ics format",
    "inputSchema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "start": {"type": "string", "description": "ISO 8601 start time"},
            "end": {"type": "string", "description": "ISO 8601 end time"},
            "description": {"type": "string", "default": ""},
            "location": {"type": "string", "default": ""},
            "ics_path": {"type": "string", "default": DEFAULT_ICS}
        },
        "required": ["summary", "start", "end"]
    }
}, lambda **kw: add_calendar_event(
    kw["summary"], kw["start"], kw["end"], 
    kw.get("description", ""), kw.get("location", ""), kw.get("ics_path", DEFAULT_ICS)
))

server.register_tool("get_tasks", {
    "description": "Retrieve tasks from JSON storage or Outlook",
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": ["json", "outlook"], "default": "json"},
            "path": {"type": "string"}
        },
        "required": []
    }
}, lambda **kw: get_tasks(kw.get("source", "json"), kw.get("path")))

server.register_tool("add_task", {
    "description": "Add a new task to local JSON storage",
    "inputSchema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "due_date": {"type": "string", "default": ""},
            "priority": {"type": "string", "enum": ["Low", "Normal", "High"], "default": "Normal"},
            "path": {"type": "string"}
        },
        "required": ["summary"]
    }
}, lambda **kw: add_task(kw["summary"], kw.get("due_date", ""), kw.get("priority", "Normal"), kw.get("path")))

server.register_tool("remind", {
    "description": "Check calendar for upcoming events within N hours",
    "inputSchema": {
        "type": "object",
        "properties": {
            "hours": {"type": "integer", "default": 24},
            "ics_path": {"type": "string", "default": DEFAULT_ICS}
        },
        "required": []
    }
}, lambda **kw: remind(kw.get("hours", 24), kw.get("ics_path", DEFAULT_ICS)))

if __name__ == "__main__":
    server.run()