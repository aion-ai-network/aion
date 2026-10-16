from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aion.db.queries import many, one
from aion.modules.identity.security import new_token, token_hash
from aion.modules.identity.service import authenticated
from aion.modules.planning.schemas import Input

router = APIRouter(prefix="/api/v1", tags=["channels"])


class PushInput(Input):
    endpoint: str = Field(max_length=3000)
    p256dh: str = Field(min_length=40, max_length=200)
    auth: str = Field(min_length=16, max_length=100)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        hosts = {
            "fcm.googleapis.com",
            "updates.push.services.mozilla.com",
            "web.push.apple.com",
            "wns.windows.com",
        }
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not any(
                parsed.hostname == h or (parsed.hostname or "").endswith("." + h) for h in hosts
            )
        ):
            raise ValueError("Unsupported push service")
        return value


@router.get("/capabilities")
async def capabilities(request: Request) -> dict[str, Any]:
    async with authenticated(request):
        s = request.app.state.settings
        return {
            "llm": bool(s.llm_enabled and s.llm_model),
            "voice": bool(
                s.stt_enabled and s.stt_model and s.telegram_bot_token.get_secret_value()
            ),
            "telegram": bool(
                s.telegram_bot_token.get_secret_value()
                and s.telegram_bot_username
                and s.telegram_webhook_secret.get_secret_value()
            ),
            "push": bool(
                s.vapid_public_key and s.vapid_private_key.get_secret_value() and s.vapid_subject
            ),
            "vapid_public_key": s.vapid_public_key,
            "storage": s.storage_backend,
        }


@router.post("/push/subscriptions", status_code=201)
async def subscribe(data: PushInput, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        if not request.app.state.settings.vapid_public_key:
            raise HTTPException(503, "Web Push ещё не настроен. Уведомления доступны в вебе.")
        item = uuid4()
        await i.connection.execute(
            text("SELECT app.register_push(:id,:endpoint,:key,:auth)"),
            {"id": item, "endpoint": data.endpoint, "key": data.p256dh, "auth": data.auth},
        )
        return {"id": item}


@router.delete("/push/subscriptions", status_code=204)
async def unsubscribe(request: Request) -> Response:
    async with authenticated(request, mutation=True) as i:
        await i.connection.execute(
            text("DELETE FROM app.push_subscriptions WHERE user_id=:u"), {"u": i.user_id}
        )
    return Response(status_code=204)


@router.get("/telegram/link")
async def link_status(request: Request) -> dict[str, Any]:
    async with authenticated(request) as i:
        return {
            "connected": bool(
                await one(
                    i.connection,
                    "SELECT user_id FROM app.telegram_links WHERE user_id=:u",
                    {"u": i.user_id},
                )
            ),
            "pending": await many(
                i.connection,
                "SELECT id,candidate_id,expires_at FROM "
                "app.telegram_link_tokens WHERE user_id=:u AND session_id=:session "
                "AND used_at IS NULL AND expires_at>now() ORDER BY created_at DESC LIMIT 1",
                {"u": i.user_id, "session": i.session_id},
            ),
        }


@router.post("/telegram/link", status_code=201)
async def link(request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        s = request.app.state.settings
        if not (
            s.telegram_bot_username
            and s.telegram_bot_token.get_secret_value()
            and s.telegram_webhook_secret.get_secret_value()
        ):
            raise HTTPException(503, "Telegram-бот ещё не настроен.")
        token = new_token()
        await i.connection.execute(
            text(
                "DELETE FROM app.telegram_link_tokens WHERE user_id=:u "
                "AND session_id=:session AND used_at IS NULL"
            ),
            {"u": i.user_id, "session": i.session_id},
        )
        item = uuid4()
        await i.connection.execute(
            text("""
          INSERT INTO app.telegram_link_tokens(id,user_id,session_id,token_hash,expires_at)
          VALUES(:id,:u,:session,:hash,:expires)
        """),
            {
                "id": item,
                "u": i.user_id,
                "session": i.session_id,
                "hash": token_hash(token),
                "expires": datetime.now(UTC) + timedelta(minutes=10),
            },
        )
        return {"id": item, "link": f"https://t.me/{s.telegram_bot_username}?start={token}"}


@router.post("/telegram/link/{item}/confirm")
async def confirm(item: UUID, request: Request) -> dict[str, bool]:
    try:
        async with authenticated(request, mutation=True) as i:
            row = await one(
                i.connection,
                "SELECT * FROM app.telegram_link_tokens WHERE user_id=:u "
                "AND id=:id AND session_id=:session AND expires_at>now() "
                "AND used_at IS NULL AND candidate_id IS NOT NULL FOR UPDATE",
                {"u": i.user_id, "id": item, "session": i.session_id},
            )
            if not row:
                raise HTTPException(
                    409, "Откройте ссылку в Telegram и подтвердите здесь в той же сессии."
                )
            await i.connection.execute(
                text(
                    "INSERT INTO app.telegram_links(user_id,telegram_id,chat_id) "
                    "VALUES(:u,:tg,:chat) ON CONFLICT(user_id) DO UPDATE SET "
                    "telegram_id=:tg,chat_id=:chat"
                ),
                {"u": i.user_id, "tg": row["candidate_id"], "chat": row["candidate_chat"]},
            )
            await i.connection.execute(
                text(
                    "UPDATE app.telegram_link_tokens SET used_at=now() WHERE user_id=:u AND id=:id"
                ),
                {"u": i.user_id, "id": item},
            )
    except IntegrityError as error:
        raise HTTPException(409, "Этот Telegram уже связан с другим аккаунтом.") from error
    return {"ok": True}


@router.delete("/telegram/link", status_code=204)
async def disconnect(request: Request) -> Response:
    async with authenticated(request, mutation=True) as i:
        await i.connection.execute(
            text("DELETE FROM app.telegram_links WHERE user_id=:u"), {"u": i.user_id}
        )
        await i.connection.execute(
            text("DELETE FROM app.telegram_link_tokens WHERE user_id=:u"), {"u": i.user_id}
        )
    return Response(status_code=204)
