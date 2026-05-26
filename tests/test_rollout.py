"""Unit tests for the blue-green rollout planner.

Every test below operates on pure planning functions. No test in this
module shells out to docker, mutates a real state file, or relies on a
running container — the planner exposes a `Runner` seam that fakes
inject into.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rollout import (
    BLUE,
    COLORS,
    GREEN,
    CutoverPlan,
    RolloutPlan,
    compose_up_command,
    execute_cutover,
    execute_plan,
    next_color,
    nginx_reload_command,
    plan_cutover,
    plan_next_rollout,
    read_active_color,
    render_upstream_conf,
    service_for_color,
    upstream_directive_for_color,
    write_active_color,
)


def test_colors_constant_is_blue_and_green():
    assert set(COLORS) == {BLUE, GREEN}


def test_next_color_flips_blue_to_green():
    assert next_color(BLUE) == GREEN


def test_next_color_flips_green_to_blue():
    assert next_color(GREEN) == BLUE


def test_next_color_rejects_unknown_color():
    with pytest.raises(ValueError):
        next_color("purple")


def test_service_for_color_maps_blue_to_app():
    assert service_for_color(BLUE) == "app"


def test_service_for_color_maps_green_to_app_green():
    assert service_for_color(GREEN) == "app_green"


def test_service_for_color_rejects_unknown_color():
    with pytest.raises(ValueError):
        service_for_color("teal")


def test_read_active_color_defaults_to_blue_when_state_missing(tmp_path: Path):
    state = tmp_path / "state" / "active-color"
    assert read_active_color(state) == BLUE


def test_read_active_color_round_trips_green(tmp_path: Path):
    state = tmp_path / "state" / "active-color"
    write_active_color(GREEN, state)
    assert read_active_color(state) == GREEN


def test_read_active_color_round_trips_blue(tmp_path: Path):
    state = tmp_path / "state" / "active-color"
    write_active_color(BLUE, state)
    assert read_active_color(state) == BLUE


def test_read_active_color_falls_back_to_blue_on_garbage(tmp_path: Path):
    state = tmp_path / "active-color"
    state.write_text("rainbow\n", encoding="utf-8")
    assert read_active_color(state) == BLUE


def test_read_active_color_strips_whitespace(tmp_path: Path):
    state = tmp_path / "active-color"
    state.write_text("  green  \n", encoding="utf-8")
    assert read_active_color(state) == GREEN


def test_write_active_color_rejects_unknown_color(tmp_path: Path):
    state = tmp_path / "active-color"
    with pytest.raises(ValueError):
        write_active_color("teal", state)


def test_write_active_color_creates_parent_directory(tmp_path: Path):
    state = tmp_path / "deeply" / "nested" / "active-color"
    write_active_color(BLUE, state)
    assert state.is_file()


def test_compose_up_command_starts_with_docker_compose():
    cmd = compose_up_command(BLUE)
    assert cmd[0] == "docker"
    assert cmd[1] == "compose"


def test_compose_up_command_targets_blue_service():
    cmd = compose_up_command(BLUE)
    assert cmd[-1] == "app"
    assert "app_green" not in cmd


def test_compose_up_command_targets_green_service():
    cmd = compose_up_command(GREEN)
    assert cmd[-1] == "app_green"


def test_compose_up_command_for_green_includes_green_profile():
    cmd = compose_up_command(GREEN)
    assert "--profile" in cmd
    idx = cmd.index("--profile")
    assert cmd[idx + 1] == GREEN


def test_compose_up_command_for_blue_does_not_use_profile_flag():
    cmd = compose_up_command(BLUE)
    assert "--profile" not in cmd


def test_compose_up_command_uses_explicit_project_name():
    cmd = compose_up_command(GREEN, project_name="rollout-demo")
    assert "--project-name" in cmd
    idx = cmd.index("--project-name")
    assert cmd[idx + 1] == "rollout-demo"


def test_compose_up_command_uses_explicit_compose_file():
    cmd = compose_up_command(BLUE, compose_file=Path("/tmp/my-compose.yml"))
    assert "/tmp/my-compose.yml" in cmd


def test_compose_up_command_runs_detached_and_waits_for_health():
    cmd = compose_up_command(GREEN)
    assert "-d" in cmd
    assert "--wait" in cmd


def test_compose_up_command_does_not_stop_or_recreate_other_color():
    """Step-3 invariant: green comes up *alongside* blue — no down/stop/rm."""
    for color in COLORS:
        cmd = compose_up_command(color)
        for destructive in ("down", "stop", "rm", "kill", "restart"):
            assert destructive not in cmd, (
                f"compose_up_command({color!r}) must not include {destructive!r}: {cmd}"
            )


def test_plan_next_rollout_promotes_green_from_fresh_state(tmp_path: Path):
    state = tmp_path / "active-color"
    plan = plan_next_rollout(
        state_path=state,
        project_name="p",
        compose_file=Path("docker-compose.yml"),
    )
    assert plan.current_color == BLUE
    assert plan.next_color == GREEN
    assert plan.next_service == "app_green"
    assert "app_green" in plan.compose_command


def test_plan_next_rollout_promotes_blue_when_green_is_active(tmp_path: Path):
    state = tmp_path / "active-color"
    write_active_color(GREEN, state)
    plan = plan_next_rollout(
        state_path=state,
        project_name="p",
        compose_file=Path("docker-compose.yml"),
    )
    assert plan.current_color == GREEN
    assert plan.next_color == BLUE
    assert plan.next_service == "app"


def test_plan_does_not_disturb_currently_active_color(tmp_path: Path):
    state = tmp_path / "active-color"
    write_active_color(BLUE, state)
    plan = plan_next_rollout(state_path=state)
    # The plan should bring up the green service *alongside* blue.
    assert plan.next_service == "app_green"
    assert plan.compose_command[-1] == "app_green"
    for destructive in ("down", "stop", "rm", "kill", "restart"):
        assert destructive not in plan.compose_command


def test_plan_describe_renders_human_readable_summary(tmp_path: Path):
    state = tmp_path / "active-color"
    write_active_color(BLUE, state)
    plan = plan_next_rollout(state_path=state, project_name="p")
    line = plan.describe()
    assert "current=blue" in line
    assert "next=green" in line
    assert "service=app_green" in line
    assert "docker compose" in line


def test_execute_plan_invokes_runner_with_the_planned_command(tmp_path: Path):
    state = tmp_path / "active-color"
    plan = plan_next_rollout(state_path=state)
    captured: list[tuple[str, ...]] = []

    def fake_runner(cmd):
        captured.append(tuple(cmd))
        return 0

    rc = execute_plan(plan, runner=fake_runner)
    assert rc == 0
    assert captured == [plan.compose_command]


def test_execute_plan_propagates_runner_exit_code(tmp_path: Path):
    state = tmp_path / "active-color"
    plan = plan_next_rollout(state_path=state)

    def fake_runner(_cmd):
        return 7

    assert execute_plan(plan, runner=fake_runner) == 7


def test_rollout_plan_is_immutable():
    plan = RolloutPlan(
        current_color=BLUE,
        next_color=GREEN,
        next_service="app_green",
        compose_command=("docker", "compose", "up"),
    )
    with pytest.raises(Exception):
        plan.next_color = "blue"  # type: ignore[misc]


def test_upstream_directive_for_blue_points_at_app_service():
    body = upstream_directive_for_color(BLUE)
    assert "server app:8000;" in body
    assert "app_green" not in body


def test_upstream_directive_for_green_points_at_app_green_service():
    body = upstream_directive_for_color(GREEN)
    assert "server app_green:8000;" in body


def test_upstream_directive_respects_custom_port():
    body = upstream_directive_for_color(BLUE, port=9090)
    assert "server app:9090;" in body


def test_upstream_directive_is_a_single_server_line():
    """upstream.conf is included inside an existing `upstream {}` block,
    so the rendered body must not declare its own upstream/server blocks."""
    for color in COLORS:
        body = upstream_directive_for_color(color)
        assert "upstream" not in body
        assert body.count("server ") == 1
        assert body.rstrip().endswith(";")


def test_upstream_directive_rejects_unknown_color():
    with pytest.raises(ValueError):
        upstream_directive_for_color("indigo")


def test_render_upstream_conf_writes_file(tmp_path: Path):
    path = tmp_path / "conf.d" / "upstream.conf"
    render_upstream_conf(GREEN, path=path)
    assert path.read_text(encoding="utf-8") == "server app_green:8000;\n"


def test_render_upstream_conf_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "deeply" / "nested" / "upstream.conf"
    render_upstream_conf(BLUE, path=path)
    assert path.is_file()


def test_render_upstream_conf_overwrites_existing_content(tmp_path: Path):
    path = tmp_path / "upstream.conf"
    render_upstream_conf(BLUE, path=path)
    render_upstream_conf(GREEN, path=path)
    body = path.read_text(encoding="utf-8")
    assert "app_green" in body
    assert body.count("server ") == 1, (
        "rendering must REPLACE the file, not append — otherwise the next "
        "cutover would leave both server directives in nginx's upstream"
    )


def test_nginx_reload_command_is_an_exec_into_proxy():
    cmd = nginx_reload_command()
    assert cmd[0] == "docker"
    assert cmd[1] == "compose"
    assert "exec" in cmd
    idx = cmd.index("exec")
    # -T disables TTY allocation so the command works in CI / non-interactive shells.
    assert cmd[idx + 1] == "-T"
    assert cmd[idx + 2] == "proxy"


def test_nginx_reload_command_uses_signal_reload_not_restart():
    """Step-4 invariant: traffic swap is graceful (SIGHUP), never a restart."""
    cmd = nginx_reload_command()
    assert "-s" in cmd
    idx = cmd.index("-s")
    assert cmd[idx + 1] == "reload"
    for destructive in ("restart", "stop", "kill", "down", "rm"):
        assert destructive not in cmd, (
            f"nginx reload command must not include {destructive!r}: {cmd}"
        )


def test_nginx_reload_command_respects_project_name_and_compose_file():
    cmd = nginx_reload_command(
        project_name="custom",
        compose_file=Path("/tmp/alt-compose.yml"),
    )
    assert "--project-name" in cmd
    assert cmd[cmd.index("--project-name") + 1] == "custom"
    assert "/tmp/alt-compose.yml" in cmd


def test_nginx_reload_command_respects_proxy_service_name():
    cmd = nginx_reload_command(proxy_service="edge_proxy")
    assert "edge_proxy" in cmd
    assert "proxy" not in [
        cmd[i] for i in range(len(cmd)) if i != cmd.index("edge_proxy")
    ] or list(cmd).count("proxy") == 0


def test_plan_cutover_targets_green_when_blue_is_active(tmp_path: Path):
    state = tmp_path / "active-color"
    write_active_color(BLUE, state)
    plan = plan_cutover(state_path=state, upstream_path=tmp_path / "u.conf")
    assert plan.from_color == BLUE
    assert plan.to_color == GREEN
    assert "server app_green:8000;" in plan.upstream_body


def test_plan_cutover_targets_blue_when_green_is_active(tmp_path: Path):
    state = tmp_path / "active-color"
    write_active_color(GREEN, state)
    plan = plan_cutover(state_path=state, upstream_path=tmp_path / "u.conf")
    assert plan.from_color == GREEN
    assert plan.to_color == BLUE
    assert "server app:8000;" in plan.upstream_body
    assert "app_green" not in plan.upstream_body


def test_plan_cutover_includes_reload_command(tmp_path: Path):
    state = tmp_path / "active-color"
    plan = plan_cutover(state_path=state, upstream_path=tmp_path / "u.conf")
    assert "-s" in plan.reload_command
    assert "reload" in plan.reload_command


def test_plan_cutover_does_not_touch_filesystem(tmp_path: Path):
    """Planning is pure — the upstream file must not appear until execute."""
    state = tmp_path / "active-color"
    upstream = tmp_path / "u.conf"
    plan_cutover(state_path=state, upstream_path=upstream)
    assert not upstream.exists(), (
        "plan_cutover must not write the upstream file — that side effect "
        "belongs in execute_cutover so a caller can inspect the plan first"
    )


def test_cutover_plan_describe_renders_human_readable_summary(tmp_path: Path):
    state = tmp_path / "active-color"
    write_active_color(BLUE, state)
    plan = plan_cutover(state_path=state, upstream_path=tmp_path / "u.conf")
    line = plan.describe()
    assert "from=blue" in line
    assert "to=green" in line
    assert "app_green:8000" in line
    assert "nginx" in line
    assert "reload" in line


def test_cutover_plan_is_immutable():
    plan = CutoverPlan(
        from_color=BLUE,
        to_color=GREEN,
        upstream_path=Path("/tmp/u.conf"),
        upstream_body="server app_green:8000;\n",
        reload_command=("docker", "compose", "exec", "proxy", "nginx", "-s", "reload"),
    )
    with pytest.raises(Exception):
        plan.to_color = BLUE  # type: ignore[misc]


def test_execute_cutover_writes_upstream_then_reloads_then_persists_state(
    tmp_path: Path,
):
    state = tmp_path / "active-color"
    write_active_color(BLUE, state)
    upstream = tmp_path / "upstream.conf"
    plan = plan_cutover(state_path=state, upstream_path=upstream)

    events: list[tuple[str, object]] = []

    def fake_runner(cmd):
        events.append(("reload", tuple(cmd)))
        # By the time the runner fires, the upstream file must already
        # be in place — otherwise an out-of-band SIGHUP would race us.
        events.append(("upstream_on_disk", upstream.read_text(encoding="utf-8")))
        return 0

    rc = execute_cutover(plan, runner=fake_runner, state_path=state)
    assert rc == 0
    assert upstream.read_text(encoding="utf-8") == "server app_green:8000;\n"
    assert read_active_color(state) == GREEN
    # The state file is committed AFTER the reload returns success.
    kinds = [kind for kind, _ in events]
    assert kinds == ["reload", "upstream_on_disk"], (
        f"unexpected runner-side event order: {events}"
    )
    _, body_seen_by_nginx = events[1]
    assert "app_green:8000" in body_seen_by_nginx


def test_execute_cutover_leaves_state_unchanged_when_reload_fails(tmp_path: Path):
    state = tmp_path / "active-color"
    write_active_color(BLUE, state)
    upstream = tmp_path / "upstream.conf"
    plan = plan_cutover(state_path=state, upstream_path=upstream)

    def failing_runner(_cmd):
        return 1

    rc = execute_cutover(plan, runner=failing_runner, state_path=state)
    assert rc == 1
    # Active-color must not advance when the proxy refused to reload —
    # otherwise the next rollout would target blue, leaving green orphaned.
    assert read_active_color(state) == BLUE


def test_execute_cutover_propagates_arbitrary_runner_exit_code(tmp_path: Path):
    state = tmp_path / "active-color"
    plan = plan_cutover(state_path=state, upstream_path=tmp_path / "u.conf")

    def fake_runner(_cmd):
        return 42

    assert execute_cutover(plan, runner=fake_runner, state_path=state) == 42


def test_execute_cutover_creates_upstream_parent_directory(tmp_path: Path):
    state = tmp_path / "active-color"
    upstream = tmp_path / "fresh" / "conf.d" / "upstream.conf"
    plan = plan_cutover(state_path=state, upstream_path=upstream)
    execute_cutover(plan, runner=lambda _cmd: 0, state_path=state)
    assert upstream.is_file()


def test_round_trip_two_cutovers_swap_color_twice(tmp_path: Path):
    """A blue→green→blue cycle must leave the proxy pointing back at blue."""
    state = tmp_path / "active-color"
    upstream = tmp_path / "upstream.conf"
    runner = lambda _cmd: 0  # noqa: E731

    plan1 = plan_cutover(state_path=state, upstream_path=upstream)
    execute_cutover(plan1, runner=runner, state_path=state)
    assert "app_green" in upstream.read_text(encoding="utf-8")
    assert read_active_color(state) == GREEN

    plan2 = plan_cutover(state_path=state, upstream_path=upstream)
    execute_cutover(plan2, runner=runner, state_path=state)
    body = upstream.read_text(encoding="utf-8")
    assert "server app:8000;" in body
    assert "app_green" not in body
    assert read_active_color(state) == BLUE
