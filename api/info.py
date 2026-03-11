from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from api.lib.http import query_params, rate_limit_check, send_json
from worker.app import fetch_info, normalize_media_info, resolve_youtube_window, select_preview_url


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        allowed, retry_after = rate_limit_check(self, "info", limit=30, window_sec=60)
        if not allowed:
            send_json(
                self,
                429,
                {"error": "Too many metadata requests. Try again shortly."},
                {"Cache-Control": "no-store", "Retry-After": str(retry_after)},
            )
            return

        url = query_params(self.path).get("url", "").strip()
        if not url:
            send_json(self, 400, {"error": "Missing url"}, {"Cache-Control": "no-store"})
            return

        try:
            raw_info = fetch_info(url)
            info, playlist_index = normalize_media_info(raw_info)
            window = resolve_youtube_window(url, info)
        except ValueError as exc:
            send_json(self, 400, {"error": str(exc)}, {"Cache-Control": "no-store"})
            return
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
                "previewUrl": select_preview_url(info),
                "playlistIndex": playlist_index,
                "initialSeek": window["initial_seek"],
                "clipWindowStart": window["window_start"],
                "clipWindowEnd": window["window_end"],
                "windowed": window["windowed"],
            },
            {"Cache-Control": "no-store"},
        )

    def log_message(self, *args):
        pass
