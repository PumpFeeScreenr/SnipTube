from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from api.lib.http import parse_float, query_params, rate_limit_check, send_file, send_json
from worker.app import (
    GIF_MAX_SEC,
    QUALITY_PROFILES,
    TMPDIR,
    clamp_range_to_window,
    cleanup_file,
    cleanup_prefix,
    download_media,
    encode_media,
    fetch_info,
    find_downloaded_media,
    is_youtube_info,
    make_gif,
    parse_clip_range,
    resolve_youtube_window,
    safe_name,
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        allowed, retry_after = rate_limit_check(self, "download", limit=10, window_sec=60)
        if not allowed:
            send_json(
                self,
                429,
                {"error": "Too many download requests. Try again in a minute."},
                {"Cache-Control": "no-store", "Retry-After": str(retry_after)},
            )
            return

        params = query_params(self.path)
        url = params.get("url", "").strip()
        fmt = params.get("format", "mp4").strip().lower()
        quality = params.get("quality", "balanced").strip().lower()

        try:
            start = parse_float(params.get("start"))
            end = parse_float(params.get("end"))
        except ValueError:
            send_json(self, 400, {"error": "start/end must be numeric seconds"}, {"Cache-Control": "no-store"})
            return

        if not url:
            send_json(self, 400, {"error": "Missing url"}, {"Cache-Control": "no-store"})
            return
        if fmt not in {"mp4", "mp3", "webm", "gif"}:
            send_json(self, 400, {"error": "format must be mp4 | mp3 | webm | gif"}, {"Cache-Control": "no-store"})
            return
        if quality not in QUALITY_PROFILES:
            send_json(self, 400, {"error": "quality must be small | balanced | high"}, {"Cache-Control": "no-store"})
            return

        try:
            info = fetch_info(url)
            window = resolve_youtube_window(url, info)
        except ValueError as exc:
            send_json(self, 400, {"error": str(exc)}, {"Cache-Control": "no-store"})
            return
        except Exception as exc:
            send_json(self, 400, {"error": f"Metadata lookup failed: {exc}"}, {"Cache-Control": "no-store"})
            return

        duration = float(info.get("duration") or 0)
        try:
            start, end = parse_clip_range(start, end, duration, fmt)
            if is_youtube_info(info) and duration > 300 and not window["windowed"]:
                raise ValueError(
                    "Videos longer than 5 minutes must use a time-marked YouTube URL. "
                    "Use 'Copy video URL at current time' and try again."
                )
            if window["windowed"]:
                start, end = clamp_range_to_window(start, end, window["window_start"], window["window_end"])
        except ValueError as exc:
            send_json(self, 400, {"error": str(exc)}, {"Cache-Control": "no-store"})
            return

        title = safe_name(info.get("title", "video"))
        trimming = start is not None or end is not None

        uid = uuid.uuid4().hex[:10]
        raw_stem = f"raw_{uid}"
        raw_tmpl = str(TMPDIR / f"{raw_stem}.%(ext)s")
        out_file = TMPDIR / f"out_{uid}.{fmt}"

        try:
            download_media(
                url,
                raw_tmpl,
                fmt,
                quality,
                info=info,
                range_start=window["window_start"] if window["windowed"] else None,
                range_end=window["window_end"] if window["windowed"] else None,
            )
        except Exception as exc:
            cleanup_prefix(raw_stem, 5)
            send_json(self, 500, {"error": f"Download failed: {exc}"}, {"Cache-Control": "no-store"})
            return

        raw_file = find_downloaded_media(raw_stem)
        if not raw_file or not raw_file.exists():
            cleanup_prefix(raw_stem, 5)
            send_json(self, 500, {"error": "Downloaded media was not found on disk"}, {"Cache-Control": "no-store"})
            return

        if fmt == "gif":
            ok, details = make_gif(raw_file, out_file, start or 0.0, end or GIF_MAX_SEC, quality)
        else:
            ok, details = encode_media(raw_file, out_file, fmt, start, end, trimming, quality)
        cleanup_prefix(raw_stem, 30)

        if not ok:
            cleanup_file(out_file, 5)
            send_json(self, 500, {"error": "ffmpeg failed", "details": details}, {"Cache-Control": "no-store"})
            return

        if not out_file.exists():
            send_json(self, 500, {"error": "Output file is missing after processing"}, {"Cache-Control": "no-store"})
            return

        suffix = "snippet" if trimming else "full"
        download_name = f"{title}_{suffix}.{fmt}"
        mime = {
            "gif": "image/gif",
            "mp3": "audio/mpeg",
            "mp4": "video/mp4",
            "webm": "video/webm",
        }[fmt]

        cleanup_file(out_file, 60)
        send_file(
            self,
            200,
            out_file,
            mime,
            {
                "Cache-Control": "no-store",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name)}",
            },
        )

    def log_message(self, *args):
        pass
