import json
import threading
import urllib.request
from contextlib import contextmanager
from http.server import HTTPServer

import pytest

from app.server import AppHandler, container_id, app_version


@contextmanager
def running_server():
    server = HTTPServer(("127.0.0.1", 0), AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def fetch(url: str):
    with urllib.request.urlopen(url, timeout=2) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_health_returns_ok():
    with running_server() as base:
        status, ctype, body = fetch(f"{base}/health")
    assert status == 200
    assert body == b"ok"
    assert ctype.startswith("text/plain")


def test_root_returns_replica_metadata():
    with running_server() as base:
        status, ctype, body = fetch(f"{base}/")
    assert status == 200
    assert ctype.startswith("application/json")
    payload = json.loads(body)
    assert "replica" in payload and payload["replica"]
    assert "version" in payload and payload["version"]


def test_unknown_path_404s():
    with running_server() as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            fetch(f"{base}/does-not-exist")
    assert exc.value.code == 404


def test_replica_id_honors_env_override(monkeypatch):
    monkeypatch.setenv("APP_REPLICA_ID", "green-7")
    assert container_id() == "green-7"


def test_version_honors_env_override(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v9")
    assert app_version() == "v9"
