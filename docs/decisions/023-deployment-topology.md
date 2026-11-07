# ADR 023 — Deployment topology

- Status: accepted
- Date: 2026-11-07 (Phase 3, Step 11)

## Context
Phase 3's Done-when needs the app at public URLs, so that someone other than the
developer can use it (Step 12: a family member, watched). The Phase 3 plan settled
the shape with Dhruv: free tiers only, the frontend on Vercel, the backend in **one**
Render web service, Postgres on Neon. Redis was planned on Upstash, with Render's own
Redis as the fallback.

The backend is three processes: the API (uvicorn), the RQ worker (imports, the
"apply to similar" job, cleanup) and the scheduler (enqueues the hourly cleanup).
Locally the worker and the scheduler run in Docker (RQ needs `fork()`), the API
outside it.

## Decision

**1. One container runs all three: `app/run_all.py`.**
Render's free plan allows one web service, and nothing needs a shared disk:
uploaded files are stored encrypted in Postgres (`upload_blobs`), not on the
filesystem. The launcher, using only the standard library:
- runs `alembic upgrade head` and the seed first. Render's pre-deploy step is for
  paid services only. This is safe because the free plan has exactly one instance,
  so two copies can't migrate at once. If either fails, the launcher exits, the
  deploy fails, and Render keeps the previous version running;
- starts the three as child processes, with the same commands as in
  docker-compose (uvicorn on `0.0.0.0:$PORT`);
- **exits with 1 as soon as any of them exits, even with 0**, after stopping the
  other two (SIGTERM, then SIGKILL after 100 s). Render restarts the container.
  Otherwise a dead worker would leave a healthy-looking API that never imports
  anything;
- passes Render's SIGTERM on to the children and exits 0. As PID 1 in a
  container it must install the handler itself, or the stop would wait for Render's
  SIGKILL. A handler is installed before setup as well: a stop during the migrations
  ends them at once, and Postgres rolls back the half-run migration.
- gives the children 100 s to stop, because on SIGTERM the RQ worker first finishes
  the import it is running, and on 0.1 CPU that can take a minute. Render waits
  `maxShutdownDelaySeconds: 120` (default 30) before killing the container; a test
  reads render.yaml and checks that the grace fits inside it.

The image is the existing `backend/Dockerfile` (now with `alembic/` and
`alembic.ini`). Its default command stays the worker, so docker-compose works as
before. `render.yaml` sets `dockerCommand: python -m app.run_all`.

**2. Redis is Render Key Value (free), not Upstash.**
The worker runs RQ's scheduler (`work(with_scheduler=True)`, needed for retries
with a delay). In RQ 2.12 that loop runs **every second**: a heartbeat pipeline
plus a check for due jobs, about 4–5 commands a second, ~400,000 a day. Upstash's
free tier is 500K commands **a month**, so it would run out in about a day.
Render Key Value has no command quota, and the API reaches it over Render's private
network (`ipAllowList: []`, nothing from outside).

**3. Everything in Singapore**: Render (web service and Key Value), Neon
(`aws-ap-southeast-1`) and Vercel's functions (`sin1`, `frontend/vercel.json`).
Every request goes browser → Vercel → Render → Neon, so the hops between hosts
matter more than the browser's distance. Singapore is the closest region to India
that all three offer.

**4. Neon's direct connection, not its pooler.** psycopg 3 prepares a statement
after it has run it five times, and a transaction-mode pooler (PgBouncer, Neon's
`-pooler` host) can hand the next transaction to another server connection, where
that statement doesn't exist. One container opens a handful of connections, well
inside Neon's direct limits. The URL's scheme has to be `postgresql+psycopg://`
(config.py refuses others).

**5. Cold starts are explained, not avoided** (decided with Dhruv). A free Render
service sleeps after 15 minutes without requests and needs about a minute to wake.
Vercel's proxy waits up to 120 s for the first byte, so the request still succeeds.
When the session check, a login or a sign-up has waited 5 s, the page adds "Kharcha's
server sleeps when nobody is using it and is waking up. This can take up to a
minute." (`WakingUpHint`). The worker and the scheduler sleep with the API. The
scheduler already enqueues a cleanup when it starts, so the cleanup catches up on
every wake. An upload request wakes the container anyway.

**6. Secrets.** Render generates `JWT_ACCESS_SECRET`, `JWT_REFRESH_SECRET` and
`PROXY_SHARED_SECRET` (`generateValue`: 256 random bits in base64, 44 characters,
above the 32-character minimum). Dhruv enters the rest by hand: `DATABASE_URL`,
`FRONTEND_ORIGIN`, and `FILE_ENCRYPTION_KEY`, generated with the documented command.
The key goes in by hand because config.py requires exactly 32 bytes in standard
base64, and Render doesn't document which base64 alphabet it uses. Vercel gets
`API_ORIGIN` and the same `PROXY_SHARED_SECRET`, copied from Render.

**7. Deploys.** Render deploys `main` only after its GitHub checks pass
(`autoDeployTrigger: checksPass`), and only when something under `backend/` changed
(`buildFilter`). Vercel deploys `frontend/` on every push to `main`.

## Alternatives rejected
- **Upstash Redis** (planned): runs out of free commands in about a day (decision 2).
  Turning off RQ's scheduler would cut that traffic, but delayed retries would then
  never run.
- **A separate Render background worker:** paid only.
- **Keeping the service awake with an external pinger** every 10 minutes: one more
  account and almost all of Render's 750 free hours, and above all **Neon's compute
  could never suspend**. While the service is up, Render keeps calling the health
  check, which runs `SELECT 1`. At Neon's minimum of 0.25 CU that is 186 CU-hours a
  month against the free plan's 100, so the database would stop mid-month (review of
  PR 40). The README warns against it.
- **Migrations in Render's pre-deploy command:** paid only.
- **A process manager (supervisord, honcho) for the three processes:** another
  dependency and config file. Its one feature we need, "if one dies, stop all", is
  about 60 lines that the tests cover fully.

## Consequences
- **Memory is the tight resource.** The free plan gives 0.1 CPU and 512 MB to all
  three. Measured locally at idle: uvicorn 127 MB, worker 108 MB, scheduler 55 MB,
  launcher 40 MB, about 330 MB. Each import runs in a forked child (copy-on-write),
  so a large PDF is the risk. To be measured on Render in Step 11's checks.
- **Free Key Value lives in memory only** (25 MB, 50 connections). A restart empties
  it: a queued import is lost, and the hourly cleanup marks that upload failed, so
  the user uploads again. Rate-limit counters reset too.
- **The first request after 15 idle minutes takes about a minute**, with the hint.
- **Every start runs the migrations and the seed**, including each wake from sleep.
  On 0.1 CPU these two short-lived interpreters add some seconds to the cold start.
  Measured in Step 11's checks before anything is changed.
- **0.1 CPU** makes imports slower than locally. Upload polling already follows the
  API's `Retry-After`, so a slower import only means more polls.
- Moving to paid plans later changes `render.yaml`, not the code: a separate worker
  service, the pre-deploy step, persistence for Key Value.
