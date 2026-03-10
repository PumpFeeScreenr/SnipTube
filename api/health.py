from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from api.lib.http import send_json
from worker.app import ffmpeg_binary, resolve_cookie_file


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        send_json(
            self,
            200,
            {
                "status": "ok",
                "ffmpeg": ffmpeg_binary() is not None,
                "cookies": resolve_cookie_file() is not None,
            },
            {"Cache-Control": "no-store"},
        )

    def log_message(self, *args):
        pass
