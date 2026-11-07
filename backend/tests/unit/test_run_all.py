"""Tests for the container launcher (app/run_all.py, ADR 023).

No real services are started: the launcher takes the functions that run
and start commands as parameters, and these tests pass fakes that record
what was asked and play a script ("this child exits on the third check").
Two tests at the end run real, tiny subprocesses to cover the two
one-line wrappers around `subprocess`.
"""

import re
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from app import run_all
from app.run_all import (
    POLL_SECONDS,
    SETUP_COMMANDS,
    STOP_GRACE_SECONDS,
    Supervisor,
    listen_port,
    run_setup,
    service_commands,
)


class FakeChild:
    """A child process whose `poll()` answers None until `exits_after` checks.

    `stubborn`: ignores SIGTERM, so `wait(timeout=...)` times out.
    """

    def __init__(
        self, *, exits_after: int | None = None, code: int = 1, stubborn: bool = False
    ) -> None:
        self.exits_after = exits_after
        self.code = code
        self.stubborn = stubborn
        self.polls = 0
        self.signals: list[int] = []
        self.killed = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None and self.exits_after is not None:
            self.polls += 1
            if self.polls >= self.exits_after:
                self.returncode = self.code
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if not self.stubborn:
            self.returncode = -sig

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -signal.SIGKILL if hasattr(signal, "SIGKILL") else -9


COMMANDS = {"api": ["api"], "worker": ["worker"], "scheduler": ["scheduler"]}


def starter(
    children: dict[str, FakeChild],
) -> tuple[Callable[[Sequence[str]], FakeChild], list[Sequence[str]]]:
    """A `start` function that hands out `children` by the command's first word."""
    started: list[Sequence[str]] = []

    def start(command: Sequence[str]) -> FakeChild:
        started.append(command)
        return children[command[0]]

    return start, started


# --- the commands -------------------------------------------------------------


def test_the_api_listens_on_every_interface_at_the_given_port() -> None:
    commands = service_commands(10000)

    assert commands["api"][1:] == [
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",  # noqa: S104 - asserting the command, not binding
        "--port",
        "10000",
    ]
    assert commands["worker"][1:] == ["-m", "app.workers.worker"]
    assert commands["scheduler"][1:] == ["-m", "app.workers.scheduler"]


def test_every_command_runs_with_this_interpreter() -> None:
    """The children must use the image's virtualenv, not some other python."""
    commands = [command for _, command in SETUP_COMMANDS] + list(service_commands(1).values())

    assert all(command[0] == sys.executable for command in commands)


def test_setup_migrates_first_then_seeds() -> None:
    assert [name for name, _ in SETUP_COMMANDS] == ["migrations", "seed"]
    assert SETUP_COMMANDS[0][1][1:] == ["-m", "alembic", "upgrade", "head"]


# --- the port -----------------------------------------------------------------


def test_the_port_comes_from_render() -> None:
    assert listen_port({"PORT": "10000"}, default=8000) == 10000


def test_without_port_the_apps_own_port_is_used() -> None:
    assert listen_port({}, default=8000) == 8000


@pytest.mark.parametrize("value", ["0", "65536", "-1"])
def test_a_port_out_of_range_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="PORT must be a TCP port"):
        listen_port({"PORT": value}, default=8000)


def test_a_port_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValueError):
        listen_port({"PORT": "http"}, default=8000)


# --- setup --------------------------------------------------------------------


def test_setup_runs_every_step_in_order() -> None:
    ran: list[list[str]] = []

    assert run_setup(lambda command: ran.append(command) or 0) == 0
    assert ran == [command for _, command in SETUP_COMMANDS]


def test_a_failed_migration_stops_setup_before_the_seed() -> None:
    ran: list[list[str]] = []

    def run(command: list[str]) -> int:
        ran.append(command)
        return 2

    assert run_setup(run) == 2
    assert ran == [SETUP_COMMANDS[0][1]]


# --- supervising ----------------------------------------------------------------


def test_one_service_exiting_stops_the_others_and_fails_the_container() -> None:
    children = {
        "api": FakeChild(),
        "worker": FakeChild(exits_after=3, code=1),  # crashes on the third check
        "scheduler": FakeChild(),
    }
    start, started = starter(children)
    sleeps: list[float] = []

    code = Supervisor(COMMANDS, start, sleep=sleeps.append).run()

    assert code == 1
    assert started == list(COMMANDS.values())
    assert children["api"].signals == [signal.SIGTERM]
    assert children["scheduler"].signals == [signal.SIGTERM]
    assert children["worker"].signals == []  # already gone
    assert sleeps == [POLL_SECONDS, POLL_SECONDS]


def test_a_service_that_exits_cleanly_still_fails_the_container() -> None:
    """All three are meant to run forever, so any exit is a restart."""
    children = {
        "api": FakeChild(exits_after=1, code=0),
        "worker": FakeChild(),
        "scheduler": FakeChild(),
    }
    start, _ = starter(children)

    assert Supervisor(COMMANDS, start, sleep=lambda _: None).run() == 1


def test_a_requested_stop_stops_every_service_and_succeeds() -> None:
    children = {"api": FakeChild(), "worker": FakeChild(), "scheduler": FakeChild()}
    start, _ = starter(children)
    supervisor = Supervisor(
        COMMANDS, start, sleep=lambda _: supervisor.request_stop(signal.SIGTERM)
    )

    assert supervisor.run() == 0
    assert all(child.signals == [signal.SIGTERM] for child in children.values())


def test_a_service_that_ignores_sigterm_is_killed() -> None:
    children = {"api": FakeChild(stubborn=True), "worker": FakeChild(), "scheduler": FakeChild()}
    start, _ = starter(children)
    supervisor = Supervisor(COMMANDS, start, sleep=lambda _: supervisor.request_stop(signal.SIGINT))

    supervisor.run()

    assert children["api"].killed is True
    assert children["worker"].killed is False


def test_the_stop_grace_fits_inside_renders_shutdown_delay() -> None:
    """Render SIGKILLs the container after `maxShutdownDelaySeconds`.

    If the launcher's grace were longer, a worker finishing its import would
    be killed by Render instead of being waited for. Read from render.yaml
    itself, so changing one without the other fails here.
    """
    render_yaml = (Path(__file__).resolve().parents[3] / "render.yaml").read_text(encoding="utf-8")
    match = re.search(r"^\s*maxShutdownDelaySeconds:\s*(\d+)", render_yaml, re.MULTILINE)
    assert match is not None

    assert STOP_GRACE_SECONDS + 10 <= int(match.group(1))  # 10 s to spare for the exit itself


# --- main -----------------------------------------------------------------------


@pytest.fixture
def quiet_main(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, object]]:
    """main() without reconfiguring logging or touching the real signal handlers."""
    installed: list[tuple[int, object]] = []
    monkeypatch.setattr(run_all, "configure_logging", lambda env: None)
    monkeypatch.setattr(
        run_all.signal, "signal", lambda sig, handler: installed.append((sig, handler))
    )
    return installed


def test_main_starts_nothing_when_setup_fails(quiet_main: list[tuple[int, object]]) -> None:
    started: list[Sequence[str]] = []

    code = run_all.main(environ={}, run=lambda command: 1, start=started.append)

    assert code == 1
    assert started == []
    # Only the setup-phase handlers were installed: nothing to supervise.
    assert [handler for _, handler in quiet_main] == [run_all._stop_during_setup] * 2


def test_a_stop_during_setup_is_handled_from_the_start(
    quiet_main: list[tuple[int, object]],
) -> None:
    """PID 1 ignores a signal without a handler, so one must exist before setup."""
    handlers_while_migrating: list[object] = []

    def run(command: list[str]) -> int:
        handlers_while_migrating.extend(handler for _, handler in quiet_main)
        return 1

    run_all.main(environ={}, run=run, start=lambda command: FakeChild())

    assert handlers_while_migrating == [run_all._stop_during_setup] * 2
    assert [sig for sig, _ in quiet_main] == [signal.SIGTERM, signal.SIGINT]


def test_the_setup_handler_ends_the_launcher_with_the_signals_code() -> None:
    """SystemExit interrupts subprocess.run, which then kills the child."""
    with pytest.raises(SystemExit) as stopped:
        run_all._stop_during_setup(signal.SIGTERM)

    assert stopped.value.code == 128 + signal.SIGTERM


def test_main_sets_up_then_supervises_on_renders_port(quiet_main: list[tuple[int, object]]) -> None:
    started: list[Sequence[str]] = []

    def start(command: Sequence[str]) -> FakeChild:
        started.append(command)
        return FakeChild(exits_after=1)  # every service "crashes" at once: the loop ends

    code = run_all.main(environ={"PORT": "10000"}, run=lambda command: 0, start=start)

    assert code == 1
    assert started[0][-2:] == ["--port", "10000"]
    # The setup handlers first, then the supervisor's.
    assert [sig for sig, _ in quiet_main] == [signal.SIGTERM, signal.SIGINT] * 2
    assert all(handler != run_all._stop_during_setup for _, handler in quiet_main[2:])


# --- the real subprocess wrappers ---------------------------------------------------


def test_run_command_returns_the_exit_code() -> None:
    assert run_all._run_command([sys.executable, "-c", "raise SystemExit(3)"]) == 3


def test_start_command_starts_a_real_process() -> None:
    child = run_all._start_command([sys.executable, "-c", "pass"])

    assert child.wait(timeout=30) == 0
