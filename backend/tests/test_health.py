from fastapi import HTTPException
from fastapi.testclient import TestClient

from aion.app import create_app
from aion.settings import Settings


def disconnected_app():
    return create_app(Settings(
        _env_file=None, environment="test", db_host="127.0.0.1", db_port=1,
        redis_url="redis://127.0.0.1:1/0", dependency_timeout_seconds=0.2,
    ))


def test_dependency_outage_changes_readiness_but_not_liveness():
    with TestClient(disconnected_app()) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["ready"] is False
        assert response.json()["checks"]["database"]["status"] == "down"
        assert response.json()["checks"]["redis"]["status"] == "down"
        assert client.get("/health/live").status_code == 200


def test_system_status_remains_readable_during_an_outage():
    with TestClient(disconnected_app()) as client:
        response = client.get("/api/v1/system/status")
        assert response.status_code == 200
        assert response.json()["operational"] is False


def test_errors_keep_request_id_and_http_headers():
    with TestClient(disconnected_app()) as client:
        response = client.post("/health/live", headers={"X-Request-ID": "check-123"})
        assert response.status_code == 405
        assert "GET" in response.headers["Allow"]
        assert response.headers["X-Request-ID"] == "check-123"
        assert response.json()["error"]["request_id"] == "check-123"


def test_invalid_request_id_is_replaced():
    with TestClient(disconnected_app()) as client:
        response = client.get("/not-found", headers={"X-Request-ID": "invalid id"})
        assert response.status_code == 404
        assert response.headers["X-Request-ID"] != "invalid id"
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_unexpected_error_does_not_expose_exception_contents():
    app = disconnected_app()

    @app.get("/test-error")
    async def fail():
        raise RuntimeError("private connection credentials")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-error")
        assert response.status_code == 500
        assert "private connection credentials" not in response.text
        assert response.json()["error"]["code"] == "internal_error"
        assert response.headers["X-Request-ID"]


def test_explicit_http_errors_preserve_authentication_challenge():
    app = disconnected_app()

    @app.get("/test-auth")
    async def authentication_error():
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})

    with TestClient(app) as client:
        response = client.get("/test-auth")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
