from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from api.lib.http import query_params, rate_limit_check, send_bytes, send_json
from worker.app import fetch_remote_media


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        allowed, retry_after = rate_limit_check(self, "x_preview", limit=30, window_sec=60)
        if not allowed:
            send_json(
                self,
                429,
                {"error": "Too many preview requests. Try again shortly."},
                {"Cache-Control": "no-store", "Retry-After": str(retry_after)},
            )
            return

        media_url = query_params(self.path).get("media_url", "").strip()
        if not media_url:
            send_json(self, 400, {"error": "Missing media_url"}, {"Cache-Control": "no-store"})
            return

        try:
            status, body, headers = fetch_remote_media(media_url, self.headers.get("range"))
        except ValueError as exc:
            send_json(self, 400, {"error": str(exc)}, {"Cache-Control": "no-store"})
            return
        except Exception as exc:
            send_json(self, 502, {"error": str(exc)}, {"Cache-Control": "no-store"})
            return

        content_type = headers.pop("Content-Type", "video/mp4")
        send_bytes(self, status, body, content_type, headers)

    def log_message(self, *args):
        pass
