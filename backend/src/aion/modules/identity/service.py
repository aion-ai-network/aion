import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aion.modules.identity.schemas import CreateInvite, ProfileUpdate
from aion.modules.identity.security import SESSION_COOKIE, csrf_token, new_token, token_hash
from aion.settings import Settings


def protect_origin(request: Request) -> None:
    settings: Settings = request.app.state.settings
    allowed = {settings.public_origin.rstrip("/")}
    if settings.environment != "production":
        allowed.update(
            {
                f"http://{host}:{port}"
                for host in ("127.0.0.1", "localhost")
                for port in (5173, 8000)
            }
        )
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    if origin is None and referer:
        parsed = urlsplit(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}"
    if request.headers.get("X-AION-Request") != "1" or (origin and origin not in allowed):
        raise HTTPException(403, "Запрос отклонён. Обновите страницу и повторите.")


async def throttle(request: Request, identity: str) -> None:
    protect_origin(request)
    settings: Settings = request.app.state.settings
    address = request.client.host if request.client else "unknown"
    # Atomically charge both the source IP and the target account. Expiry cannot
    # be lost between INCR and EXPIRE, including across parallel API processes.
    script = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return count
    """
    try:
        for scope, value, limit in (
            ("ip", address, settings.auth_attempt_limit * 10),
            ("account", identity, settings.auth_attempt_limit),
        ):
            key = f"{settings.rate_limit_prefix}:auth:{scope}:{token_hash(value)}"
            count = await request.app.state.redis.eval(script, 1, key, settings.auth_window_seconds)
            if int(count) > limit:
                raise HTTPException(
                    429,
                    "Слишком много попыток. Попробуйте позже.",
                    headers={"Retry-After": str(settings.auth_window_seconds)},
                )
    except RedisError as error:
        raise HTTPException(503, "Вход временно недоступен. Попробуйте позже.") from error


@dataclass
class Identity:
    connection: AsyncConnection
    user_id: UUID
    session_id: UUID
    email: str
    role: str
    csrf: str

    def require_admin(self) -> None:
        if self.role != "admin":
            raise HTTPException(403, "Нужны права администратора.")


@asynccontextmanager
async def authenticated(request: Request, *, mutation: bool = False) -> AsyncIterator[Identity]:
    raw_token = request.cookies.get(SESSION_COOKIE, "")
    if len(raw_token) != 43:
        raise HTTPException(401, "Войдите в аккаунт.")
    async with request.app.state.database.begin() as connection:
        result = await connection.execute(
            text("SELECT * FROM app.auth_session(:hash)"), {"hash": token_hash(raw_token)}
        )
        row = result.mappings().first()
        if row is None:
            raise HTTPException(401, "Сессия завершена. Войдите снова.")
        await connection.execute(
            text("SELECT set_config('aion.user_id', :id, true)"), {"id": str(row["user_id"])}
        )
        csrf = csrf_token(raw_token)
        if mutation:
            protect_origin(request)
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf):
                raise HTTPException(403, "Сессия изменилась. Обновите страницу.")
        yield Identity(
            connection, row["user_id"], row["session_id"], row["email"], row["role"], csrf
        )


async def load_profile(identity: Identity, owner_id: UUID) -> dict[str, Any]:
    if owner_id != identity.user_id:
        raise HTTPException(404, "Профиль не найден.")
    result = await identity.connection.execute(
        text("SELECT * FROM app.user_profiles WHERE user_id = :id"), {"id": owner_id}
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(404, "Профиль не найден.")
    return dict(row)


async def current_user(identity: Identity) -> dict[str, Any]:
    limits = await identity.connection.execute(
        text("SELECT * FROM app.user_limits WHERE user_id = :id"), {"id": identity.user_id}
    )
    return {
        "user": {"id": identity.user_id, "email": identity.email, "role": identity.role},
        "profile": await load_profile(identity, identity.user_id),
        "limits": dict(limits.mappings().one()),
        "csrf_token": identity.csrf,
        "session_id": identity.session_id,
    }


async def save_profile(identity: Identity, owner_id: UUID, data: ProfileUpdate) -> dict[str, Any]:
    await load_profile(identity, owner_id)
    values = data.model_dump()
    values["display_name"] = values["display_name"].strip()
    if not values["display_name"]:
        raise HTTPException(422, "Укажите имя.")
    result = await identity.connection.execute(
        text("""
        UPDATE app.user_profiles SET display_name = :display_name, timezone = :timezone,
          day_start = :day_start, day_end = :day_end, quiet_start = :quiet_start,
          quiet_end = :quiet_end, mode = :mode, notifications_enabled = :notifications_enabled,
          onboarding_completed = :onboarding_completed, version = version + 1
        WHERE user_id = :owner AND version = :version RETURNING *
    """),
        {**values, "owner": owner_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(409, "Настройки уже изменились. Обновите страницу.")
    return dict(row)


async def create_invite(
    identity: Identity, data: CreateInvite, settings: Settings
) -> dict[str, Any]:
    identity.require_admin()
    email = str(data.email).lower()
    exists = await identity.connection.scalar(
        text("SELECT id FROM app.users WHERE email = :email"), {"email": email}
    )
    if exists:
        raise HTTPException(409, "Аккаунт с этим email уже существует.")
    token = new_token()
    result = await identity.connection.execute(
        text("""
        INSERT INTO app.invites (id,email,token_hash,created_by,expires_at)
        VALUES (:id,:email,:hash,:creator,:expires)
        RETURNING id,email,created_at,expires_at,revoked_at,used_at
    """),
        {
            "id": uuid4(),
            "email": email,
            "hash": token_hash(token),
            "creator": identity.user_id,
            "expires": datetime.now(UTC) + timedelta(hours=data.hours),
        },
    )
    return {
        **dict(result.mappings().one()),
        "link": f"{settings.public_origin.rstrip('/')}/join#{token}",
    }
