#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Email Client v3.2 (Plugin-Ready & Lazy-Connection)
Secure IMAP/POP3/SMTP operations with OS keyring integration, environment fallback,
and strict per-call connection lifecycle to minimize idle resource usage.
"""
import os
import re
import ssl
import imaplib
import poplib
import smtplib
import email
import email.utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email import policy
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from mcp_shared import (
    BaseMCPServer, _log, normalize_path, _ensure_allowed,
    conversation_memory, dialog_ctx
)

# ─── Plugin Metadata & Lifecycle Hooks ───────────────────────────────────────
__mcp_plugin__ = {
    "name": "email-client",
    "version": "3.2.0",
    "description": "Secure lazy-connection email client with keyring auth (IMAP/POP3/SMTP)",
    "dependencies": ["keyring"],
    "on_load": lambda: _log("[email-client] Loaded. Lazy connection mode active. No persistent sockets."),
    "on_unload": lambda: _log("[email-client] Unloaded. All transient connections safely closed.")
}

SERVICE_NAME = "MCP.Email"
MAX_FETCH_LIMIT = 50
MAX_BODY_PREVIEW = 4000
SAFE_HEADERS = ["From", "To", "Subject", "Date", "Message-ID", "In-Reply-To"]

# ─── Credential Management (Secure Chain) ────────────────────────────────────
def _resolve_credentials(protocol: str, username: str, explicit_pass: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Resolve password: explicit arg → keyring → env var."""
    if explicit_pass:
        return explicit_pass, "explicit"
    
    # Try OS keyring
    try:
        import keyring
        pw = keyring.get_password(SERVICE_NAME, f"{protocol.lower()}:{username}")
        if pw:
            return pw, "keyring"
    except Exception as e:
        _log(f"[email-client] Keyring unavailable: {e}")
    
    # Try environment variable
    safe_username = username.replace('@', '_AT_').replace('.', '_DOT_')
    env_var = f"MCP_EMAIL_{protocol.upper()}_{safe_username}_PASS"
    env_pw = os.environ.get(env_var)
    if env_pw:
        return env_pw, "env"
    
    return None, "missing"

def _save_to_keyring(protocol: str, username: str, password: str) -> bool:
    """Save credentials to OS keyring."""
    try:
        import keyring
        keyring.set_password(SERVICE_NAME, f"{protocol.lower()}:{username}", password)
        _log(f"[email-client] Credentials saved to keyring for {username}")
        return True
    except Exception as e:
        _log(f"[email-client] Keyring save failed: {e}")
        return False

def _clear_keyring(protocol: str, username: str) -> bool:
    """Remove credentials from OS keyring."""
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, f"{protocol.lower()}:{username}")
        _log(f"[email-client] Credentials cleared from keyring for {username}")
        return True
    except Exception:
        return False

def _validate_email(addr: str) -> Tuple[bool, str]:
    """Validate and normalize email address."""
    if not addr:
        return False, ""
    _, parsed = email.utils.parseaddr(addr)
    if not parsed:
        return False, addr
    # Basic RFC 5322 check
    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", parsed):
        return False, addr
    return True, parsed

# ─── Core Tools (Lazy Connection Pattern) ────────────────────────────────────

def fetch_emails(server: str, username: str, password: Optional[str] = None,
                 folder: str = "INBOX", limit: int = 10, 
                 protocol: str = "imap") -> Dict:
    """
    Fetch recent emails via IMAP or POP3.
    
    Args:
        server: Mail server hostname
        username: Email account username
        password: Password (optional, uses keyring/env if not provided)
        folder: IMAP folder name (ignored for POP3)
        limit: Max messages to fetch (capped at 50)
        protocol: 'imap' or 'pop3'
    """
    d_id = dialog_ctx.get()
    limit = min(abs(limit), MAX_FETCH_LIMIT)
    
    pwd, src = _resolve_credentials(protocol, username, password)
    if not pwd:
        return {
            "status": "error",
            "error": f"Credentials missing for {username}. Use 'password' arg, keyring, or env var MCP_EMAIL_{protocol.upper()}_..."
        }

    conn = None
    try:
        ctx = ssl.create_default_context()
        
        # ─── IMAP Path ──────────────────────────────────────────────────
        if protocol.lower() == "imap":
            conn = imaplib.IMAP4_SSL(server, context=ctx)
            conn.login(username, pwd)
            
            # Select folder
            status, _ = conn.select(folder)
            if status != "OK":
                return {"status": "error", "error": f"Failed to select folder '{folder}'"}
            
            # Search all messages
            status, data = conn.search(None, "ALL")
            if status != "OK" or not data[0]:
                return {"status": "empty", "protocol": "imap", "server": server, "messages": [], "count": 0}
            
            msg_nums = data[0].split()
            targets = msg_nums[-limit:]  # Most recent
            results = []
            
            for num in reversed(targets):
                try:
                    _, hdr = conn.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
                    if not hdr or not hdr[0]:
                        continue
                    msg = email.message_from_bytes(hdr[0][1])
                    results.append({
                        "uid": num.decode(),
                        "from": msg.get("From", "Unknown"),
                        "to": msg.get("To", ""),
                        "subject": msg.get("Subject", "(No Subject)"),
                        "date": msg.get("Date", ""),
                        "folder": folder
                    })
                except Exception as e:
                    _log(f"[email-client] Skip message {num}: {e}")
                    continue
            
            conversation_memory.add(
                op="fetch_emails",
                paths={"server": server, "folder": folder},
                status="success",
                dialog=d_id,
                context=f"Fetched {len(results)} IMAP messages from {server}/{folder}"
            )
            
            return {
                "status": "success",
                "protocol": "imap",
                "server": server,
                "folder": folder,
                "count": len(results),
                "messages": results,
                "auth_source": src
            }
        
        # ─── POP3 Path ──────────────────────────────────────────────────
        elif protocol.lower() == "pop3":
            conn = poplib.POP3_SSL(server, context=ctx)
            conn.user(username)
            conn.pass_(pwd)
            
            count, _ = conn.stat()
            if count == 0:
                return {"status": "empty", "protocol": "pop3", "server": server, "messages": [], "count": 0}
            
            # Fetch most recent messages (headers only)
            results = []
            start_idx = max(1, count - limit + 1)
            
            for i in range(start_idx, count + 1):
                try:
                    resp, lines, octets = conn.top(i, 1)  # Headers + 1 line of body
                    msg = email.message_from_bytes(b"\r\n".join(lines))
                    results.append({
                        "index": i,
                        "from": msg.get("From", "Unknown"),
                        "to": msg.get("To", ""),
                        "subject": msg.get("Subject", "(No Subject)"),
                        "date": msg.get("Date", ""),
                        "size": octets
                    })
                except Exception as e:
                    _log(f"[email-client] Skip POP3 message {i}: {e}")
                    continue
            
            conversation_memory.add(
                op="fetch_emails",
                paths={"server": server},
                status="success",
                dialog=d_id,
                context=f"Fetched {len(results)} POP3 messages from {server}"
            )
            
            return {
                "status": "success",
                "protocol": "pop3",
                "server": server,
                "count": len(results),
                "messages": results,
                "auth_source": src
            }
        
        else:
            return {
                "status": "error",
                "error": f"Unsupported protocol '{protocol}'. Use 'imap' or 'pop3'."
            }
    
    except imaplib.IMAP4.error as e:
        return {"status": "error", "protocol": protocol, "error": f"IMAP Error: {e}"}
    except poplib.error_proto as e:
        return {"status": "error", "protocol": protocol, "error": f"POP3 Error: {e}"}
    except ssl.SSLError as e:
        return {"status": "error", "protocol": protocol, "error": f"SSL/TLS Error: {e}"}
    except Exception as e:
        return {"status": "error", "protocol": protocol, "error": f"Connection failed: {e}"}
    
    finally:
        # Always close connection
        try:
            if conn:
                if protocol.lower() == "imap":
                    conn.logout()
                elif protocol.lower() == "pop3":
                    conn.quit()
        except Exception:
            pass


def search_emails(server: str, username: str, password: Optional[str] = None,
                  query: str = "ALL", since_days: int = 7, 
                  folder: str = "INBOX") -> Dict:
    """
    Search IMAP mailbox with criteria.
    
    Args:
        server: IMAP server
        username: Email account
        password: Password (optional)
        query: IMAP search query (default "ALL")
        since_days: Only messages from last N days
        folder: IMAP folder
    """
    d_id = dialog_ctx.get()
    pwd, src = _resolve_credentials("imap", username, password)
    if not pwd:
        return {"status": "error", "error": "IMAP credentials required for search."}
    
    conn = None
    try:
        import datetime as dt
        ctx = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(server, context=ctx)
        conn.login(username, pwd)
        conn.select(folder)
        
        # Build search criteria
        since_date = (dt.datetime.now() - dt.timedelta(days=since_days)).strftime("%d-%b-%Y")
        search_str = f"SINCE {since_date} {query}".strip()
        
        _log(f"[email-client] IMAP search: '{search_str}' in {folder}")
        status, data = conn.search(None, search_str)
        
        if status != "OK":
            return {"status": "error", "error": f"Search command failed: {status}"}
        
        msg_nums = data[0].split() if data[0] else []
        
        conversation_memory.add(
            op="search_emails",
            paths={"server": server, "folder": folder},
            status="success",
            dialog=d_id,
            context=f"Found {len(msg_nums)} messages matching '{query}' since {since_days}d"
        )
        
        return {
            "status": "success",
            "server": server,
            "folder": folder,
            "query": query,
            "since_days": since_days,
            "count": len(msg_nums),
            "uids": [m.decode() for m in msg_nums[:100]]  # Limit to 100 UIDs
        }
    
    except Exception as e:
        return {"status": "error", "error": str(e)}
    
    finally:
        try:
            if conn:
                conn.logout()
        except Exception:
            pass


def send_email(smtp_server: str, username: str, to: List[str], subject: str,
               body: str, password: Optional[str] = None, 
               attachments: List[str] = None,
               port: int = 465, use_tls: bool = True) -> Dict:
    """
    Send email via SMTP with optional attachments.
    
    Args:
        smtp_server: SMTP server hostname
        username: Sender email
        to: List of recipient emails
        subject: Email subject
        body: Plain text body
        password: Password (optional)
        attachments: List of file paths (optional)
        port: SMTP port (default 465 for SSL)
        use_tls: Use TLS/SSL
    """
    d_id = dialog_ctx.get()
    attachments = attachments or []
    
    pwd, src = _resolve_credentials("smtp", username, password)
    if not pwd:
        return {"status": "error", "error": "SMTP credentials missing."}
    
    # Validate recipients
    valid_to = []
    for addr in to:
        ok, parsed = _validate_email(addr)
        if ok:
            valid_to.append(parsed)
        else:
            return {"status": "error", "error": f"Invalid recipient address: {addr}"}
    
    if not valid_to:
        return {"status": "error", "error": "No valid recipients."}
    
    # Validate sender
    ok_from, parsed_from = _validate_email(username)
    if not ok_from:
        return {"status": "error", "error": f"Invalid sender address: {username}"}
    
    # Build message
    msg = MIMEMultipart()
    msg["From"] = parsed_from
    msg["To"] = ", ".join(valid_to)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=smtp_server)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    
    # Attachments (validate each)
    for att_path in attachments:
        p = Path(normalize_path(att_path))
        try:
            _ensure_allowed(p, "send_email")
        except PermissionError as e:
            return {"status": "error", "error": f"Attachment blocked: {e}"}
        
        if not p.is_file():
            return {"status": "error", "error": f"Attachment not found: {att_path}"}
        
        try:
            with open(p, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{p.name}"'
            )
            msg.attach(part)
        except Exception as e:
            return {"status": "error", "error": f"Failed to read attachment {p.name}: {e}"}
    
    # Send
    conn = None
    try:
        ctx = ssl.create_default_context()
        
        if use_tls and port == 465:
            # SMTPS (direct SSL)
            conn = smtplib.SMTP_SSL(smtp_server, port, context=ctx, timeout=30)
        elif use_tls and port == 587:
            # STARTTLS
            conn = smtplib.SMTP(smtp_server, port, timeout=30)
            conn.starttls(context=ctx)
        else:
            # Plain (not recommended)
            conn = smtplib.SMTP(smtp_server, port, timeout=30)
        
        conn.login(username, pwd)
        conn.sendmail(parsed_from, valid_to, msg.as_string())
        
        conversation_memory.add(
            op="send_email",
            paths={"server": smtp_server},
            status="sent",
            dialog=d_id,
            context=f"Sent email '{subject}' to {len(valid_to)} recipient(s)"
        )
        
        return {
            "status": "sent",
            "server": smtp_server,
            "from": parsed_from,
            "recipients": valid_to,
            "subject": subject,
            "attachments_count": len(attachments),
            "auth_source": src
        }
    
    except smtplib.SMTPAuthenticationError:
        return {"status": "error", "error": "Authentication failed. Check credentials or use app-password."}
    except smtplib.SMTPConnectError:
        return {"status": "error", "error": f"Cannot connect to {smtp_server}:{port}"}
    except Exception as e:
        return {"status": "error", "error": f"SMTP failed: {e}"}
    
    finally:
        try:
            if conn:
                conn.quit()
        except Exception:
            pass


def test_connection(server: str, username: str, password: Optional[str] = None,
                    protocol: str = "imap", port: int = None) -> Dict:
    """
    Test email server connection and authentication.
    
    Args:
        server: Server hostname
        username: Email account
        password: Password (optional)
        protocol: 'imap' or 'smtp'
        port: Override default port
    """
    pwd, src = _resolve_credentials(protocol, username, password)
    if not pwd:
        return {"status": "error", "error": "Credentials missing for test."}
    
    conn = None
    try:
        ctx = ssl.create_default_context()
        
        if protocol.lower() == "imap":
            conn = imaplib.IMAP4_SSL(server, port or 993, context=ctx)
            conn.login(username, pwd)
            capabilities = conn.capability()
            conn.logout()
            conn = None
            
            return {
                "status": "ok",
                "protocol": "imap",
                "server": server,
                "port": port or 993,
                "auth_source": src,
                "capabilities": capabilities[0].decode() if capabilities and capabilities[0] else "unknown"
            }
        
        elif protocol.lower() == "smtp":
            conn = smtplib.SMTP_SSL(server, port or 465, context=ctx, timeout=10)
            conn.login(username, pwd)
            conn.quit()
            conn = None
            
            return {
                "status": "ok",
                "protocol": "smtp",
                "server": server,
                "port": port or 465,
                "auth_source": src
            }
        
        elif protocol.lower() == "pop3":
            conn = poplib.POP3_SSL(server, port or 995, context=ctx)
            conn.user(username)
            conn.pass_(pwd)
            count, _ = conn.stat()
            conn.quit()
            conn = None
            
            return {
                "status": "ok",
                "protocol": "pop3",
                "server": server,
                "port": port or 995,
                "message_count": count,
                "auth_source": src
            }
        
        else:
            return {"status": "error", "error": f"Unsupported protocol: {protocol}. Use 'imap', 'smtp', or 'pop3'."}
    
    except imaplib.IMAP4.error as e:
        return {"status": "failed", "protocol": protocol, "error": f"IMAP Error: {e}"}
    except smtplib.SMTPAuthenticationError:
        return {"status": "failed", "protocol": protocol, "error": "Authentication failed."}
    except Exception as e:
        return {"status": "failed", "protocol": protocol, "error": str(e)}
    
    finally:
        try:
            if conn:
                if protocol.lower() == "imap":
                    conn.logout()
                elif protocol.lower() == "smtp":
                    conn.quit()
                elif protocol.lower() == "pop3":
                    conn.quit()
        except Exception:
            pass


def clear_credentials(username: str, protocol: str = "imap") -> Dict:
    """
    Remove saved credentials from OS keyring.
    
    Args:
        username: Email account
        protocol: 'imap' or 'smtp' or 'pop3'
    """
    d_id = dialog_ctx.get()
    
    if _clear_keyring(protocol, username):
        conversation_memory.add(
            op="clear_credentials",
            paths={"user": username},
            status="cleared",
            dialog=d_id,
            context=f"Cleared {protocol} credentials for {username}"
        )
        return {"status": "cleared", "protocol": protocol, "username": username}
    
    return {
        "status": "not_found",
        "message": "No keyring entry found or keyring unavailable.",
        "hint": "Credentials may be stored in environment variables instead."
    }


# ─── Plugin Registration ─────────────────────────────────────────────────────
def register_tools(server: BaseMCPServer):
    """Register all email tools with the MCP server."""
    
    server.register_tool("fetch_emails", {
        "description": "Fetch recent emails securely (IMAP/POP3, lazy connection, auto-closes)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Mail server hostname"},
                "username": {"type": "string", "description": "Email account"},
                "password": {"type": "string", "description": "Password (optional, uses keyring/env)"},
                "folder": {"type": "string", "default": "INBOX", "description": "IMAP folder (ignored for POP3)"},
                "limit": {"type": "integer", "default": 10, "description": "Max messages (capped at 50)"},
                "protocol": {"type": "string", "enum": ["imap", "pop3"], "default": "imap"}
            },
            "required": ["server", "username"]
        }
    }, lambda **kw: fetch_emails(
        kw["server"], kw["username"],
        kw.get("password"), kw.get("folder", "INBOX"),
        kw.get("limit", 10), kw.get("protocol", "imap")
    ))
    
    server.register_tool("search_emails", {
        "description": "Search IMAP mailbox with date filter",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "query": {"type": "string", "default": "ALL", "description": "IMAP search query"},
                "since_days": {"type": "integer", "default": 7},
                "folder": {"type": "string", "default": "INBOX"}
            },
            "required": ["server", "username"]
        }
    }, lambda **kw: search_emails(
        kw["server"], kw["username"],
        kw.get("password"), kw.get("query", "ALL"),
        kw.get("since_days", 7), kw.get("folder", "INBOX")
    ))
    
    server.register_tool("send_email", {
        "description": "Send email via SMTP with TLS and safe attachment validation",
        "inputSchema": {
            "type": "object",
            "properties": {
                "smtp_server": {"type": "string", "description": "SMTP server hostname"},
                "username": {"type": "string", "description": "Sender email"},
                "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient emails"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Plain text body"},
                "password": {"type": "string", "description": "Password (optional)"},
                "attachments": {
                    "type": "array", "items": {"type": "string"},
                    "default": [], "description": "File paths to attach"
                },
                "port": {"type": "integer", "default": 465, "description": "SMTP port (465=SSL, 587=STARTTLS)"},
                "use_tls": {"type": "boolean", "default": True}
            },
            "required": ["smtp_server", "username", "to", "subject", "body"]
        }
    }, lambda **kw: send_email(
        kw["smtp_server"], kw["username"],
        kw["to"], kw["subject"], kw["body"],
        kw.get("password"), kw.get("attachments", []),
        kw.get("port", 465), kw.get("use_tls", True)
    ))
    
    server.register_tool("test_connection", {
        "description": "Verify email server connection and credentials (IMAP/SMTP/POP3)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "protocol": {"type": "string", "enum": ["imap", "smtp", "pop3"], "default": "imap"},
                "port": {"type": "integer", "description": "Override default port"}
            },
            "required": ["server", "username"]
        }
    }, lambda **kw: test_connection(
        kw["server"], kw["username"],
        kw.get("password"), kw.get("protocol", "imap"), kw.get("port")
    ))
    
    server.register_tool("clear_credentials", {
        "description": "Remove saved email credentials from OS keyring",
        "inputSchema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "Email account"},
                "protocol": {"type": "string", "enum": ["imap", "smtp", "pop3"], "default": "imap"}
            },
            "required": ["username"]
        }
    }, lambda **kw: clear_credentials(kw["username"], kw.get("protocol", "imap")))


# ─── Standalone Run ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    from mcp_shared import BaseMCPServer
    server = BaseMCPServer("email-client", "3.2")
    register_tools(server)
    server.run()