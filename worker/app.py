"""
SnipTube worker

This service handles metadata lookup and media processing. Deploy it on a
long-running host with ffmpeg available, then point the static frontend at it.
"""

import base64
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, after_this_request, jsonify, request, send_file
from flask_cors import CORS

try:
    import yt_dlp
except ImportError as exc:
    raise SystemExit("Missing dependency: pip install -r requirements.txt") from exc

try:
    import certifi
except ImportError:
    certifi = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None


app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sniptube-worker")

TMPDIR = Path(tempfile.gettempdir()) / "sniptube"
TMPDIR.mkdir(exist_ok=True)

GIF_MAX_SEC = 30
PREVIEW_CACHE_TTL_SEC = 1800
PREVIEW_PROFILE = "preview"

IGNORED_DOWNLOAD_SUFFIXES = (
    ".description",
    ".info.json",
    ".jpg",
    ".jpeg",
    ".part",
    ".png",
    ".srt",
    ".temp",
    ".vtt",
    ".webp",
    ".ytdl",
)
MEDIA_SUFFIX_PRIORITY = (
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".m4a",
    ".mp3",
    ".aac",
    ".opus",
)
COOKIE_FILE = TMPDIR / "cookies.txt"
PREVIEW_CACHE_DIR = TMPDIR / "preview_cache"
PREVIEW_CACHE_DIR.mkdir(exist_ok=True)
PREVIEW_CACHE_CLEANUP_INTERVAL_SEC = 300
PREVIEW_LOCKS: dict[str, threading.Lock] = {}
PREVIEW_LOCKS_GUARD = threading.Lock()
LAST_PREVIEW_CACHE_CLEANUP = 0.0
QUALITY_PROFILES = {
    "small": {
        "gif": {
            "download_format": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]/best",
            "fps": 10,
            "merge_output_format": "mp4",
            "width": 360,
        },
        "mp3": {
            "audio_bitrate": "96k",
            "download_format": "bestaudio/best",
        },
        "mp4": {
            "audio_bitrate": "96k",
            "crf": 30,
            "download_format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]/best",
            "merge_output_format": "mp4",
            "preset": "veryfast",
        },
        "webm": {
            "audio_bitrate": "96k",
            "crf": 38,
            "download_format": "bestvideo[height<=480][ext=webm]+bestaudio[ext=webm]/bestvideo[height<=480]+bestaudio/best[height<=480][ext=webm]/best[height<=480]/best",
            "merge_output_format": "webm",
        },
    },
    "balanced": {
        "gif": {
            "download_format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]/best",
            "fps": 12,
            "merge_output_format": "mp4",
            "width": 480,
        },
        "mp3": {
            "audio_bitrate": "160k",
            "download_format": "bestaudio/best",
        },
        "mp4": {
            "audio_bitrate": "128k",
            "crf": 24,
            "download_format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
            "merge_output_format": "mp4",
            "preset": "fast",
        },
        "webm": {
            "audio_bitrate": "128k",
            "crf": 34,
            "download_format": "bestvideo[height<=720][ext=webm]+bestaudio[ext=webm]/bestvideo[height<=720]+bestaudio/best[height<=720][ext=webm]/best[height<=720]/best",
            "merge_output_format": "webm",
        },
    },
    "high": {
        "gif": {
            "download_format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "fps": 15,
            "merge_output_format": "mp4",
            "width": 640,
        },
        "mp3": {
            "audio_bitrate": "256k",
            "download_format": "bestaudio/best",
        },
        "mp4": {
            "audio_bitrate": "160k",
            "crf": 20,
            "download_format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "preset": "fast",
        },
        "webm": {
            "audio_bitrate": "160k",
            "crf": 30,
            "download_format": "bestvideo[ext=webm]+bestaudio[ext=webm]/bestvideo+bestaudio/best",
            "merge_output_format": "webm",
        },
    },
}


def configure_ssl_certificates() -> None:
    if certifi is None:
        return

    cafile = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", cafile)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", cafile)


def safe_name(value: str) -> str:
    return re.sub(r"[^\w\s\-.]", "", value or "").strip()[:72] or "video"


def cleanup_file(path: Path, delay: int = 120) -> None:
    def _remove() -> None:
        time.sleep(delay)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    threading.Thread(target=_remove, daemon=True).start()


def cleanup_prefix(prefix: str, delay: int = 120) -> None:
    def _remove() -> None:
        time.sleep(delay)
        for path in TMPDIR.glob(f"{prefix}*"):
            if path.is_file():
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    threading.Thread(target=_remove, daemon=True).start()


def cleanup_stale_previews(force: bool = False) -> None:
    global LAST_PREVIEW_CACHE_CLEANUP

    now = time.time()
    if not force and (now - LAST_PREVIEW_CACHE_CLEANUP) < PREVIEW_CACHE_CLEANUP_INTERVAL_SEC:
        return
    LAST_PREVIEW_CACHE_CLEANUP = now

    cutoff = now - PREVIEW_CACHE_TTL_SEC
    for path in PREVIEW_CACHE_DIR.glob("*.mp4"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def preview_generation_lock(cache_file: Path):
    key = cache_file.name
    with PREVIEW_LOCKS_GUARD:
        lock = PREVIEW_LOCKS.setdefault(key, threading.Lock())
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def find_downloaded_media(stem: str) -> Path | None:
    candidates = []
    for path in TMPDIR.glob(f"{stem}*"):
        if not path.is_file():
            continue
        if path.name.endswith(IGNORED_DOWNLOAD_SUFFIXES):
            continue
        candidates.append(path)

    if not candidates:
        return None

    def sort_key(path: Path) -> tuple[int, float]:
        suffix = path.suffix.lower()
        try:
            priority = MEDIA_SUFFIX_PRIORITY.index(suffix)
        except ValueError:
            priority = len(MEDIA_SUFFIX_PRIORITY)
        return (priority, -path.stat().st_mtime)

    candidates.sort(key=sort_key)
    return candidates[0]


def ffmpeg_binary() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    if imageio_ffmpeg is None:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        log.exception("Failed to resolve bundled ffmpeg")
        return None


def check_ffmpeg() -> None:
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required on the worker host")
    result = subprocess.run([ffmpeg, "-version"], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg is required on the worker host")


def resolve_cookie_file() -> str | None:
    cookie_path = os.environ.get("YTDLP_COOKIES_PATH", "").strip()
    if cookie_path:
        path = Path(cookie_path)
        if path.exists():
            return str(path)
        log.warning("Ignoring missing YTDLP_COOKIES_PATH: %s", cookie_path)

    encoded = os.environ.get("YTDLP_COOKIES_B64", "").strip()
    if not encoded:
        return None

    try:
        COOKIE_FILE.write_bytes(base64.b64decode(encoded))
    except Exception:
        log.exception("Failed to decode YTDLP_COOKIES_B64")
        return None

    return str(COOKIE_FILE)


def ytdlp_opts(extra: dict | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    ffmpeg = ffmpeg_binary()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    cookie_file = resolve_cookie_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if extra:
        opts.update(extra)
    return opts


def fetch_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(ytdlp_opts({"skip_download": True})) as ydl:
        return ydl.extract_info(url, download=False)


def select_preview_url(info: dict) -> str | None:
    if (info.get("extractor_key") or "").lower() != "twitter":
        return None

    direct_mp4 = []
    hls_fallback = []
    for fmt in info.get("formats") or []:
        media_url = fmt.get("url")
        if not media_url:
            continue

        protocol = (fmt.get("protocol") or "").lower()
        ext = (fmt.get("ext") or "").lower()
        height = int(fmt.get("height") or 0)
        width = int(fmt.get("width") or 0)
        score = (height * width, height, width)

        if protocol in {"https", "http"} and ext == "mp4":
            direct_mp4.append((score, media_url))
            continue
        if protocol == "m3u8_native":
            hls_fallback.append((score, media_url))

    if direct_mp4:
        return max(direct_mp4, key=lambda item: item[0])[1]
    if hls_fallback:
        return max(hls_fallback, key=lambda item: item[0])[1]
    return None


def get_quality_profile(fmt: str, quality: str) -> dict:
    quality_profiles = QUALITY_PROFILES.get(quality)
    if quality_profiles is None:
        raise ValueError(f"Unsupported quality: {quality}")

    profile = quality_profiles.get(fmt)
    if profile is None:
        raise ValueError(f"Unsupported quality profile for format: {fmt}")

    return profile


def download_media(url: str, raw_tmpl: str, profile: str, quality: str) -> None:
    if profile == PREVIEW_PROFILE:
        download_opts = {
            "format": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]/best[ext=mp4]/best",
            "outtmpl": raw_tmpl,
            "merge_output_format": "mp4",
        }
    else:
        profile_opts = get_quality_profile(profile, quality)
        download_opts = {
            "format": profile_opts["download_format"],
            "outtmpl": raw_tmpl,
        }
        merge_output_format = profile_opts.get("merge_output_format")
        if merge_output_format:
            download_opts["merge_output_format"] = merge_output_format

    with yt_dlp.YoutubeDL(ytdlp_opts(download_opts)) as ydl:
        ydl.download([url])


def parse_clip_range(start: float | None, end: float | None, duration: float, fmt: str) -> tuple[float | None, float | None]:
    if start is not None and start < 0:
        raise ValueError("start must be 0 or greater")
    if end is not None and end < 0:
        raise ValueError("end must be 0 or greater")

    if duration > 0 and start is not None and start >= duration:
        raise ValueError("start exceeds video duration")

    if duration > 0 and end is not None:
        end = min(end, duration)

    if fmt == "gif":
        start = 0.0 if start is None else start
        if end is None or (end - start) > GIF_MAX_SEC:
            end = start + GIF_MAX_SEC
        if duration > 0:
            end = min(end, duration)

    if start is not None and end is not None and end <= start:
        raise ValueError("end must be greater than start")

    return start, end


def run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        return False, "ffmpeg executable is not available"
    normalized = list(cmd)
    normalized[0] = ffmpeg
    result = subprocess.run(normalized, capture_output=True)
    if result.returncode != 0:
        return False, result.stderr.decode(errors="replace")[-1200:]
    return True, ""


def encode_media(raw: Path, out: Path, fmt: str, start: float | None, end: float | None, trimming: bool, quality: str) -> tuple[bool, str]:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(raw)]
    if start is not None and end is not None:
        cmd += ["-t", str(end - start)]
    elif end is not None:
        cmd += ["-to", str(end)]

    profile = get_quality_profile(fmt, quality)
    if fmt == "mp3":
        cmd += ["-vn", "-ar", "44100", "-ac", "2", "-b:a", profile["audio_bitrate"], str(out)]
    elif fmt == "webm":
        cmd += [
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            str(profile["crf"]),
            "-deadline",
            "good",
            "-row-mt",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            profile["audio_bitrate"],
        ]
        cmd.append(str(out))
    else:
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            profile.get("preset", "fast"),
            "-crf",
            str(profile["crf"]),
            "-c:a",
            "aac",
            "-b:a",
            profile["audio_bitrate"],
        ] if trimming else ["-c", "copy"]
        cmd.append(str(out))

    return run_ffmpeg(cmd)


def make_gif(raw: Path, out: Path, start: float, end: float, quality: str) -> tuple[bool, str]:
    profile = get_quality_profile("gif", quality)
    palette = TMPDIR / f"palette_{out.stem}.png"
    duration = end - start
    fps = profile["fps"]
    scale = f"scale='min({profile['width']},iw)':-1:flags=lanczos"

    palette_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(raw),
        "-vf",
        f"fps={fps},{scale},palettegen=stats_mode=diff",
        str(palette),
    ]
    ok, msg = run_ffmpeg(palette_cmd)
    if not ok:
        return False, f"Palette pass failed: {msg}"

    render_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(raw),
        "-i",
        str(palette),
        "-lavfi",
        f"fps={fps},{scale} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
        str(out),
    ]
    ok, msg = run_ffmpeg(render_cmd)
    cleanup_file(palette, 5)
    if not ok:
        return False, f"Render pass failed: {msg}"
    return True, ""


def make_full_preview(raw: Path, out: Path) -> tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(raw),
        "-vf",
        "fps=12,scale='min(640,iw)':-2:flags=fast_bilinear",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "34",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        str(out),
    ]
    return run_ffmpeg(cmd)


def preview_cache_path(url: str) -> Path:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]
    return PREVIEW_CACHE_DIR / f"{key}.mp4"


def has_fresh_preview(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    age = time.time() - path.stat().st_mtime
    return age < PREVIEW_CACHE_TTL_SEC


configure_ssl_certificates()
check_ffmpeg()


@app.route("/api/info")
def api_info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400

    try:
        info = fetch_info(url)
    except yt_dlp.utils.DownloadError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Metadata fetch failed")
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "id": info.get("id"),
            "title": info.get("title"),
            "channel": info.get("uploader") or info.get("channel") or "Unknown",
            "duration": int(info.get("duration") or 0),
            "thumb": info.get("thumbnail"),
            "platform": info.get("extractor_key", ""),
            "previewUrl": select_preview_url(info),
        }
    )


@app.route("/api/download")
def api_download():
    url = request.args.get("url", "").strip()
    fmt = request.args.get("format", "mp4").strip().lower()
    quality = request.args.get("quality", "balanced").strip().lower()
    start = request.args.get("start", type=float)
    end = request.args.get("end", type=float)

    if not url:
        return jsonify({"error": "Missing url"}), 400
    if fmt not in {"mp4", "mp3", "webm", "gif"}:
        return jsonify({"error": "format must be mp4 | mp3 | webm | gif"}), 400
    if quality not in QUALITY_PROFILES:
        return jsonify({"error": "quality must be small | balanced | high"}), 400

    try:
        info = fetch_info(url)
    except Exception as exc:
        return jsonify({"error": f"Metadata lookup failed: {exc}"}), 400

    duration = float(info.get("duration") or 0)
    try:
        start, end = parse_clip_range(start, end, duration, fmt)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    title = safe_name(info.get("title", "video"))
    trimming = start is not None or end is not None

    uid = uuid.uuid4().hex[:10]
    raw_stem = f"raw_{uid}"
    raw_tmpl = str(TMPDIR / f"{raw_stem}.%(ext)s")
    out_file = TMPDIR / f"out_{uid}.{fmt}"

    try:
        download_media(url, raw_tmpl, fmt, quality)
    except Exception as exc:
        cleanup_prefix(raw_stem, 5)
        return jsonify({"error": f"Download failed: {exc}"}), 500

    raw_file = find_downloaded_media(raw_stem)
    if not raw_file or not raw_file.exists():
        cleanup_prefix(raw_stem, 5)
        return jsonify({"error": "Downloaded media was not found on disk"}), 500

    if fmt == "gif":
        ok, details = make_gif(raw_file, out_file, start or 0.0, end or GIF_MAX_SEC, quality)
    else:
        ok, details = encode_media(raw_file, out_file, fmt, start, end, trimming, quality)
    cleanup_prefix(raw_stem, 30)

    if not ok:
        cleanup_file(out_file, 5)
        return jsonify({"error": "ffmpeg failed", "details": details}), 500

    if not out_file.exists():
        return jsonify({"error": "Output file is missing after processing"}), 500

    suffix = "snippet" if trimming else "full"
    download_name = f"{title}_{suffix}.{fmt}"
    mime = {
        "gif": "image/gif",
        "mp3": "audio/mpeg",
        "mp4": "video/mp4",
        "webm": "video/webm",
    }[fmt]

    @after_this_request
    def cleanup_response(response):
        cleanup_file(out_file, 60)
        return response

    return send_file(str(out_file), mimetype=mime, as_attachment=True, download_name=download_name)


@app.route("/api/preview")
def api_preview():
    url = request.args.get("url", "").strip()

    if not url:
        return jsonify({"error": "Missing url"}), 400

    cleanup_stale_previews()
    cache_file = preview_cache_path(url)
    if has_fresh_preview(cache_file):
        return send_file(str(cache_file), mimetype="video/mp4", as_attachment=False, conditional=True)

    with preview_generation_lock(cache_file):
        if has_fresh_preview(cache_file):
            return send_file(str(cache_file), mimetype="video/mp4", as_attachment=False, conditional=True)

        temp_out = PREVIEW_CACHE_DIR / f"{cache_file.stem}.{uuid.uuid4().hex[:8]}.tmp.mp4"

        uid = uuid.uuid4().hex[:10]
        raw_stem = f"{PREVIEW_PROFILE}_raw_{uid}"
        raw_tmpl = str(TMPDIR / f"{raw_stem}.%(ext)s")

        try:
            download_media(url, raw_tmpl, PREVIEW_PROFILE, "balanced")
        except Exception as exc:
            cleanup_prefix(raw_stem, 5)
            return jsonify({"error": f"Preview download failed: {exc}"}), 500

        raw_file = find_downloaded_media(raw_stem)
        if not raw_file or not raw_file.exists():
            cleanup_prefix(raw_stem, 5)
            return jsonify({"error": "Preview source media was not found on disk"}), 500

        ok, details = make_full_preview(raw_file, temp_out)
        cleanup_prefix(raw_stem, 30)

        if not ok:
            cleanup_file(temp_out, 5)
            return jsonify({"error": "preview ffmpeg failed", "details": details}), 500

        if not temp_out.exists():
            return jsonify({"error": "Preview output is missing after processing"}), 500
        temp_out.replace(cache_file)
    return send_file(str(cache_file), mimetype="video/mp4", as_attachment=False, conditional=True)


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "cookies": resolve_cookie_file() is not None,
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    log.info("SnipTube worker listening on http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
