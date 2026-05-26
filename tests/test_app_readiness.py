import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import HTTPServer

import pytest

from app.server import AppHandler, ReadinessState, readiness


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


@pytest.fixture(autouse=True)
def _reset_global_readiness():
    readiness.mark_ready()
    yield
    readiness.mark_ready()


def fetch(url: str):
    with urllib.request.urlopen(url, timeout=2) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_ready_endpoint_returns_200_when_ready():
    readiness.mark_ready()
    with running_server() as base:
        status, ctype, body = fetch(f"{base}/ready")
    assert status == 200
    assert body == b"ready"
    assert ctype.startswith("text/plain")


def test_ready_endpoint_returns_503_when_drained():
    readiness.mark_not_ready()
    with running_server() as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            fetch(f"{base}/ready")
    assert exc.value.code == 503


def test_health_endpoint_stays_200_even_when_not_ready():
    readiness.mark_not_ready()
    with running_server() as base:
        status, _ctype, body = fetch(f"{base}/health")
    assert status == 200
    assert body == b"ok"


def test_readiness_state_warmup_blocks_then_passes():
    fake_now = [1000.0]

    def fake_clock() -> float:
        return fake_now[0]

    state = ReadinessState(warmup_seconds=5.0, clock=fake_clock)
    assert state.is_ready() is False
    fake_now[0] += 4.99
    assert state.is_ready() is False
    fake_now[0] += 0.02
    assert state.is_ready() is True


def test_readiness_state_drain_overrides_warmup():
    state = ReadinessState(warmup_seconds=0.0)
    assert state.is_ready() is True
    state.mark_not_ready()
    assert state.is_ready() is False
    state.mark_ready()
    assert state.is_ready() is True


def test_readiness_state_rejects_negative_warmup():
    state = ReadinessState(warmup_seconds=-3.0)
    assert state.is_ready() is True
