"""Read-only sanitized calendar feed; no authentication or API sampling here."""

import json
from http.server import BaseHTTPRequestHandler

from playtime.serverless import handle


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code, body = handle("GET", action="calendar")
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, s-maxage=60" if code == 200 else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.end_headers()

    def log_message(self, format, *args):
        pass
