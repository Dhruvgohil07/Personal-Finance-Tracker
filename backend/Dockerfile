# The image the RQ worker runs in (the `worker` service in docker-compose.yml).
#
# Why a container at all: RQ's worker runs every job in a child process made
# with fork(), which Windows does not have, so the worker always runs on
# Linux (Phase 2 plan, constraint 6). The API itself still runs locally with
# `uv run uvicorn ...`.
#
# Build and start it from the repo root:  docker compose up -d --build worker

# Python 3.12, matching backend/.python-version. "slim" leaves out compilers
# and docs; every dependency we use ships prebuilt wheels, so none are needed.
FROM python:3.12-slim

# uv, copied out of its official image. Pinned to the version that wrote
# uv.lock, so `uv sync --locked` reads the lockfile exactly as it was made.
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /bin/uv

# UV_COMPILE_BYTECODE: compile .pyc files once at build time, not on every start.
# UV_LINK_MODE=copy:  no hard links (they don't work across Docker layers).
# UV_PYTHON_DOWNLOADS=never: use the image's Python 3.12, never fetch another.
# PYTHONUNBUFFERED:   print log lines immediately, so `docker compose logs`
#                     shows them as they happen.
# PATH:               the virtualenv's python comes first.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, the code second. Docker caches each step, and this
# step only reruns when the lockfile changes - not on every code change.
# --locked: fail if uv.lock does not match pyproject.toml (as in CI).
# --no-dev: no pytest/ruff in the image; the tests run outside it.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app

# Don't run as root: if a job were ever exploited (it parses untrusted
# files), the attacker gets an unprivileged user in a throwaway container.
RUN useradd --create-home --uid 10001 kharcha
USER kharcha

CMD ["python", "-m", "app.workers.worker"]
