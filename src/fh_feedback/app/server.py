"""Chat server for FH ReviewIQ. Stdlib only (http.server) — serves the
static chat UI and a streaming /api/chat endpoint backed by rag.stream_answer().

Local:  python -m fh_feedback.app.server [--port 8765]
Hosted: reads PORT from the environment (the convention most PaaS platforms
        use) and binds 0.0.0.0; set FEEDBACK_RADAR_TOKEN so the app isn't
        open to anyone with the URL - see CLAUDE.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fh_feedback.app.rag import GENERATION_BACKEND, stream_answer
from fh_feedback.search import preload_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
}

ACCESS_TOKEN = os.environ.get("FEEDBACK_RADAR_TOKEN", "").strip()
RATE_LIMIT_PER_MIN = int(os.environ.get("FEEDBACK_RADAR_RATE_LIMIT", "30"))

# Simple in-memory global rate limiter - a safety net against runaway API
# spend on a shared/hosted deployment, not a precision tool. Resets every
# 60s; shared across all callers by design (protects total spend, not a
# per-user quota).
_rate_window_start = time.time()
_rate_count = 0


def _rate_limited() -> bool:
    global _rate_window_start, _rate_count
    now = time.time()
    if now - _rate_window_start > 60:
        _rate_window_start = now
        _rate_count = 0
    _rate_count += 1
    return _rate_count > RATE_LIMIT_PER_MIN


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path: str):
        fp = (STATIC_DIR / rel_path.lstrip("/")).resolve()
        if STATIC_DIR not in fp.parents and fp != STATIC_DIR:
            self.send_error(403)
            return
        if not fp.exists() or not fp.is_file():
            self.send_error(404)
            return
        body = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(fp.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not ACCESS_TOKEN:
            return True  # no token configured - local/dev mode, wide open
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {ACCESS_TOKEN}"

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send_static("index.html")
        elif path in ("/app.js", "/app.css") or path.startswith("/img/"):
            self._send_static(path)
        elif path == "/api/config":
            # tells the frontend whether it needs to prompt for a token,
            # without ever revealing the token itself
            self._send_json(200, {"auth_required": bool(ACCESS_TOKEN)})
        else:
            self.send_error(404)

    def _write_chunk(self, obj: dict):
        line = (json.dumps(obj) + "\n").encode("utf-8")
        self.wfile.write(f"{len(line):x}\r\n".encode("ascii"))
        self.wfile.write(line)
        self.wfile.write(b"\r\n")

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404)
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if _rate_limited():
            self._send_json(429, {"error": "rate limited - too many questions this minute, try again shortly"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send_json(400, {"error": "bad request body"})
            return

        message = (body.get("message") or "").strip()
        history = body.get("history") or []
        if not message:
            self._send_json(400, {"error": "empty message"})
            return

        # Streamed as newline-delimited JSON over chunked transfer encoding:
        # {"type":"sources",...} the instant retrieval finishes, then
        # {"type":"delta","text":...} as the summary is written, then one
        # {"type":"done","answer":...,"sources":...}.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for event in stream_answer(message, history):
                self._write_chunk(event)
        except Exception as e:  # noqa: BLE001 - surface any pipeline error to the UI
            log.exception("chat pipeline failed")
            self._write_chunk({"type": "done", "answer": f"Server error: {e}", "sources": []})
        finally:
            self.wfile.write(b"0\r\n\r\n")


def run(port: int | None = None, open_browser: bool = True) -> None:
    log.info("generation backend: %s", GENERATION_BACKEND)
    if GENERATION_BACKEND == "cli" and os.environ.get("PORT"):
        log.warning(
            "running with the 'cli' backend (no ANTHROPIC_API_KEY) on what looks like a hosted "
            "platform (PORT env var set) - this uses YOUR personal Claude Code login for every "
            "request from every visitor. Fine for a quick test; set ANTHROPIC_API_KEY before "
            "sharing the URL with anyone else."
        )
    if not ACCESS_TOKEN:
        log.warning("FEEDBACK_RADAR_TOKEN not set - /api/chat is open to anyone who can reach this server")

    log.info("loading embedding model...")
    preload_model()
    log.info("model loaded")

    # Cloud platforms (Render, Fly.io, etc.) set PORT and expect a bind on
    # 0.0.0.0; local dev defaults to loopback-only for safety.
    env_port = os.environ.get("PORT")
    bind_host = "0.0.0.0" if env_port else "127.0.0.1"
    port = port or int(env_port or 8765)

    server = ThreadingHTTPServer((bind_host, port), Handler)
    url = f"http://{'127.0.0.1' if bind_host == '0.0.0.0' else bind_host}:{port}"
    log.info("FH ReviewIQ running at %s (bound %s:%s)", url, bind_host, port)
    if open_browser and bind_host == "127.0.0.1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    run(port=args.port, open_browser=not args.no_browser)
