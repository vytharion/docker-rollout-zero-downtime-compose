"""Blue-green rollout planner + driver.

This module owns one small decision: given the currently active color
(blue or green), figure out which color to deploy next and what the
exact ``docker compose`` invocation looks like for that deploy. It
deliberately stops short of cutting traffic over — that lives in a
later step. The contract here is: the new color must come up
*alongside* the running one, never in place of it.

The planning surface is pure (no I/O), so unit tests exercise it
without ever shelling out to docker. The CLI entrypoint wires it to
``subprocess.run`` for real use.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


BLUE = "blue"
GREEN = "green"
COLORS: tuple[str, ...] = (BLUE, GREEN)

DEFAULT_STATE_PATH = Path(
    os.environ.get("ROLLOUT_STATE_PATH", "state/active-color")
)
DEFAULT_COMPOSE_FILE = Path(
    os.environ.get("ROLLOUT_COMPOSE_FILE", "docker-compose.yml")
)
DEFAULT_PROJECT_NAME = os.environ.get("ROLLOUT_PROJECT_NAME", "rollout")


def next_color(current: str) -> str:
    if current == BLUE:
        return GREEN
    if current == GREEN:
        return BLUE
    raise ValueError(f"unknown color: {current!r}")


def service_for_color(color: str) -> str:
    if color == BLUE:
        return "app"
    if color == GREEN:
        return "app_green"
    raise ValueError(f"unknown color: {color!r}")


def read_active_color(state_path: Path = DEFAULT_STATE_PATH) -> str:
    if not state_path.exists():
        return BLUE
    raw = state_path.read_text(encoding="utf-8").strip()
    if raw in COLORS:
        return raw
    return BLUE


def write_active_color(
    color: str,
    state_path: Path = DEFAULT_STATE_PATH,
) -> None:
    if color not in COLORS:
        raise ValueError(f"unknown color: {color!r}")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(color + "\n", encoding="utf-8")


def compose_up_command(
    color: str,
    project_name: str = DEFAULT_PROJECT_NAME,
    compose_file: Path = DEFAULT_COMPOSE_FILE,
) -> tuple[str, ...]:
    """Build the argv that brings up `color` without touching the other.

    ``docker compose up -d --wait`` only starts the named service. The
    profile gate ensures we never accidentally rebuild or recreate the
    currently-serving color when invoking the command for the new one.
    """
    service = service_for_color(color)
    parts: list[str] = [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "-f",
        str(compose_file),
    ]
    # blue is the default-profile service; only green is profile-gated,
    # so only green needs --profile to be visible to the up command.
    if color == GREEN:
        parts.extend(["--profile", GREEN])
    parts.extend(["up", "-d", "--wait", service])
    return tuple(parts)


@dataclass(frozen=True)
class RolloutPlan:
    current_color: str
    next_color: str
    next_service: str
    compose_command: tuple[str, ...]

    def describe(self) -> str:
        return (
            f"current={self.current_color} next={self.next_color} "
            f"service={self.next_service} "
            f"cmd={shlex.join(self.compose_command)}"
        )


def plan_next_rollout(
    state_path: Path = DEFAULT_STATE_PATH,
    project_name: str = DEFAULT_PROJECT_NAME,
    compose_file: Path = DEFAULT_COMPOSE_FILE,
) -> RolloutPlan:
    current = read_active_color(state_path)
    nxt = next_color(current)
    cmd = compose_up_command(
        nxt,
        project_name=project_name,
        compose_file=compose_file,
    )
    return RolloutPlan(
        current_color=current,
        next_color=nxt,
        next_service=service_for_color(nxt),
        compose_command=cmd,
    )


Runner = Callable[[Sequence[str]], int]


def default_runner(cmd: Sequence[str]) -> int:
    completed = subprocess.run(list(cmd), check=False)
    return completed.returncode


def execute_plan(plan: RolloutPlan, runner: Runner = default_runner) -> int:
    return runner(plan.compose_command)


def main(argv: Iterable[str] | None = None) -> int:
    plan = plan_next_rollout()
    print(plan.describe())
    return execute_plan(plan)


if __name__ == "__main__":
    raise SystemExit(main())
