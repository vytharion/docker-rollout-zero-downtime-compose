from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def load_compose() -> dict:
    with COMPOSE_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_compose_file_exists():
    assert COMPOSE_PATH.is_file(), f"missing {COMPOSE_PATH}"


def test_compose_declares_app_and_proxy_services():
    compose = load_compose()
    services = compose.get("services", {})
    assert "app" in services, "compose must define an 'app' service"
    assert "proxy" in services, "compose must define a 'proxy' service"


def test_app_service_builds_from_local_context():
    compose = load_compose()
    app = compose["services"]["app"]
    build = app.get("build")
    assert build is not None, "app service must have a build section"
    if isinstance(build, dict):
        assert build.get("context") == "./app"
    else:
        assert build == "./app"


def test_proxy_depends_on_app():
    compose = load_compose()
    proxy = compose["services"]["proxy"]
    depends = proxy.get("depends_on")
    assert depends is not None, "proxy must declare depends_on"
    if isinstance(depends, list):
        assert "app" in depends
    else:
        assert "app" in depends.keys()


def test_proxy_publishes_host_port():
    compose = load_compose()
    proxy = compose["services"]["proxy"]
    ports = proxy.get("ports", [])
    assert ports, "proxy must publish at least one port to the host"
    mapped = [str(p) for p in ports]
    assert any(":80" in entry for entry in mapped), (
        "proxy must publish container port 80 to the host"
    )


def test_proxy_mounts_nginx_config_readonly():
    compose = load_compose()
    proxy = compose["services"]["proxy"]
    volumes = proxy.get("volumes", [])
    assert volumes, "proxy must mount its nginx config"
    nginx_mounts = [v for v in volumes if "nginx.conf" in str(v)]
    assert nginx_mounts, "proxy must mount nginx.conf"
    assert any(":ro" in str(v) for v in nginx_mounts), (
        "nginx.conf should be mounted read-only"
    )


def test_app_service_declares_a_healthcheck():
    compose = load_compose()
    app = compose["services"]["app"]
    healthcheck = app.get("healthcheck")
    assert healthcheck is not None, "app service must declare a healthcheck"
    test_cmd = healthcheck.get("test")
    assert test_cmd, "healthcheck must specify a test command"
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    assert "/ready" in joined, (
        "healthcheck should probe the /ready readiness endpoint, not /health"
    )


def test_app_healthcheck_tunes_timing_for_rollouts():
    compose = load_compose()
    healthcheck = compose["services"]["app"]["healthcheck"]
    for key in ("interval", "timeout", "retries", "start_period"):
        assert key in healthcheck, (
            f"healthcheck must set '{key}' so rollouts have predictable timing"
        )


def test_proxy_waits_for_app_to_be_healthy():
    compose = load_compose()
    depends = compose["services"]["proxy"].get("depends_on")
    assert isinstance(depends, dict), (
        "proxy.depends_on must use the long form to gate on app health"
    )
    app_dep = depends.get("app")
    assert isinstance(app_dep, dict), "depends_on.app must be a mapping"
    assert app_dep.get("condition") == "service_healthy", (
        "proxy must wait for app to report service_healthy before starting"
    )


def test_services_share_a_user_defined_network():
    compose = load_compose()
    app_nets = set(compose["services"]["app"].get("networks", []))
    proxy_nets = set(compose["services"]["proxy"].get("networks", []))
    shared = app_nets & proxy_nets
    assert shared, "app and proxy must share at least one user-defined network"
    declared = set(compose.get("networks", {}).keys())
    assert shared <= declared, (
        f"shared networks {shared} must be declared at top level: {declared}"
    )
