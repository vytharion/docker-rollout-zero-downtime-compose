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
    RolloutPlan,
    compose_up_command,
    execute_plan,
    next_color,
    plan_next_rollout,
    read_active_color,
    service_for_color,
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
