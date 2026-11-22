"""Liveness watchdog coverage for the Serena hang class (issue #204)."""
from __future__ import annotations

import http.server
import threading
import time

from engram.pipeline.serena_liveness import check_once, probe_mcp


class _Healthy(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Hung(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        time.sleep(5)

    def log_message(self, *args):
        pass


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/mcp"


def test_healthy_endpoint_passes():
    server, url = _serve(_Healthy)
    try:
        assert probe_mcp(url, timeout_s=1) is True
    finally:
        server.shutdown()


def test_listening_but_hung_endpoint_fails_and_recovers(tmp_path):
    server, url = _serve(_Hung)
    marker = tmp_path / "recovered"
    try:
        assert probe_mcp(url, timeout_s=0.3) is False
        assert check_once(
            url, timeout_s=0.3, on_hang=f"touch {marker}"
        ) is False
        assert marker.exists()
    finally:
        server.shutdown()
