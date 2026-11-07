"""The deployed container's one process: `python -m app.run_all` (ADR 023).

Render's free plan gives one web service, so the API, the RQ worker and
the scheduler share one container (Phase 3 plan, decided with Dhruv).
This launcher is the container's command, and does three things:

1. **Setup, before anything listens.** `alembic upgrade head`, then the
   seed (both are idempotent). Render's own pre-deploy step is not
   available on the free plan; running them here works because there is
   exactly one instance, so no two copies can migrate at once. If either
   fails, the launcher exits and the deploy fails: Render keeps the last
   good version running instead of starting this one against a database
   it doesn't match.
2. **Start the three services** as child processes, each with the same
   command it has in docker-compose.yml (the API with uvicorn on `$PORT`).
3. **Watch them.** If ANY of them exits, the others are stopped and the
   launcher exits with an error. Render then restarts the whole
   container. Without this, a crashed worker would go unnoticed: the API
   would answer the health check while no upload is ever imported.

When Render stops the container (a deploy, a restart, the free plan's
spin-down), it sends SIGTERM to this process only; it is passed on to the
three children, which shut down cleanly.

Only the standard library's `subprocess` is used: no supervisor program to
install and configure for three commands.
"""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from types import FrameType
from typing import Protocol

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging

log = structlog.get_logger()

# The interpreter running this launcher, so the children use the same
# virtualenv (in the image: /app/.venv/bin/python).
PYTHON = sys.executable

SETUP_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("migrations", [PYTHON, "-m", "alembic", "upgrade", "head"]),
    ("seed", [PYTHON, "-m", "app.db.seed"]),
)

# How often the children are checked, and how long they get to stop after
# SIGTERM before they are killed. On SIGTERM the RQ worker finishes the job
# it is running (a "warm shutdown"), and an import can take a minute on the
# free plan's 0.1 CPU, so the grace is generous. It must stay below
# render.yaml's `maxShutdownDelaySeconds` (120), after which Render kills
# the whole container; a test checks the two together (review of PR 40).
POLL_SECONDS = 1.0
STOP_GRACE_SECONDS = 100.0

# Render passes the port to listen on in $PORT (10000 unless changed).
PORT_VARIABLE = "PORT"


class Child(Protocol):
    """What the launcher needs from a running child: `subprocess.Popen`'s API."""

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


def service_commands(port: int) -> dict[str, list[str]]:
    """The three services, by name, with the commands that start them.

    `--host 0.0.0.0`: listen on every interface, so Render's router can
    reach the API (127.0.0.1 would only be reachable inside the container).
    uvicorn's access log is already disabled by the app (app/core/logging.py).
    """
    return {
        "api": [
            PYTHON,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",  # noqa: S104 - a container must listen on all interfaces
            "--port",
            str(port),
        ],
        "worker": [PYTHON, "-m", "app.workers.worker"],
        "scheduler": [PYTHON, "-m", "app.workers.scheduler"],
    }


def listen_port(environ: Mapping[str, str], default: int) -> int:
    """$PORT if Render set it, otherwise the app's API_PORT."""
    value = environ.get(PORT_VARIABLE)
    if value is None:
        return default
    port = int(value)
    if not 0 < port < 65536:
        raise ValueError(f"{PORT_VARIABLE} must be a TCP port, got {port}")
    return port


def run_setup(run: Callable[[list[str]], int]) -> int:
    """Run the setup commands in order; 0, or the exit code of the first failure."""
    for name, command in SETUP_COMMANDS:
        log.info("setup_started", step=name)
        code = run(command)
        if code != 0:
            log.error("setup_failed", step=name, exit_code=code)
            return code
    log.info("setup_finished")
    return 0


class Supervisor:
    """Starts the services and stops all of them as soon as one ends.

    `start` and `sleep` are passed in so the tests can use fake processes
    and a clock that doesn't wait.
    """

    def __init__(
        self,
        commands: Mapping[str, Sequence[str]],
        start: Callable[[Sequence[str]], Child],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._commands = commands
        self._start = start
        self._sleep = sleep
        self._children: dict[str, Child] = {}
        self._stop_requested = False

    def request_stop(self, signum: int, frame: FrameType | None = None) -> None:
        """The SIGTERM/SIGINT handler: finish the loop in `run` and shut down."""
        log.info("stop_requested", signal=signal.Signals(signum).name)
        self._stop_requested = True

    def run(self) -> int:
        """Start every service, then wait. Returns the launcher's exit code.

        0 when the stop was asked for (SIGTERM); otherwise 1, also when a
        service exits with 0: the container exists to run all three, so
        one ending on its own is always a failure Render should restart.
        """
        for name, command in self._commands.items():
            self._children[name] = self._start(command)
            log.info("service_started", service=name)

        exit_code = 0
        while not self._stop_requested:
            ended = self._first_ended()
            if ended is not None:
                name, code = ended
                log.error("service_exited", service=name, exit_code=code)
                exit_code = 1
                break
            self._sleep(POLL_SECONDS)

        self._stop_all()
        return exit_code

    def _first_ended(self) -> tuple[str, int] | None:
        for name, child in self._children.items():
            code = child.poll()
            if code is not None:
                return name, code
        return None

    def _stop_all(self) -> None:
        """SIGTERM to every child still running, then wait; kill what remains."""
        running = {name: child for name, child in self._children.items() if child.poll() is None}
        for child in running.values():
            child.send_signal(signal.SIGTERM)
        for name, child in running.items():
            try:
                child.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning("service_killed", service=name)
                child.kill()
                child.wait()
        log.info("services_stopped", count=len(running))


def _run_command(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode  # noqa: S603 - fixed commands


def _start_command(command: Sequence[str]) -> Child:
    return subprocess.Popen(command)  # noqa: S603 - fixed commands


def _stop_during_setup(signum: int, frame: FrameType | None = None) -> None:
    """SIGTERM/SIGINT while migrating or seeding: give up at once.

    The SystemExit raised here interrupts `subprocess.run`, which kills the
    running alembic or seed child before letting the exception through. A
    half-run migration is rolled back by Postgres (alembic runs it in one
    transaction), and the next start runs it again. 128 + the signal's
    number is the conventional exit code for "ended by a signal".
    """
    log.info("stop_requested_during_setup", signal=signal.Signals(signum).name)
    raise SystemExit(128 + signum)


def main(
    environ: Mapping[str, str] = os.environ,
    run: Callable[[list[str]], int] = _run_command,
    start: Callable[[Sequence[str]], Child] = _start_command,
) -> int:
    """Setup, then supervise. Returns the exit code for `sys.exit`."""
    settings = get_settings()
    configure_logging(settings.env)

    # A handler from the very start: as PID 1 in a container, this process
    # IGNORES any signal it has no handler for, so a stop that arrived
    # during the migrations used to wait for Render's SIGKILL (review of
    # PR 40).
    signal.signal(signal.SIGTERM, _stop_during_setup)
    signal.signal(signal.SIGINT, _stop_during_setup)
    code = run_setup(run)
    if code != 0:
        return code

    supervisor = Supervisor(service_commands(listen_port(environ, settings.api_port)), start)
    # From here a stop means "stop the children cleanly". Installed before
    # they start, so a stop that arrives while they are starting is not lost.
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
