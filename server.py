"""Dependency-free web server for the SOP demo."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine import ModelClient, Session, respond

ROOT = Path(__file__).parent / "web"
SESSIONS: dict[str, Session] = {}
SESSION_LOCKS: dict[str, threading.Lock] = {}
LOCK = threading.RLock()  # guards the session tables only; model calls run under a per-session lock
MODEL = ModelClient()
TTL = 60 * 60 * 4


def new_session() -> tuple[str, Session]:
    now = time.time()
    for old in [k for k, v in SESSIONS.items() if now - v.updated_at > TTL]:
        SESSIONS.pop(old, None)
        SESSION_LOCKS.pop(old, None)
    sid = secrets.token_urlsafe(32)
    session = Session()
    SESSIONS[sid] = session
    SESSION_LOCKS[sid] = threading.Lock()
    return sid, session


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # Do not log chat contents or session identifiers.
        print("%s %s" % (self.address_string(), format % args))

    def response_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:")

    def json_response(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.response_headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self.json_response(200, {"ok": True, "model_enabled": MODEL.enabled})
        if parsed.path == "/api/session":
            sid = parse_qs(parsed.query).get("id", [""])[0]
            with LOCK:
                if sid not in SESSIONS or time.time() - SESSIONS[sid].updated_at > TTL:
                    sid, session = new_session()
                else:
                    session = SESSIONS[sid]
                return self.json_response(200, {"session_id": sid, "session": session.public(), "model_enabled": MODEL.enabled})
        name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        if name not in ("index.html", "app.js", "style.css"):
            return self.json_response(404, {"error": "Not found"})
        body = (ROOT / name).read_bytes()
        mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
        self.send_response(200)
        self.response_headers(mime + "; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path not in ("/api/chat", "/api/reset"):
            return self.json_response(404, {"error": "Not found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 10000 or size <= 0:
                return self.json_response(413, {"error": "Request too large"})
            data = json.loads(self.rfile.read(size))
            sid = data.get("session_id", "")
            if not isinstance(sid, str):
                raise ValueError("Invalid session")
            if self.path == "/api/reset":
                with LOCK:
                    SESSIONS.pop(sid, None)
                    SESSION_LOCKS.pop(sid, None)
                    sid, session = new_session()
                    return self.json_response(200, {"session_id": sid, "session": session.public()})
            text = data.get("message", "")
            if not isinstance(text, str) or len(text) > 2000:
                raise ValueError("Message must be at most 2000 characters")
            with LOCK:
                if sid not in SESSIONS or time.time() - SESSIONS[sid].updated_at > TTL:
                    return self.json_response(404, {"error": "Session expired. Start a new chat."})
                session, session_lock = SESSIONS[sid], SESSION_LOCKS[sid]
            with session_lock:
                answer = respond(session, text, MODEL)
                state = session.public()
            return self.json_response(200, {"reply": answer, "session": state})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.json_response(400, {"error": str(exc)})


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    print(f"SOP claims demo at http://localhost:{port} (model {'on' if MODEL.enabled else 'fallback mode'})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
