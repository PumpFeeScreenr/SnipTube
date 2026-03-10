from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from api.lib.http import query_params, send_json
from worker.app import fetch_info


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = query_params(self.path).get("url", "").strip()
        if not url:
            send_json(self, 400, {"error": "Missing url"}, {"Cache-Control": "no-store"})
            return

        try:
            info = fetch_info(url)
        except Exception as exc:
            send_json(self, 400, {"error": str(exc)}, {"Cache-Control": "no-store"})
            return

        send_json(
            self,
            200,
            {
                "id": info.get("id"),
                "title": info.get("title"),
                "channel": info.get("uploader") or info.get("channel") or "Unknown",
                "duration": int(info.get("duration") or 0),
                "thumb": info.get("thumbnail"),
                "platform": info.get("extractor_key", ""),
            },
            {"Cache-Control": "no-store"},
        )

    def log_message(self, *args):
        pass
