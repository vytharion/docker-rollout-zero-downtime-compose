"""Tests for the Makefile that packages the rollout flow.

The Makefile is the CI / SSH entry point for the entire deploy. Every
test below uses ``make -n`` (dry-run) so the suite never actually shells
out to docker, never touches a real state file, and never opens a
socket — it just asserts the recipe make WOULD run looks the way step 6
promises it does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"


pytestmark = pytest.mark.skipif(
    shutil.which("make") is None,
    reason="GNU make is not installed in this environment",
)


def _run_make(*args: str, **overrides: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **overrides}
    return subprocess.run(
        ["make", "-n", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Static shape of the Makefile
# ---------------------------------------------------------------------------


def test_makefile_exists():
    assert MAKEFILE.is_file(), "Makefile must live at the codebase root"


def test_help_is_default_goal():
    body = MAKEFILE.read_text(encoding="utf-8")
    assert ".DEFAULT_GOAL := help" in body, (
        "Running bare `make` over SSH should print help, not silently "
        "trigger a deploy"
    )


def test_makefile_declares_phony_targets():
    body = MAKEFILE.read_text(encoding="utf-8")
    phony_lines = [
        line for line in body.splitlines() if line.startswith(".PHONY:")
    ]
    assert phony_lines, ".PHONY declaration missing"
    phony_targets = set()
    for line in phony_lines:
        phony_targets.update(line.split(":", 1)[1].split())
    for target in ("help", "init", "build", "up", "down", "deploy", "test"):
        assert target in phony_targets, (
            f"target {target!r} must be .PHONY so a stray file of that "
            f"name in the workspace cannot short-circuit the recipe"
        )


def test_makefile_anchors_paths_with_abspath_of_makefile_list():
    """Step-6 invariant: paths must resolve from the Makefile's own
    directory, not the caller's $PWD, so `ssh host make -C /opt/svc
    deploy` works from any login shell."""
    body = MAKEFILE.read_text(encoding="utf-8")
    assert "$(abspath" in body
    assert "MAKEFILE_LIST" in body


# ---------------------------------------------------------------------------
# deploy: the CI / SSH entrypoint
# ---------------------------------------------------------------------------


def test_deploy_target_invokes_rollout_module():
    result = _run_make("deploy")
    assert result.returncode == 0, result.stderr
    assert (
        "scripts.rollout" in result.stdout
        or "scripts/rollout.py" in result.stdout
    ), f"deploy must drive the python rollout entrypoint, got:\n{result.stdout}"


def test_deploy_depends_on_init():
    """init has to run before the first deploy: state file + upstream
    snippet must exist or rollout.py has nothing to read."""
    result = _run_make("deploy")
    assert result.returncode == 0, result.stderr
    # init's recipe seeds active-color; it must show up in the dry-run trace
    # of `make -n deploy`.
    assert "active-color" in result.stdout, (
        "deploy did not pull in init — state file would be missing on a "
        f"fresh host. Output was:\n{result.stdout}"
    )


def test_deploy_passes_smoke_url_when_overridden():
    result = _run_make("deploy", SMOKE_URL="http://example.test/healthz")
    assert result.returncode == 0, result.stderr
    assert "--smoke-url" in result.stdout
    assert "http://example.test/healthz" in result.stdout


def test_deploy_omits_smoke_flag_when_override_absent():
    result = _run_make("deploy")
    assert result.returncode == 0, result.stderr
    assert "--smoke-url" not in result.stdout, (
        "an empty SMOKE_URL must not produce a `--smoke-url` flag with no "
        "argument — that would crash argparse"
    )


def test_deploy_forwards_every_smoke_knob_independently():
    result = _run_make(
        "deploy",
        SMOKE_STATUS="204",
        SMOKE_ATTEMPTS="10",
        SMOKE_TIMEOUT="0.5",
        SMOKE_DELAY="0.25",
    )
    assert result.returncode == 0, result.stderr
    for flag, value in (
        ("--smoke-status", "204"),
        ("--smoke-attempts", "10"),
        ("--smoke-timeout", "0.5"),
        ("--smoke-delay", "0.25"),
    ):
        assert flag in result.stdout, (
            f"deploy did not forward {flag} when override was set"
        )
        assert value in result.stdout, (
            f"deploy did not forward {flag}'s value {value!r}"
        )


def test_deploy_propagates_project_name_to_rollout_env():
    """rollout.py reads ROLLOUT_PROJECT_NAME from the env; make must set
    it so the script targets the same compose project the operator named."""
    result = _run_make("deploy", PROJECT_NAME="ci-7")
    assert result.returncode == 0, result.stderr
    assert "ROLLOUT_PROJECT_NAME=ci-7" in result.stdout


def test_deploy_propagates_compose_file_to_rollout_env():
    result = _run_make("deploy", COMPOSE_FILE="/tmp/alt-compose.yml")
    assert result.returncode == 0, result.stderr
    assert "ROLLOUT_COMPOSE_FILE=/tmp/alt-compose.yml" in result.stdout


def test_deploy_uses_overridable_python_interpreter():
    """The host may not have `python3` on PATH (e.g. SSH'd as a deploy user
    where only `/opt/python/bin/python3.11` exists). PYTHON= must win."""
    result = _run_make("deploy", PYTHON="/opt/python/bin/python3.11")
    assert result.returncode == 0, result.stderr
    assert "/opt/python/bin/python3.11" in result.stdout
    assert "-m scripts.rollout" in result.stdout


def test_deploy_does_not_invoke_destructive_compose_commands():
    """A deploy that calls down/stop/kill/rm/restart would drop traffic —
    the whole point of steps 3 through 5 is to avoid that. Pin the
    recipe so a future refactor can't reintroduce it."""
    result = _run_make("deploy")
    assert result.returncode == 0, result.stderr
    for destructive in ("compose down", "compose stop", "compose kill",
                        "compose rm", "compose restart"):
        assert destructive not in result.stdout, (
            f"deploy recipe must not run '{destructive}':\n{result.stdout}"
        )


def test_deploy_runs_with_non_interactive_shell():
    """SSH and CI runners have no TTY. The Makefile must not invoke
    docker compose `exec` without `-T` (that flag lives in rollout.py's
    nginx_reload_command); at the Makefile level, just make sure no
    target asks for a TTY via `-it`/`-i`/`exec` directly."""
    body = MAKEFILE.read_text(encoding="utf-8")
    assert " -it " not in body
    assert "\t-it " not in body
    # exec without -T would also fail in CI; only rollout.py issues exec,
    # the Makefile itself must not.
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        assert "compose exec" not in stripped, (
            f"Makefile must not call `compose exec` directly (rollout.py "
            f"owns the only exec, and it uses -T): {stripped!r}"
        )


# ---------------------------------------------------------------------------
# Other targets — small sanity pins
# ---------------------------------------------------------------------------


def test_up_target_brings_blue_alongside_proxy_only():
    """`make up` is the BOOTSTRAP, not the deploy. It must bring up the
    blue replica + proxy. Green is added later by `make deploy`."""
    result = _run_make("up")
    assert result.returncode == 0, result.stderr
    assert "app_green" not in result.stdout, (
        "`make up` must not name app_green — green is a deploy-time "
        f"replica, not part of the bootstrap. Got:\n{result.stdout}"
    )
    assert "app" in result.stdout
    assert "proxy" in result.stdout


def test_up_depends_on_init():
    """Up needs the seed upstream.conf + state file too, otherwise nginx
    boots against an empty include and 502s every request."""
    result = _run_make("up")
    assert result.returncode == 0, result.stderr
    assert "active-color" in result.stdout


def test_test_target_runs_pytest():
    result = _run_make("test")
    assert result.returncode == 0, result.stderr
    assert "pytest" in result.stdout


def test_clean_target_removes_state_only_not_containers():
    """`make clean` is for resetting the on-host bookkeeping. It must
    NEVER invoke docker — that would surprise an operator who only
    wanted to wipe the active-color file."""
    result = _run_make("clean")
    assert result.returncode == 0, result.stderr
    # Check for an actual docker command invocation, not the substring
    # "docker" that may appear inside an absolute path.
    invocations = [
        line.split() and line.split()[0]
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    assert "docker" not in invocations, (
        f"clean must not run docker — saw invocations {invocations!r}"
    )
    assert "rm" in invocations


def test_help_target_prints_usage_banner():
    result = _run_make("help")
    assert result.returncode == 0, result.stderr
    # `make -n` prints the recipe even with the `@` prefix, so the awk
    # command itself shows up — including the usage banner text.
    assert "make <target>" in result.stdout
