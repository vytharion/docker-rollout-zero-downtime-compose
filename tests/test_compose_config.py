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
