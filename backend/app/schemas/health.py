"""Response model for GET /api/v1/health."""

from typing import Literal

from pydantic import BaseModel

CheckStatus = Literal["ok", "unavailable"]


class HealthChecks(BaseModel):
    database: CheckStatus
    redis: CheckStatus


class HealthResponse(BaseModel):
    # "ok" only if every check is ok.
    status: CheckStatus
    checks: HealthChecks
    # Which build is running, e.g. "0.1.0". Useful after a deploy to confirm
    # the new version is actually live.
    version: str
