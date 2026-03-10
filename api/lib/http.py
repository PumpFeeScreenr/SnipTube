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
    send_bytes(handler, code, path.read_bytes(), content_type, extra_headers=extra_headers)
