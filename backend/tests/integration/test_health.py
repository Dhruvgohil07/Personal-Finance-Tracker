"""The health check against the REAL Postgres and Redis from docker-compose."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_health_db
from app.main import create_app

pytestmark = pytest.mark.integration


def test_health_is_green_with_real_services(db: Session) -> None:
    app = create_app()
    # Point the route at the test database (the `db` fixture); Redis is the
    # real one from docker-compose, through the normal get_redis dependency.
    app.dependency_overrides[get_health_db] = lambda: db

    response = TestClient(app).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "redis": "ok"}
