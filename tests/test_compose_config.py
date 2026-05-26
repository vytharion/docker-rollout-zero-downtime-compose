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


def test_compose_declares_a_green_app_service():
    compose = load_compose()
    services = compose.get("services", {})
    assert "app_green" in services, (
        "compose must define an 'app_green' service so the rollout script "
        "can bring it up alongside the running blue instance"
    )


def test_green_service_is_gated_by_green_profile():
    compose = load_compose()
    green = compose["services"]["app_green"]
    profiles = green.get("profiles", [])
    assert "green" in profiles, (
        "app_green must sit behind the 'green' compose profile so a plain "
        "`docker compose up` never starts both colors at once"
    )


def test_blue_service_is_not_profile_gated():
    compose = load_compose()
    blue = compose["services"]["app"]
    profiles = blue.get("profiles", [])
    assert not profiles, (
        "the blue service (named 'app') must be the default profile so "
        "the baseline stack still comes up without profile flags"
    )


def test_green_service_shares_blue_build_context():
    compose = load_compose()
    blue_build = compose["services"]["app"].get("build")
    green_build = compose["services"]["app_green"].get("build")
    assert green_build == blue_build, (
        "both colors must build from the same image so a green rollout "
        "ships the same code that has been tested as blue"
    )


def test_green_service_joins_the_shared_edge_network():
    compose = load_compose()
    green_nets = set(compose["services"]["app_green"].get("networks", []))
    proxy_nets = set(compose["services"]["proxy"].get("networks", []))
    assert green_nets & proxy_nets, (
        "app_green must share a network with the proxy so the proxy can "
        "reach the green instance when traffic is cut over"
    )


def test_green_service_declares_its_own_healthcheck():
    compose = load_compose()
    green = compose["services"]["app_green"]
    healthcheck = green.get("healthcheck")
    assert healthcheck is not None, (
        "app_green must declare a healthcheck so `docker compose up --wait` "
        "can block until the new color reports ready"
    )
    test_cmd = healthcheck.get("test")
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    assert "/ready" in joined, (
        "the green healthcheck must probe /ready, the same readiness probe "
        "step 2 wired for blue"
    )


def test_color_labels_are_set_on_both_services():
    compose = load_compose()
    blue_labels = compose["services"]["app"].get("labels", {})
    green_labels = compose["services"]["app_green"].get("labels", {})

    def get_label(labels, key):
        if isinstance(labels, list):
            for entry in labels:
                if entry.startswith(f"{key}="):
                    return entry.split("=", 1)[1]
            return None
        return labels.get(key)

    blue_color = get_label(blue_labels, "com.vytharion.rollout.color")
    green_color = get_label(green_labels, "com.vytharion.rollout.color")
    assert blue_color == "blue"
    assert green_color == "green"


def test_proxy_mounts_upstream_conf_d_directory():
    """Step 4 needs the proxy to see ``upstream.conf`` updates the rollout
    script writes on the host — that means mounting the whole conf.d
    directory, not just the static nginx.conf."""
    compose = load_compose()
    volumes = compose["services"]["proxy"].get("volumes", [])
    conf_d_mounts = [v for v in volumes if "conf.d" in str(v)]
    assert conf_d_mounts, (
        "proxy must mount the nginx/conf.d directory so the rollout script "
        "can swap upstream.conf and have nginx see it immediately"
    )
    target = "/etc/nginx/conf.d"
    assert any(target in str(v) for v in conf_d_mounts), (
        f"proxy's conf.d mount must target {target} inside the container"
    )
