"""Background work with RQ (SPEC §11).

- `queue.py`  - the queue the API puts jobs on, and how a job is enqueued
- `jobs.py`   - the job functions the worker runs
- `worker.py` - the worker process itself (`python -m app.workers.worker`)
"""
