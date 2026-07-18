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
PORT = int(os.environ.get("PORT", "8080"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/") or self.path == "/health":
            self._proxy()
            return
        if self.path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def _proxy(self):
        try:
            with urllib.request.urlopen(RELAY + self.path, timeout=10) as r:
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

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"dashboard on :{PORT} -> relay {RELAY}")
    httpd.serve_forever()
