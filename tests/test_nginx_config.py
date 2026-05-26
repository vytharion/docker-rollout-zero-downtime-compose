import re
from pathlib import Path

NGINX_PATH = Path(__file__).resolve().parent.parent / "nginx" / "nginx.conf"


def load_nginx() -> str:
    return NGINX_PATH.read_text(encoding="utf-8")


def test_nginx_conf_exists():
    assert NGINX_PATH.is_file(), f"missing {NGINX_PATH}"


def test_declares_upstream_block_named_app_backend():
    conf = load_nginx()
    assert re.search(r"\bupstream\s+app_backend\s*\{", conf), (
        "nginx.conf must declare an 'upstream app_backend' block"
    )


def test_upstream_points_at_compose_service_app():
    conf = load_nginx()
    block = re.search(r"upstream\s+app_backend\s*\{([^}]*)\}", conf, re.DOTALL)
    assert block is not None
    body = block.group(1)
    assert re.search(r"\bserver\s+app:\d+\s*;", body), (
        "upstream must reference the compose service name 'app' on a port"
    )


def test_root_location_proxies_to_upstream():
    conf = load_nginx()
    assert "proxy_pass http://app_backend" in conf, (
        "a location block must proxy_pass to http://app_backend"
    )


def test_listens_on_port_80():
    conf = load_nginx()
    assert re.search(r"\blisten\s+80\b", conf), "proxy must listen on port 80"
