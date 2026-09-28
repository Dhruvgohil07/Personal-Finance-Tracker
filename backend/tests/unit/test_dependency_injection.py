"""How FastAPI's dependency injection behaves (see docs/request-lifecycle.md §2 ④).

These tests use a tiny app of their own, not app.main, so they show the
framework's behaviour on its own, without our middleware or real services.
"""

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

calls: list[int] = []


def get_thing() -> int:
    """A dependency that appends to a list and returns 42."""
    calls.append(42)
    return 42


def get_wrapper(thing: Annotated[int, Depends(get_thing)]) -> int:
    """A dependency that depends on get thing and returns thing+1."""
    return thing + 1


app = FastAPI()


@app.get("/test")
def read_both(
    thing: Annotated[int, Depends(get_thing)],
    wrapper: Annotated[int, Depends(get_wrapper)],
) -> dict[str, int]:
    """A route that depends on both get_thing and get_wrapper."""
    return {"thing": thing, "wrapper": wrapper}


def test_dependency_is_called_once_per_request() -> None:
    """Test that a dependency is called only once per request."""
    calls.clear()  # clears the calls list before running the test
    client = TestClient(app)
    response = client.get("/test")
    assert response.status_code == 200
    assert response.json() == {"thing": 42, "wrapper": 43}
    assert len(calls) == 1

    client.get("/test")
    assert len(calls) == 2
