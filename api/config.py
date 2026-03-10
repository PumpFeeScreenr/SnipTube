import json
import os
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        worker_url = os.environ.get("WORKER_URL", "").rstrip("/")
        if not worker_url:
            host = self.headers.get("x-forwarded-host") or self.headers.get("host") or ""
            proto = self.headers.get("x-forwarded-proto") or "https"
            if host:
                worker_url = f"{proto}://{host}".rstrip("/")
        self._json(200, {"workerUrl": worker_url})

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
