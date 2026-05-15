from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import APIKeyAuthMiddleware


def _build_app(api_key: str | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(APIKeyAuthMiddleware, api_key=api_key)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    @app.post("/mutate")
    def mutate() -> dict:
        return {"mutated": True}

    @app.delete("/mutate")
    def delete_mutate() -> dict:
        return {"deleted": True}

    return app


def test_get_request_is_never_blocked_even_with_api_key_set() -> None:
    client = TestClient(_build_app(api_key="secret"))
    response = client.get("/ping")
    assert response.status_code == 200


def test_mutating_request_without_header_is_rejected() -> None:
    client = TestClient(_build_app(api_key="secret"))
    response = client.post("/mutate")
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid or missing api key"


def test_mutating_request_with_wrong_header_is_rejected() -> None:
    client = TestClient(_build_app(api_key="secret"))
    response = client.post("/mutate", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_mutating_request_with_correct_header_passes() -> None:
    client = TestClient(_build_app(api_key="secret"))
    response = client.post("/mutate", headers={"X-API-Key": "secret"})
    assert response.status_code == 200
    assert response.json() == {"mutated": True}


def test_delete_with_correct_header_passes() -> None:
    client = TestClient(_build_app(api_key="secret"))
    response = client.delete("/mutate", headers={"X-API-Key": "secret"})
    assert response.status_code == 200


def test_middleware_noop_when_api_key_is_none() -> None:
    client = TestClient(_build_app(api_key=None))
    response = client.post("/mutate")
    assert response.status_code == 200


def test_middleware_noop_when_api_key_is_empty_string() -> None:
    client = TestClient(_build_app(api_key=""))
    response = client.post("/mutate")
    assert response.status_code == 200
