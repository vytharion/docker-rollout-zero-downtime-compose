"""Blue-green rollout planner + driver.

This module owns the three small decisions a zero-downtime blue-green
deploy has to make:

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

3. After the swap, run a smoke probe against the live endpoint to
   confirm the new replica actually answers real requests, not just
   passes its container healthcheck. Step 5's invariant lives here: if
   the probe never returns the expected status, the script reverses
   the cutover so the service is left pointing at the previously good
   replica instead of the broken new one.

All three planning surfaces are pure (no I/O) so unit tests exercise
them without ever shelling out to docker, touching real nginx, or
opening a socket. The CLI entrypoint wires them to ``subprocess.run``
and ``urllib.request`` for real use.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


BLUE = "blue"
GREEN = "green"
COLORS: tuple[str, ...] = (BLUE, GREEN)

# Reserved for the integration entrypoint: a clean rollback (smoke
# failed, previous color successfully restored) returns this code so an
# operator / CI job can distinguish "deploy aborted safely" from a
# generic runner failure.
ROLLED_BACK_EXIT_CODE = 2

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

DEFAULT_SMOKE_URL = os.environ.get(
    "ROLLOUT_SMOKE_URL", "http://localhost:8080/"
)
DEFAULT_SMOKE_STATUS = int(os.environ.get("ROLLOUT_SMOKE_STATUS", "200"))
DEFAULT_SMOKE_ATTEMPTS = int(os.environ.get("ROLLOUT_SMOKE_ATTEMPTS", "5"))
DEFAULT_SMOKE_TIMEOUT = float(os.environ.get("ROLLOUT_SMOKE_TIMEOUT", "2.0"))
DEFAULT_SMOKE_DELAY = float(os.environ.get("ROLLOUT_SMOKE_DELAY", "1.0"))


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
    # Stored so plan_rollback can rebuild the inverse upstream body
    # without the caller having to remember which port was used.
    port: int = DEFAULT_APP_PORT

    def describe(self) -> str:
        body = self.upstream_body.strip().replace("\n", " | ")
        return (
            f"cutover from={self.from_color} to={self.to_color} "
            f"upstream={self.upstream_path} body={body!r} "
            f"reload={shlex.join(self.reload_command)}"
        )


@dataclass(frozen=True)
class SmokePlan:
    probe_url: str
    expected_status: int
    attempts: int
    timeout_seconds: float
    attempt_delay_seconds: float

    def describe(self) -> str:
        return (
            f"smoke url={self.probe_url} expect={self.expected_status} "
            f"attempts={self.attempts} timeout={self.timeout_seconds}s "
            f"delay={self.attempt_delay_seconds}s"
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
        port=port,
    )


def plan_smoke_test(
    probe_url: str = DEFAULT_SMOKE_URL,
    expected_status: int = DEFAULT_SMOKE_STATUS,
    attempts: int = DEFAULT_SMOKE_ATTEMPTS,
    timeout_seconds: float = DEFAULT_SMOKE_TIMEOUT,
    attempt_delay_seconds: float = DEFAULT_SMOKE_DELAY,
) -> SmokePlan:
    """Build a SmokePlan with conservative lower bounds applied.

    The clamps exist so a misconfigured CLI flag (``--smoke-attempts 0``
    or ``--smoke-timeout -1``) cannot construct a plan that would either
    skip probing entirely or hand a negative timeout to urllib.
    """
    return SmokePlan(
        probe_url=probe_url,
        expected_status=expected_status,
        attempts=max(1, attempts),
        timeout_seconds=max(0.001, timeout_seconds),
        attempt_delay_seconds=max(0.0, attempt_delay_seconds),
    )


def plan_rollback(cutover: CutoverPlan) -> CutoverPlan:
    """Build the cutover that reverses ``cutover``.

    The result swaps the colors and rebuilds the upstream body to point
    back at the original (pre-cutover) color, but keeps the upstream
    path and reload command — rolling back is "do the same swap dance,
    backwards, against the same file and the same proxy".
    """
    return CutoverPlan(
        from_color=cutover.to_color,
        to_color=cutover.from_color,
        upstream_path=cutover.upstream_path,
        upstream_body=upstream_directive_for_color(
            cutover.from_color, port=cutover.port
        ),
        reload_command=cutover.reload_command,
        port=cutover.port,
    )


Runner = Callable[[Sequence[str]], int]
Prober = Callable[[str, float], int]
Sleeper = Callable[[float], None]


def default_runner(cmd: Sequence[str]) -> int:
    completed = subprocess.run(list(cmd), check=False)
    return completed.returncode


def default_prober(url: str, timeout: float) -> int:
    """Best-effort HTTP probe returning the response status, or 0 on failure.

    A status of ``0`` represents "couldn't even ask" — DNS, TCP, TLS, or
    timeout — and is treated by ``run_smoke_test`` exactly the same as a
    non-matching HTTP status. The caller never has to distinguish "5xx"
    from "connection refused"; both mean "the new replica isn't serving
    what we asked for".
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": "rollout-smoke/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return 0


def run_smoke_test(
    plan: SmokePlan,
    prober: Prober = default_prober,
    sleeper: Sleeper = time.sleep,
) -> bool:
    """Probe ``plan.probe_url`` until it returns ``plan.expected_status``.

    Returns True the first time the prober's status matches. Returns
    False if every attempt in ``plan.attempts`` fails. The sleeper is
    only invoked *between* attempts — never after the last one, so a
    rejected deploy doesn't sit and wait before rolling back.
    """
    last_index = plan.attempts - 1
    for attempt in range(plan.attempts):
        status = prober(plan.probe_url, plan.timeout_seconds)
        if status == plan.expected_status:
            return True
        if attempt < last_index:
            sleeper(plan.attempt_delay_seconds)
    return False


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


def execute_rollback(
    cutover: CutoverPlan,
    runner: Runner = default_runner,
    state_path: Path = DEFAULT_STATE_PATH,
) -> int:
    """Undo ``cutover``: rewrite the upstream back to the original color
    and reload nginx, persisting state only if the reload succeeds.

    Implemented as a normal ``execute_cutover`` against the inverse plan
    so every safety property of cutover (write-before-reload ordering,
    no-state-update on reload failure) applies to rollback for free.
    """
    rollback = plan_rollback(cutover)
    return execute_cutover(rollback, runner=runner, state_path=state_path)


def execute_rollout(
    plan: RolloutPlan,
    cutover: CutoverPlan,
    smoke: SmokePlan,
    runner: Runner = default_runner,
    prober: Prober = default_prober,
    sleeper: Sleeper = time.sleep,
    state_path: Path = DEFAULT_STATE_PATH,
) -> int:
    """Run the full blue-green deploy end-to-end.

    Sequence:
        1. ``docker compose up`` the new color alongside the live one.
        2. Atomic cutover at the proxy.
        3. Smoke-probe the live endpoint.
        4. If the probe failed, roll back to the previous color.

    Exit code contract:
        ``0``                          new color is live and serving.
        ``ROLLED_BACK_EXIT_CODE`` (2)  smoke failed; previous color restored cleanly.
        anything else                  a runner returned non-zero; the state
                                       may be partially advanced and an
                                       operator must inspect.
    """
    rc = execute_plan(plan, runner=runner)
    if rc != 0:
        return rc
    rc = execute_cutover(cutover, runner=runner, state_path=state_path)
    if rc != 0:
        return rc
    if run_smoke_test(smoke, prober=prober, sleeper=sleeper):
        return 0
    rb_rc = execute_rollback(cutover, runner=runner, state_path=state_path)
    if rb_rc != 0:
        return rb_rc
    return ROLLED_BACK_EXIT_CODE


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rollout",
        description="Zero-downtime blue-green rollout driver",
    )
    parser.add_argument(
        "--smoke-url",
        default=DEFAULT_SMOKE_URL,
        help="URL to probe after cutover (default: %(default)s).",
    )
    parser.add_argument(
        "--smoke-status",
        type=int,
        default=DEFAULT_SMOKE_STATUS,
        help="HTTP status the probe must return to count as healthy.",
    )
    parser.add_argument(
        "--smoke-attempts",
        type=int,
        default=DEFAULT_SMOKE_ATTEMPTS,
        help="Total probe attempts before declaring failure.",
    )
    parser.add_argument(
        "--smoke-timeout",
        type=float,
        default=DEFAULT_SMOKE_TIMEOUT,
        help="Per-request timeout in seconds.",
    )
    parser.add_argument(
        "--smoke-delay",
        type=float,
        default=DEFAULT_SMOKE_DELAY,
        help="Seconds to wait between failed probe attempts.",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    argv_list = None if argv is None else list(argv)
    args = parse_args(argv_list)
    plan = plan_next_rollout()
    cutover = plan_cutover()
    smoke = plan_smoke_test(
        probe_url=args.smoke_url,
        expected_status=args.smoke_status,
        attempts=args.smoke_attempts,
        timeout_seconds=args.smoke_timeout,
        attempt_delay_seconds=args.smoke_delay,
    )
    print(plan.describe())
    print(cutover.describe())
    print(smoke.describe())
    return execute_rollout(plan, cutover, smoke)


if __name__ == "__main__":
    raise SystemExit(main())
