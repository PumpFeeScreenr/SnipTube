import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def query_params(path: str) -> dict[str, str]:
    parsed = urlparse(path)
    return {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}


def parse_float(value: str | None) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    return float(raw)


def send_bytes(handler, code: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def send_json(handler, code: int, payload: dict, extra_headers: dict[str, str] | None = None) -> None:
    send_bytes(
        handler,
        code,
        json.dumps(payload).encode("utf-8"),
        "application/json; charset=utf-8",
        extra_headers=extra_headers,
    )


def send_file(handler, code: int, path: Path, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
    data = path.read_bytes()
    size = len(data)
    range_header = handler.headers.get("range", "").strip()
    headers = {"Accept-Ranges": "bytes"}
    if extra_headers:
        headers.update(extra_headers)

    if range_header.startswith("bytes="):
        try:
            raw_range = range_header.split("=", 1)[1].split(",", 1)[0]
            start_text, end_text = raw_range.split("-", 1)
            if start_text == "":
                length = int(end_text)
                start = max(0, size - length)
                end = size - 1
            else:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
            start = max(0, start)
            end = min(size - 1, end)
            if start > end or start >= size:
                send_json(handler, 416, {"error": "Requested range not satisfiable"}, {"Content-Range": f"bytes */{size}"})
                return
            chunk = data[start : end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            send_bytes(handler, 206, chunk, content_type, extra_headers=headers)
            return
        except Exception:
            pass

    send_bytes(handler, code, data, content_type, extra_headers=headers)
