"""Blue-green rollout planner + driver.

This module owns two small decisions:

1. Given the currently active color (blue or green), figure out which
   color to deploy next and what the exact ``docker compose`` invocation
   looks like for that deploy. Step 3's invariant lives here: the new
   color must come up *alongside* the running one, never in place of it.

2. Once the new color is healthy, swap which replica the reverse proxy
   sends traffic to. Step 4's invariant lives here: the moment of
   switching upstreams must be atomic. We do that by rewriting a
   single ``include``-d ``upstream.conf`` snippet and asking nginx to
   ``-s reload`` — SIGHUP triggers a graceful worker swap, so in-flight
   requests finish on the old config while new ones bind to the new one.

Both planning surfaces are pure (no I/O) so unit tests exercise them
without ever shelling out to docker or touching real nginx. The CLI
entrypoint wires them to ``subprocess.run`` for real use.
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
DEFAULT_UPSTREAM_CONF_PATH = Path(
    os.environ.get("ROLLOUT_UPSTREAM_CONF", "nginx/conf.d/upstream.conf")
)
DEFAULT_PROXY_SERVICE = os.environ.get("ROLLOUT_PROXY_SERVICE", "proxy")
DEFAULT_APP_PORT = int(os.environ.get("ROLLOUT_APP_PORT", "8000"))


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


def upstream_directive_for_color(
    color: str,
    port: int = DEFAULT_APP_PORT,
) -> str:
    """Return the body of the included ``upstream.conf`` for ``color``.

    The string is exactly what nginx will splice inside the
    ``upstream app_backend { ... }`` block in the main config. Keeping
    this as a pure function means tests can assert on the directive
    without ever touching the filesystem.
    """
    service = service_for_color(color)
    return f"server {service}:{port};\n"


def render_upstream_conf(
    color: str,
    path: Path = DEFAULT_UPSTREAM_CONF_PATH,
    port: int = DEFAULT_APP_PORT,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        upstream_directive_for_color(color, port=port),
        encoding="utf-8",
    )


def nginx_reload_command(
    project_name: str = DEFAULT_PROJECT_NAME,
    compose_file: Path = DEFAULT_COMPOSE_FILE,
    proxy_service: str = DEFAULT_PROXY_SERVICE,
) -> tuple[str, ...]:
    """The argv that asks the proxy to reload its config in place.

    ``nginx -s reload`` sends SIGHUP to the master process, which forks
    a new worker pool against the freshly-mounted ``upstream.conf`` and
    lets the old pool finish in-flight requests. There is no restart,
    no listen-socket churn, and no dropped TCP connection — that is the
    atomicity guarantee step 4 owes the rest of the series.
    """
    return (
        "docker",
        "compose",
        "--project-name",
        project_name,
        "-f",
        str(compose_file),
        "exec",
        "-T",
        proxy_service,
        "nginx",
        "-s",
        "reload",
    )


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


@dataclass(frozen=True)
class CutoverPlan:
    from_color: str
    to_color: str
    upstream_path: Path
    upstream_body: str
    reload_command: tuple[str, ...]

    def describe(self) -> str:
        body = self.upstream_body.strip().replace("\n", " | ")
        return (
            f"cutover from={self.from_color} to={self.to_color} "
            f"upstream={self.upstream_path} body={body!r} "
            f"reload={shlex.join(self.reload_command)}"
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


def plan_cutover(
    state_path: Path = DEFAULT_STATE_PATH,
    upstream_path: Path = DEFAULT_UPSTREAM_CONF_PATH,
    project_name: str = DEFAULT_PROJECT_NAME,
    compose_file: Path = DEFAULT_COMPOSE_FILE,
    proxy_service: str = DEFAULT_PROXY_SERVICE,
    port: int = DEFAULT_APP_PORT,
) -> CutoverPlan:
    current = read_active_color(state_path)
    target = next_color(current)
    return CutoverPlan(
        from_color=current,
        to_color=target,
        upstream_path=upstream_path,
        upstream_body=upstream_directive_for_color(target, port=port),
        reload_command=nginx_reload_command(
            project_name=project_name,
            compose_file=compose_file,
            proxy_service=proxy_service,
        ),
    )


Runner = Callable[[Sequence[str]], int]


def default_runner(cmd: Sequence[str]) -> int:
    completed = subprocess.run(list(cmd), check=False)
    return completed.returncode


def execute_plan(plan: RolloutPlan, runner: Runner = default_runner) -> int:
    return runner(plan.compose_command)


def execute_cutover(
    plan: CutoverPlan,
    runner: Runner = default_runner,
    state_path: Path = DEFAULT_STATE_PATH,
) -> int:
    """Atomically point the proxy at ``plan.to_color`` and persist it.

    Order matters and is the same order any operator running this by
    hand would use: write the new upstream snippet first (so a racing
    SIGHUP from outside this script would still pick the new value),
    then ask nginx to reload, then commit the new color to disk only
    if the reload reported success. If the reload fails we leave the
    state file untouched so the next ``plan_cutover`` proposes the same
    target and the operator can investigate without losing track of
    which color the proxy is currently pointing at.
    """
    plan.upstream_path.parent.mkdir(parents=True, exist_ok=True)
    plan.upstream_path.write_text(plan.upstream_body, encoding="utf-8")
    rc = runner(plan.reload_command)
    if rc != 0:
        return rc
    write_active_color(plan.to_color, state_path)
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    plan = plan_next_rollout()
    print(plan.describe())
    rc = execute_plan(plan)
    if rc != 0:
        return rc
    cutover = plan_cutover()
    print(cutover.describe())
    return execute_cutover(cutover)


if __name__ == "__main__":
    raise SystemExit(main())
