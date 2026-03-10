from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from api.lib.http import query_params, send_file, send_json
from worker.app import (
    PREVIEW_CACHE_DIR,
    cleanup_file,
    cleanup_prefix,
    download_media,
    find_downloaded_media,
    has_fresh_preview,
    make_full_preview,
    preview_cache_path,
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = query_params(self.path).get("url", "").strip()
        if not url:
            send_json(self, 400, {"error": "Missing url"}, {"Cache-Control": "no-store"})
            return

        cache_file = preview_cache_path(url)
        if has_fresh_preview(cache_file):
            send_file(self, 200, cache_file, "video/mp4", {"Cache-Control": "public, max-age=300"})
            return

        temp_out = PREVIEW_CACHE_DIR / f"{cache_file.stem}.{uuid.uuid4().hex[:8]}.tmp.mp4"
        uid = uuid.uuid4().hex[:10]
        raw_stem = f"preview_raw_{uid}"
        raw_tmpl = str((PREVIEW_CACHE_DIR.parent / f"{raw_stem}.%(ext)s"))

        try:
            download_media(url, raw_tmpl, "preview", "balanced")
        except Exception as exc:
            cleanup_prefix(raw_stem, 5)
            send_json(self, 500, {"error": f"Preview download failed: {exc}"}, {"Cache-Control": "no-store"})
            return

        raw_file = find_downloaded_media(raw_stem)
        if not raw_file or not raw_file.exists():
            cleanup_prefix(raw_stem, 5)
            send_json(self, 500, {"error": "Preview source media was not found on disk"}, {"Cache-Control": "no-store"})
            return

        ok, details = make_full_preview(raw_file, temp_out)
        cleanup_prefix(raw_stem, 30)

        if not ok:
            cleanup_file(temp_out, 5)
            send_json(self, 500, {"error": "preview ffmpeg failed", "details": details}, {"Cache-Control": "no-store"})
            return

        if not temp_out.exists():
            send_json(self, 500, {"error": "Preview output is missing after processing"}, {"Cache-Control": "no-store"})
            return

        temp_out.replace(cache_file)
        send_file(self, 200, cache_file, "video/mp4", {"Cache-Control": "public, max-age=300"})

    def log_message(self, *args):
        pass
