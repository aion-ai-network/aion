import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.concurrency import run_in_threadpool

from aion.db.queries import many, one
from aion.db.tenant import tenant_connection
from aion.modules.planning.notifications import quiet_until
from aion.modules.planning.service import profile, rules
from aion.settings import Settings


async def telegram(settings: Settings, method: str, data: dict[str, Any]) -> dict[str, Any]:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise ValueError("Telegram not configured")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=data)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise ValueError("Telegram rejected request")
        return dict(payload["result"])


def push(
    settings: Settings, subscriptions: list[dict[str, Any]], title: str, message: str, intent: str
) -> list[UUID]:
    from pywebpush import WebPushException, webpush

    expired = []
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=json.dumps(
                    {"title": title, "body": message, "tag": intent, "url": "/notifications"},
                    ensure_ascii=False,
                ),
                vapid_private_key=settings.vapid_private_key.get_secret_value(),
                vapid_claims={"sub": settings.vapid_subject},
                timeout=15,
                ttl=3600,
            )
        except WebPushException as error:
            if error.response is not None and error.response.status_code in (404, 410):
                expired.append(sub["id"])
            else:
                raise
    return expired


async def send_delivery(
    engine: AsyncEngine, settings: Settings, user: UUID, item: UUID
) -> datetime | None:
    async with tenant_connection(engine, user) as c:
        row = await one(
            c,
            "SELECT * FROM app.deliveries WHERE user_id=:u AND id=:id FOR UPDATE",
            {"u": user, "id": item},
        )
        if not row or row["state"] in ("sent", "failed", "cancelled", "uncertain"):
            return None
        if row["state"] == "sending":
            await c.execute(
                text(
                    "UPDATE app.deliveries SET state='uncertain',error_code='interrupted' "
                    "WHERE user_id=:u AND id=:id"
                ),
                {"u": user, "id": item},
            )
            return None
        title, message, keyboard = "AION", "", None
        p = await profile(c, user)
        if row["intent_id"]:
            intent = await one(
                c,
                "SELECT i.*,a.outcome,a.version,a.keep_on_pause,n.message,n.title "
                "FROM app.notification_intents i JOIN app.activities a ON "
                "a.user_id=i.user_id AND a.id=i.activity_id JOIN app.web_notifications n "
                "ON n.user_id=i.user_id AND n.intent_id=i.id "
                "WHERE i.user_id=:u AND i.id=:id",
                {"u": user, "id": row["intent_id"]},
            )
            r = await rules(c, user)
            if (
                not intent
                or intent["state"] != "delivered"
                or intent["outcome"] != "unknown"
                or intent["version"] != intent["activity_version"]
                or row["channel"] not in r["channels"]
                or not p["notifications_enabled"]
                or (p["mode"] == "paused" and not intent["keep_on_pause"])
            ):
                await c.execute(
                    text("UPDATE app.deliveries SET state='cancelled' WHERE user_id=:u AND id=:id"),
                    {"u": user, "id": item},
                )
                return None
            if until := quiet_until(p, datetime.now(UTC)):
                return until
            title, message = intent["title"], intent["message"]
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": label, "callback_data": f"{intent['id']}:{action}"}
                        for label, action in (
                            ("Сделал", "done"),
                            ("Частично", "partial"),
                            ("Отменил", "cancelled"),
                        )
                    ]
                ]
            }
        else:
            answer = await one(
                c,
                "SELECT content FROM app.messages WHERE user_id=:u AND request_key=:k",
                {"u": user, "k": row["external_id"]},
            )
            if not answer:
                return None
            message = answer["content"]
        link = await one(c, "SELECT * FROM app.telegram_links WHERE user_id=:u", {"u": user})
        subscriptions = await many(
            c, "SELECT * FROM app.push_subscriptions WHERE user_id=:u LIMIT 10", {"u": user}
        )
        configured = (
            row["channel"] == "telegram" and link and settings.telegram_bot_token.get_secret_value()
        ) or (
            row["channel"] == "push"
            and subscriptions
            and settings.vapid_public_key
            and settings.vapid_private_key.get_secret_value()
            and settings.vapid_subject
        )
        await c.execute(
            text(
                "UPDATE app.deliveries SET state=:state,error_code=:error,updated_at=now() "
                "WHERE user_id=:u AND id=:id"
            ),
            {
                "u": user,
                "id": item,
                "state": "sending" if configured else "failed",
                "error": None if configured else "channel_unavailable",
            },
        )
        if not configured:
            return None
    state, external_id, code = "sent", None, None
    expired: list[UUID] = []
    try:
        if row["channel"] == "telegram":
            assert link
            data = {"chat_id": link["chat_id"], "text": message[:4000]}
            if keyboard:
                data["reply_markup"] = keyboard
            result = await telegram(settings, "sendMessage", data)
            external_id = str(result["message_id"])
        else:
            expired = await run_in_threadpool(
                push, settings, subscriptions, title, message, str(row["intent_id"])
            )
    except Exception as error:
        # A timeout may have occurred after acceptance. Never blindly resend it.
        state, code = "uncertain", type(error).__name__[:80]
    async with tenant_connection(engine, user) as c:
        await c.execute(
            text(
                "UPDATE app.deliveries SET state=:state,external_id=coalesce(:external,"
                "external_id),error_code=:error,updated_at=now() WHERE user_id=:u "
                "AND id=:id AND state='sending'"
            ),
            {"state": state, "external": external_id, "error": code, "u": user, "id": item},
        )
        for sub in expired:
            await c.execute(
                text("DELETE FROM app.push_subscriptions WHERE user_id=:u AND id=:id"),
                {"u": user, "id": sub},
            )
    return None
