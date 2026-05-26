"""Minimal stdlib HTTP service for the rollout tutorial baseline.

Exposes:
  GET /health -> 200 "ok"        liveness check used by the proxy + later
                                  steps' readiness gate.
  GET /       -> 200 JSON        reports the container id so a client can
                                  see which replica served the request.
"""

from __future__ import annotations

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer


def container_id() -> str:
    explicit = os.environ.get("APP_REPLICA_ID")
    if explicit:
        return explicit
    return socket.gethostname()


def app_version() -> str:
    return os.environ.get("APP_VERSION", "v1")


class AppHandler(BaseHTTPRequestHandler):
    server_version = "rollout-app/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _write(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if self.path == "/health":
            self._write(200, b"ok", "text/plain; charset=utf-8")
            return
        if self.path == "/":
            payload = {"replica": container_id(), "version": app_version()}
            body = json.dumps(payload).encode("utf-8")
            self._write(200, body, "application/json")
            return
        self._write(404, b"not found", "text/plain; charset=utf-8")


def make_server(host: str = "0.0.0.0", port: int = 8000) -> HTTPServer:
    return HTTPServer((host, port), AppHandler)


def main() -> None:
    port = int(os.environ.get("APP_PORT", "8000"))
    server = make_server(port=port)
    server.serve_forever()


if __name__ == "__main__":
    main()
