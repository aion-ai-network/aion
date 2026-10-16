"""Run with AION_INTEGRATION=1 against the dedicated aion_test database."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from aion.app import create_app
from aion.db.tenant import tenant_connection
from aion.modules.identity.cli import bootstrap_admin, recovery_link
from aion.modules.identity.security import SESSION_COOKIE, token_hash
from aion.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AION_INTEGRATION") != "1", reason="requires isolated PostgreSQL and Redis"
    ),
]
PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def database():
    settings = Settings(
        _env_file=None,
        environment="test",
        db_name=os.getenv("AION_DB_NAME", "aion_test"),
        db_port=int(os.getenv("AION_DB_PORT", "55432")),
        db_migration_password=SecretStr(
            os.getenv("AION_DB_MIGRATION_PASSWORD", "local_migration_password")
        ),
        redis_url=SecretStr(os.getenv("AION_REDIS_URL", "redis://127.0.0.1:56379/0")),
        public_origin="http://testserver",
    )
    assert settings.db_name.endswith("_test"), "Refusing integration writes outside a test database"
    prefix = f"m1-{uuid4().hex}-"
    email = f"{prefix}admin@example.com"
    bootstrap_admin(settings, email, PASSWORD, "Test administrator")
    engine = create_engine(settings.migration_database_url)
    try:
        yield settings, engine, prefix, email
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM app.users WHERE email LIKE :prefix"), {"prefix": prefix + "%"}
            )
        engine.dispose()


@contextmanager
def browser(settings):
    with TestClient(
        create_app(settings),
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
        headers={"X-AION-Request": "1", "Origin": "http://testserver"},
    ) as client:
        yield client


def login(client, email, password=PASSWORD):
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    account = client.get("/api/v1/auth/me").json()
    client.headers["X-CSRF-Token"] = account["csrf_token"]
    return account


@pytest.fixture
def context(database):
    settings, engine, prefix, email = database
    settings = settings.model_copy(update={"rate_limit_prefix": f"m1-test:{uuid4().hex}"})
    with browser(settings) as admin:
        login(admin, email)
        yield settings, engine, prefix, admin


def invite(context, label=None):
    _, _, prefix, admin = context
    email = f"{prefix}{label or uuid4().hex}@example.com"
    response = admin.post("/api/v1/admin/invites", json={"email": email})
    assert response.status_code == 201, response.text
    data = response.json()
    return email, data["link"].split("#")[1], data["id"]


@contextmanager
def member(context):
    email, token, _ = invite(context)
    with browser(context[0]) as client:
        response = client.post(
            "/api/v1/auth/register",
            json={"token": token, "password": PASSWORD, "display_name": "Participant"},
        )
        assert response.status_code == 201, response.text
        account = client.get("/api/v1/auth/me").json()
        client.headers["X-CSRF-Token"] = account["csrf_token"]
        yield client, account, email


def profile_payload(account):
    return {key: value for key, value in account["profile"].items() if key != "user_id"}


def test_invite_registration_profile_and_isolation(context):
    with member(context) as (alice, a, _), member(context) as (bob, b, _):
        assert a["user"]["id"] != b["user"]["id"]
        payload = {
            **profile_payload(a),
            "timezone": "Europe/Saratov",
            "mode": "light",
            "onboarding_completed": True,
        }
        own = f"/api/v1/profiles/{a['user']['id']}"
        assert alice.put(own, json=payload).status_code == 200
        assert alice.get(own).json()["timezone"] == "Europe/Saratov"
        assert alice.put(own, json=payload).status_code == 409
        assert bob.get(own).status_code == 404
        assert bob.put(own, json=profile_payload(b)).status_code == 404
        spoof = {**profile_payload(b), "user_id": a["user"]["id"]}
        assert bob.put(f"/api/v1/profiles/{b['user']['id']}", json=spoof).status_code == 422
        assert bob.get("/api/v1/admin/users").status_code == 403
        assert alice.get("/api/v1/auth/me").headers["cache-control"] == "no-store"


@pytest.mark.parametrize("state", ["revoked", "expired", "used"])
def test_invites_are_single_use_and_expirable(context, state):
    _, token, invite_id = invite(context)
    if state == "revoked":
        assert context[3].delete(f"/api/v1/admin/invites/{invite_id}").status_code == 204
    elif state == "expired":
        with context[1].begin() as connection:
            connection.execute(
                text("UPDATE app.invites SET expires_at=now()-interval '1 second' WHERE id=:id"),
                {"id": UUID(invite_id)},
            )
    with browser(context[0]) as client:
        data = {"token": token, "password": PASSWORD, "display_name": "Test"}
        if state == "used":
            assert client.post("/api/v1/auth/register", json=data).status_code == 201
        assert client.post("/api/v1/auth/register", json=data).status_code == 400


def test_parallel_redemption_creates_one_account(context):
    email, token, _ = invite(context)

    def redeem(_):
        with browser(context[0]) as client:
            return client.post(
                "/api/v1/auth/register",
                json={"token": token, "password": PASSWORD, "display_name": "Concurrent"},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(redeem, range(2))) == [201, 400]
    with context[1].connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM app.users WHERE email=:email"), {"email": email}
            )
            == 1
        )


def test_logout_revokes_server_session(context):
    with member(context) as (client, _, _):
        token = client.cookies.get(SESSION_COOKIE)
        assert client.post("/api/v1/auth/logout").status_code == 204
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/api/v1/auth/me").status_code == 401


def test_sessions_are_owner_scoped_and_revocable(context):
    with member(context) as (alice, a, email), member(context) as (bob, _, _):
        assert bob.delete(f"/api/v1/auth/sessions/{a['session_id']}").status_code == 404
        with browser(context[0]) as second:
            login(second, email)
            assert len(alice.get("/api/v1/auth/sessions").json()) == 2
            assert alice.post("/api/v1/auth/logout-all").status_code == 204
            assert second.get("/api/v1/auth/me").status_code == 401


def test_revoked_access_invalidates_all_sessions(context):
    with member(context) as (client, account, email):
        url = f"/api/v1/admin/users/{account['user']['id']}/access"
        assert context[3].put(url, json={"status": "disabled"}).status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
            ).status_code
            == 401
        )
        assert context[3].put(url, json={"status": "active"}).status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401
        login(client, email)


def test_csrf_and_origin_are_required(context):
    with member(context) as (client, account, _):
        path = f"/api/v1/profiles/{account['user']['id']}"
        csrf = client.headers.pop("X-CSRF-Token")
        assert client.put(path, json=profile_payload(account)).status_code == 403
        client.headers["X-CSRF-Token"] = csrf
        client.headers["Origin"] = "https://attacker.example"
        assert client.put(path, json=profile_payload(account)).status_code == 403
        client.headers.pop("X-AION-Request")
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "unknown@example.com", "password": PASSWORD}
            ).status_code
            == 403
        )


def test_password_recovery_revokes_sessions_and_token(context):
    with member(context) as (client, _, email):
        token = recovery_link(context[0], email).split("#")[1]
        with context[1].connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM app.password_resets WHERE token_hash=:hash"),
                    {"hash": token_hash(token)},
                )
                == 1
            )
        data = {"token": token, "password": "a different strong passphrase"}
        with browser(context[0]) as recovery:
            assert recovery.post("/api/v1/auth/reset-password", json=data).status_code == 204
            assert recovery.post("/api/v1/auth/reset-password", json=data).status_code == 400
        assert client.get("/api/v1/auth/me").status_code == 401
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
            ).status_code
            == 401
        )
        login(client, email, data["password"])


def test_session_expiry(context):
    with member(context) as (client, account, _):
        with context[1].begin() as connection:
            connection.execute(
                text("UPDATE app.sessions SET expires_at=now()-interval '1 second' WHERE id=:id"),
                {"id": UUID(account["session_id"])},
            )
        assert client.get("/api/v1/auth/me").status_code == 401


def test_login_rate_limit(context):
    settings = context[0].model_copy(update={"auth_attempt_limit": 2})
    with browser(settings) as client:
        data = {"email": "unknown@example.com", "password": "wrong"}
        assert client.post("/api/v1/auth/login", json=data).status_code == 401
        assert client.post("/api/v1/auth/login", json=data).status_code == 401
        response = client.post("/api/v1/auth/login", json=data)
        assert response.status_code == 429
        assert "Retry-After" in response.headers


def test_rls_and_pool_context_do_not_leak(context):
    with member(context) as (_, a, _), member(context) as (_, b, _):

        async def verify():
            engine = create_async_engine(context[0].database_url, pool_size=1, max_overflow=0)
            alice, bob = UUID(a["user"]["id"]), UUID(b["user"]["id"])
            try:
                async with tenant_connection(engine, alice) as connection:
                    assert await connection.scalar(text("SELECT current_user")) == "aion_runtime"
                    result = await connection.execute(text("SELECT user_id FROM app.user_profiles"))
                    assert result.scalars().all() == [alice]
                    assert (
                        await connection.scalar(text("SELECT count(*) FROM app.user_limits")) == 1
                    )
                    assert await connection.scalar(text("SELECT count(*) FROM app.users")) == 1
                async with engine.begin() as connection:
                    assert (
                        await connection.scalar(text("SELECT count(*) FROM app.user_profiles")) == 0
                    )
                async with tenant_connection(engine, bob) as connection:
                    result = await connection.execute(text("SELECT user_id FROM app.user_profiles"))
                    assert result.scalars().all() == [bob]
                with pytest.raises(DBAPIError):
                    async with tenant_connection(engine, alice) as connection:
                        await connection.execute(
                            text("UPDATE app.user_profiles SET user_id=:other"), {"other": bob}
                        )
                with pytest.raises(DBAPIError):
                    async with engine.begin() as connection:
                        await connection.execute(text("SELECT password_hash FROM app.users"))
                async with engine.begin() as connection:
                    assert (
                        await connection.scalar(text("SELECT count(*) FROM app.user_profiles")) == 0
                    )
            finally:
                await engine.dispose()

        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(verify())


def test_bootstrap_is_single_use(context):
    with pytest.raises(ValueError, match="already exists"):
        bootstrap_admin(context[0], "other@example.com", PASSWORD, "Other")
