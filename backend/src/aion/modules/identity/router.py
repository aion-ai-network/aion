from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from aion.modules.identity.schemas import (
    AccessUpdate,
    CreateInvite,
    Login,
    ProfileUpdate,
    Register,
    ResetPassword,
)
from aion.modules.identity.security import (
    DUMMY_HASH,
    SESSION_COOKIE,
    hash_password,
    new_token,
    token_hash,
    verify_password,
)
from aion.modules.identity.service import (
    authenticated,
    create_invite,
    current_user,
    load_profile,
    save_profile,
    throttle,
)
from aion.settings import Settings

router = APIRouter(prefix="/api/v1", tags=["identity"])


def set_session(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.environment == "production",
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("/auth/login")
async def login(data: Login, request: Request, response: Response) -> dict[str, bool]:
    settings: Settings = request.app.state.settings
    email = str(data.email).lower()
    await throttle(request, f"login:{email}")
    async with request.app.state.database.connect() as connection:
        result = await connection.execute(
            text("SELECT * FROM app.auth_credentials(:email)"), {"email": email}
        )
        credentials = result.mappings().first()
    async with request.app.state.password_limiter:
        valid = await run_in_threadpool(
            verify_password,
            data.password.get_secret_value(),
            credentials["password_hash"] if credentials else DUMMY_HASH,
        )
    if not valid or not credentials or credentials["status"] != "active":
        raise HTTPException(401, "Неверный email или пароль.")
    token = new_token()
    async with request.app.state.database.begin() as connection:
        created = await connection.scalar(
            text("""
            SELECT app.auth_login(:user,:password,:session,:hash,:expires)
        """),
            {
                "user": credentials["id"],
                "password": credentials["password_hash"],
                "session": uuid4(),
                "hash": token_hash(token),
                "expires": datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds),
            },
        )
        if not created:
            raise HTTPException(401, "Неверный email или пароль.")
    set_session(response, token, settings)
    return {"ok": True}


@router.post("/auth/register", status_code=201)
async def register(data: Register, request: Request, response: Response) -> dict[str, bool]:
    settings: Settings = request.app.state.settings
    await throttle(request, f"register:{token_hash(data.token)}")
    name = data.display_name.strip()
    if not name:
        raise HTTPException(422, "Укажите имя.")
    async with request.app.state.password_limiter:
        password = await run_in_threadpool(hash_password, data.password.get_secret_value())
    token = new_token()
    try:
        async with request.app.state.database.begin() as connection:
            user_id = await connection.scalar(
                text("""
                SELECT app.auth_register(:invite,:password,:name,:user,:session,:hash,:expires)
            """),
                {
                    "invite": token_hash(data.token),
                    "password": password,
                    "name": name,
                    "user": uuid4(),
                    "session": uuid4(),
                    "hash": token_hash(token),
                    "expires": datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds),
                },
            )
            if user_id is None:
                raise HTTPException(400, "Приглашение недействительно или уже использовано.")
    except IntegrityError as error:
        raise HTTPException(400, "Приглашение недействительно или уже использовано.") from error
    set_session(response, token, settings)
    return {"ok": True}


@router.post("/auth/reset-password", status_code=204)
async def reset_password(data: ResetPassword, request: Request) -> Response:
    await throttle(request, f"reset:{token_hash(data.token)}")
    async with request.app.state.password_limiter:
        password = await run_in_threadpool(hash_password, data.password.get_secret_value())
    async with request.app.state.database.begin() as connection:
        valid = await connection.scalar(
            text("SELECT app.auth_reset(:hash,:password)"),
            {"hash": token_hash(data.token), "password": password},
        )
        if not valid:
            raise HTTPException(400, "Ссылка недействительна или срок её действия истёк.")
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/auth/me")
async def me(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    async with authenticated(request) as identity:
        return await current_user(identity)


@router.get("/profiles/{user_id}")
async def get_profile(user_id: UUID, request: Request) -> dict[str, Any]:
    async with authenticated(request) as identity:
        return await load_profile(identity, user_id)


@router.put("/profiles/{user_id}")
async def update_profile(user_id: UUID, data: ProfileUpdate, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as identity:
        return await save_profile(identity, user_id, data)


@router.get("/auth/sessions")
async def sessions(request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as identity:
        result = await identity.connection.execute(
            text("""
            SELECT id,created_at,expires_at FROM app.sessions
            WHERE user_id = :user AND revoked_at IS NULL AND expires_at > now()
            ORDER BY created_at DESC
        """),
            {"user": identity.user_id},
        )
        return [dict(row) for row in result.mappings()]


@router.delete("/auth/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: UUID, request: Request) -> Response:
    async with authenticated(request, mutation=True) as identity:
        revoked = await identity.connection.scalar(
            text("""
            UPDATE app.sessions SET revoked_at = now()
            WHERE id = :id AND user_id = :user AND revoked_at IS NULL RETURNING id
        """),
            {"id": session_id, "user": identity.user_id},
        )
        if revoked is None:
            raise HTTPException(404, "Сессия не найдена.")
    return Response(status_code=204)


@router.post("/auth/logout", status_code=204)
@router.post("/auth/logout-all", status_code=204)
async def logout(request: Request) -> Response:
    async with authenticated(request, mutation=True) as identity:
        await identity.connection.execute(
            text("""
                UPDATE app.sessions SET revoked_at = now()
                WHERE user_id = :user AND (:all_sessions OR id = :session)
            """),
            {
                "user": identity.user_id,
                "session": identity.session_id,
                "all_sessions": request.url.path.endswith("logout-all"),
            },
        )
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/admin/invites")
async def invites(request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as identity:
        identity.require_admin()
        result = await identity.connection.execute(
            text("""
            SELECT id,email,created_at,expires_at,revoked_at,used_at FROM app.invites
            ORDER BY created_at DESC LIMIT 100
        """)
        )
        return [dict(row) for row in result.mappings()]


@router.post("/admin/invites", status_code=201)
async def issue_invite(data: CreateInvite, request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    async with authenticated(request, mutation=True) as identity:
        return await create_invite(identity, data, request.app.state.settings)


@router.delete("/admin/invites/{invite_id}", status_code=204)
async def revoke_invite(invite_id: UUID, request: Request) -> Response:
    async with authenticated(request, mutation=True) as identity:
        identity.require_admin()
        result = await identity.connection.scalar(
            text("""
            UPDATE app.invites SET revoked_at = now()
            WHERE id = :id AND used_at IS NULL AND revoked_at IS NULL RETURNING id
        """),
            {"id": invite_id},
        )
        if result is None:
            raise HTTPException(404, "Действующее приглашение не найдено.")
    return Response(status_code=204)


@router.get("/admin/users")
async def users(request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as identity:
        identity.require_admin()
        result = await identity.connection.execute(
            text("""
            SELECT id,email,role,status,created_at FROM app.users ORDER BY created_at DESC LIMIT 200
        """)
        )
        return [dict(row) for row in result.mappings()]


@router.put("/admin/users/{user_id}/access", status_code=204)
async def set_access(user_id: UUID, data: AccessUpdate, request: Request) -> Response:
    async with authenticated(request, mutation=True) as identity:
        identity.require_admin()
        changed = await identity.connection.scalar(
            text("SELECT app.admin_access(:id,:status)"), {"id": user_id, "status": data.status}
        )
        if not changed:
            raise HTTPException(404, "Участник не найден или изменение недоступно.")
    return Response(status_code=204)
