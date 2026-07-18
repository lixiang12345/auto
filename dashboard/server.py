"""Static dashboard server — serves the single-page UI and proxies /api to the relay.

Kept dependency-free (stdlib only) so the dashboard image is tiny. It fetches
live data from the relay over the internal Docker network.
"""

from __future__ import annotations

import os
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
RELAY = os.environ.get("RELAY_URL", "http://relay:8000")
REG = os.environ.get("REG_URL", "http://registration:8001")
PORT = int(os.environ.get("PORT", "8080"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def do_GET(self):
        if self.path.startswith("/reg/"):
            self._proxy(REG + self.path[4:])
            return
        if self.path.startswith("/api/") or self.path == "/health":
            self._proxy(RELAY + self.path)
            return
        if self.path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/reg/"):
            self._proxy_post(REG + self.path[4:])
            return
        if self.path.startswith("/api/"):
            self._proxy_post(RELAY + self.path)
            return
        self.send_response(405)
        self.end_headers()

    def _proxy(self, url: str):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = r.read()
            self.send_response(r.status)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # noqa: BLE001
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(f'{{"error":"{e}"}}'.encode())

    def _proxy_post(self, url: str):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            self.send_response(r.status)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:  # noqa: BLE001
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(f'{{"error":"{e}"}}'.encode())

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"dashboard on :{PORT} -> relay {RELAY}")
    httpd.serve_forever()
