"""Tests for app.core.errors: every error has the same safe JSON shape."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    UnauthorizedError,
    register_exception_handlers,
)


class LoginBody(BaseModel):
    email: str
    password: str = Field(min_length=8)


def make_app() -> FastAPI:
    """A tiny app with routes that raise each kind of error.

    We test the handlers on this throwaway app because the real app
    (main.py) is only built in Step 4.
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/uploads/{upload_id}")
    def get_upload(upload_id: int) -> dict:
        raise NotFoundError("Upload not found")

    @app.post("/uploads")
    def create_upload() -> dict:
        raise ConflictError("This statement was already uploaded", details={"upload_id": 7})

    @app.get("/me")
    def me() -> dict:
        raise UnauthorizedError()

    @app.post("/login")
    def login(body: LoginBody) -> dict:
        return {"ok": True}

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("narration UPI/RAHUL SHARMA leaked into an error")

    return app


@pytest.fixture
def client() -> TestClient:
    # By default TestClient re-raises server errors inside the test, so a
    # 500 would crash the test instead of returning a response. Turning that
    # off lets us check the response a real client would get.
    return TestClient(make_app(), raise_server_exceptions=False)


def test_not_found_error(client: TestClient) -> None:
    response = client.get("/uploads/1")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "Upload not found", "details": None}
    }


def test_conflict_error_includes_details(client: TestClient) -> None:
    response = client.post("/uploads")

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "CONFLICT",
        "message": "This statement was already uploaded",
        "details": {"upload_id": 7},
    }


def test_default_message_is_used_when_none_given(client: TestClient) -> None:
    response = client.get("/me")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == UnauthorizedError.message


def test_app_error_base_class_defaults() -> None:
    error = AppError()

    assert error.status_code == 400
    assert error.code == "BAD_REQUEST"
    assert str(error) == AppError.message


def test_validation_error_lists_fields_without_echoing_input(client: TestClient) -> None:
    response = client.post("/login", json={"password": "short1"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    fields = {detail["field"] for detail in error["details"]}
    assert fields == {"body.email", "body.password"}
    # The password the client sent must never come back in the response.
    assert "short1" not in response.text


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_wrong_method_keeps_allow_header(client: TestClient) -> None:
    response = client.delete("/me")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert response.headers["allow"] == "GET"


def test_unexpected_error_returns_generic_500(client: TestClient) -> None:
    response = client.get("/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    # The real exception text stays in our logs, never in the response.
    assert "RAHUL" not in response.text
