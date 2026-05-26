import re
from pathlib import Path

NGINX_ROOT = Path(__file__).resolve().parent.parent / "nginx"
NGINX_PATH = NGINX_ROOT / "nginx.conf"
UPSTREAM_CONF_PATH = NGINX_ROOT / "conf.d" / "upstream.conf"


def load_nginx() -> str:
    return NGINX_PATH.read_text(encoding="utf-8")


def load_upstream_conf() -> str:
    return UPSTREAM_CONF_PATH.read_text(encoding="utf-8")


def test_nginx_conf_exists():
    assert NGINX_PATH.is_file(), f"missing {NGINX_PATH}"


def test_declares_upstream_block_named_app_backend():
    conf = load_nginx()
    assert re.search(r"\bupstream\s+app_backend\s*\{", conf), (
        "nginx.conf must declare an 'upstream app_backend' block"
    )


def test_upstream_block_includes_external_upstream_snippet():
    """The atomic-cutover seam: the upstream points to its server via an
    `include`, so the rollout script can swap one file and SIGHUP nginx."""
    conf = load_nginx()
    block = re.search(r"upstream\s+app_backend\s*\{([^}]*)\}", conf, re.DOTALL)
    assert block is not None
    body = block.group(1)
    assert re.search(
        r"\binclude\s+/etc/nginx/conf\.d/upstream\.conf\s*;", body
    ), (
        "upstream block must include /etc/nginx/conf.d/upstream.conf so the "
        "rollout script can swap a single file and reload nginx atomically"
    )


def test_root_location_proxies_to_upstream():
    conf = load_nginx()
    assert "proxy_pass http://app_backend" in conf, (
        "a location block must proxy_pass to http://app_backend"
    )


def test_listens_on_port_80():
    conf = load_nginx()
    assert re.search(r"\blisten\s+80\b", conf), "proxy must listen on port 80"


def test_upstream_snippet_file_exists():
    assert UPSTREAM_CONF_PATH.is_file(), (
        f"missing initial upstream snippet at {UPSTREAM_CONF_PATH} — the "
        "stack starts out serving blue, so a baseline 'server app:8000;' "
        "must ship in the repo"
    )


def test_initial_upstream_snippet_points_at_blue():
    body = load_upstream_conf()
    assert re.search(r"\bserver\s+app:\d+\s*;", body), (
        "the baseline upstream snippet must point at the blue service "
        "(named 'app') so a fresh `docker compose up` starts serving "
        "from blue without any cutover work"
    )
    assert "app_green" not in body, (
        "the baseline snippet must NOT reference app_green — that swap "
        "is the rollout script's job, not the repo's default state"
    )


def test_upstream_snippet_has_no_outer_block():
    """The included file is spliced INSIDE an `upstream { ... }` block in
    nginx.conf, so it must not itself open another upstream block."""
    body = load_upstream_conf()
    assert "upstream" not in body, (
        "upstream.conf is included inside an existing `upstream app_backend "
        "{}` block — nesting another `upstream` keyword inside it would be "
        "an nginx syntax error"
    )
