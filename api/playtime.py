"""Public report (GET) and authenticated scheduled sample (POST)."""

import json
from http.server import BaseHTTPRequestHandler

from playtime.serverless import handle


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond("GET")

    def do_POST(self):
        self.respond("POST")

    def respond(self, method):
        status, body = handle(method, self.headers.get("Authorization", ""),
                              self.headers.get("X-Playtime-Action", "sample"))
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, s-maxage=60, stale-while-revalidate=60"
                         if method == "GET" and status == 200 else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        # Never include request paths, query strings or credentials in app logs.
        pass
