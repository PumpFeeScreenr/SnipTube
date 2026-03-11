import io
import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from api.lib.http import parse_float, query_params, send_file


class FakeHandler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.status = None
        self.sent_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def header(self, key):
        for k, v in self.sent_headers:
            if k.lower() == key.lower():
                return v
        return None


class HttpUtilsTests(unittest.TestCase):
    def test_query_params(self):
        params = query_params("/api/info?url=https%3A%2F%2Fexample.com&x=1&x=2")
        self.assertEqual(params["url"], "https://example.com")
        self.assertEqual(params["x"], "2")

    def test_parse_float(self):
        self.assertEqual(parse_float("1.25"), 1.25)
        self.assertEqual(parse_float(" 2 "), 2.0)
        self.assertIsNone(parse_float(""))
        self.assertIsNone(parse_float(None))

    def test_send_file_no_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.bin"
            path.write_bytes(b"abcdef")
            handler = FakeHandler()
            send_file(handler, 200, path, "application/octet-stream")
            self.assertEqual(handler.status, 200)
            self.assertEqual(handler.wfile.getvalue(), b"abcdef")
            self.assertEqual(handler.header("Accept-Ranges"), "bytes")
            self.assertEqual(handler.header("Content-Type"), "application/octet-stream")

    def test_send_file_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.bin"
            path.write_bytes(b"abcdef")
            handler = FakeHandler(headers={"range": "bytes=2-4"})
            send_file(handler, 200, path, "application/octet-stream")
            self.assertEqual(handler.status, 206)
            self.assertEqual(handler.wfile.getvalue(), b"cde")
            self.assertEqual(handler.header("Content-Range"), "bytes 2-4/6")

    def test_send_file_suffix_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.bin"
            path.write_bytes(b"abcdef")
            handler = FakeHandler(headers={"range": "bytes=-3"})
            send_file(handler, 200, path, "application/octet-stream")
            self.assertEqual(handler.status, 206)
            self.assertEqual(handler.wfile.getvalue(), b"def")
            self.assertEqual(handler.header("Content-Range"), "bytes 3-5/6")

    def test_send_file_invalid_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.bin"
            path.write_bytes(b"abcdef")
            handler = FakeHandler(headers={"range": "bytes=99-100"})
            send_file(handler, 200, path, "application/octet-stream")
            self.assertEqual(handler.status, 416)
            payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
            self.assertEqual(payload["error"], "Requested range not satisfiable")


if __name__ == "__main__":
    unittest.main()
