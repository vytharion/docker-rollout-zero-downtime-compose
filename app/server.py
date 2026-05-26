"""Minimal stdlib HTTP service for the rollout tutorial.

Exposes:
  GET /health -> 200 "ok"        liveness check. Returns OK while the process
                                  is alive; never reflects warm-up or drain.
  GET /ready  -> 200 / 503       readiness probe used by the docker
                                  healthcheck and (later) the proxy cutover.
                                  503 during warm-up, or after a drain signal.
  GET /       -> 200 JSON        reports the replica id + app version so a
                                  client can see which container served it.
"""

from __future__ import annotations

import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable


def container_id() -> str:
    explicit = os.environ.get("APP_REPLICA_ID")
    if explicit:
        return explicit
    return socket.gethostname()


def app_version() -> str:
    return os.environ.get("APP_VERSION", "v1")


class ReadinessState:
    def __init__(
        self,
        warmup_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._warmup = max(0.0, warmup_seconds)
        self._clock = clock
        self._origin = clock()
        self._forced_not_ready = False

    def mark_not_ready(self) -> None:
        self._forced_not_ready = True

    def mark_ready(self) -> None:
        self._forced_not_ready = False
        self._origin = self._clock() - self._warmup

    def is_ready(self) -> bool:
        if self._forced_not_ready:
            return False
        return (self._clock() - self._origin) >= self._warmup


def _warmup_from_env() -> float:
    raw = os.environ.get("APP_READY_AFTER_SECONDS", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


readiness = ReadinessState(warmup_seconds=_warmup_from_env())


class AppHandler(BaseHTTPRequestHandler):
    server_version = "rollout-app/1.0"
    readiness_state: ReadinessState = readiness

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _write(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_ready(self) -> None:
        if self.readiness_state.is_ready():
            self._write(200, b"ready", "text/plain; charset=utf-8")
            return
        self._write(503, b"not ready", "text/plain; charset=utf-8")

    def _handle_root(self) -> None:
        payload = {"replica": container_id(), "version": app_version()}
        body = json.dumps(payload).encode("utf-8")
        self._write(200, body, "application/json")

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if self.path == "/health":
            self._write(200, b"ok", "text/plain; charset=utf-8")
            return
        if self.path == "/ready":
            self._handle_ready()
            return
        if self.path == "/":
            self._handle_root()
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
