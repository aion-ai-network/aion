from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aion.db.queries import one
from aion.modules.planning.service import enqueue, profile, rules


def quiet_until(p: dict[str, Any], now: datetime) -> datetime | None:
    zone = ZoneInfo(p["timezone"])
    local = now.astimezone(zone)
    start, end = p["quiet_start"], p["quiet_end"]
    current = local.time().replace(tzinfo=None)
    quiet = (
        (start <= current < end)
        if start < end
        else (current >= start or current < end)
        if start > end
        else False
    )
    if not quiet:
        return None
    day = local.date() + (timedelta(days=1) if start > end and current >= start else timedelta())
    return datetime.combine(day, end, zone).astimezone(UTC)


async def deliver(
    c: AsyncConnection, user: UUID, payload: dict[str, Any], now: datetime | None = None
) -> datetime | None:
    now = now or datetime.now(UTC)
    a = await one(
        c,
        "SELECT * FROM app.activities WHERE user_id=:u AND id=:a FOR UPDATE",
        {"u": user, "a": UUID(payload["activity_id"])},
    )
    intent = await one(
        c,
        "SELECT * FROM app.notification_intents WHERE user_id=:u AND id=:id FOR UPDATE",
        {"u": user, "id": UUID(payload["intent_id"])},
    )
    if not a or not intent or intent["state"] != "pending":
        return None
    p, r = await profile(c, user), await rules(c, user)
    r.update({k: v for k, v in a["notification_overrides"].items() if v is not None})
    enabled = r[
        {"reminder": "reminders", "progress": "progress", "result": "results"}[intent["kind"]]
    ]
    stale = (
        a["version"] != intent["activity_version"]
        or a["outcome"] != "unknown"
        or not enabled
        or not p["notifications_enabled"]
        or (p["mode"] == "paused" and not a["keep_on_pause"])
    )
    grace = timedelta(minutes=1) if r["lead_minutes"] == 0 else timedelta()
    stale |= intent["kind"] == "reminder" and now >= a["starts_at"] + grace
    stale |= intent["kind"] == "progress" and now >= a["ends_at"]
    if stale:
        await c.execute(
            text(
                "UPDATE app.notification_intents SET state='cancelled' WHERE user_id=:u AND id=:id"
            ),
            {"u": user, "id": intent["id"]},
        )
        return None
    if intent["due_at"] > now:
        return intent["due_at"]
    until = quiet_until(p, now)
    if until:
        return until
    # Serialize per-user counters across concurrent delivery workers.
    await c.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": f"delivery:{user}"}
    )
    local = now.astimezone(ZoneInfo(p["timezone"]))
    midnight = datetime.combine(local.date(), datetime.min.time(), local.tzinfo).astimezone(UTC)
    count = await c.scalar(
        text("SELECT count(*) FROM app.web_notifications WHERE user_id=:u AND created_at>=:start"),
        {"u": user, "start": midnight},
    )
    if int(count or 0) >= r["daily_limit"]:
        tomorrow = datetime.combine(
            local.date() + timedelta(days=1), p["quiet_end"], local.tzinfo
        ).astimezone(UTC)
        return tomorrow
    labels = {"reminder": "Скоро начнётся", "progress": "Как продвигается", "result": "Как прошло"}
    when = a["starts_at"].astimezone(ZoneInfo(p["timezone"])).strftime("%d.%m в %H:%M")
    message = f"{labels[intent['kind']]} «{a['title']}» ({when})?"
    if intent["kind"] == "reminder":
        message = f"{labels['reminder']}: «{a['title']}» — {when}."
    if r["message_format"] != "кратко" and a["notes"]:
        message += " " + a["notes"][:400]
    if r["tone"] == "поддерживающий":
        message = "В своём темпе. " + message
    elif r["tone"] == "нейтральный":
        message = {
            "reminder": "Напоминание",
            "progress": "Запрос прогресса",
            "result": "Запрос результата",
        }[intent["kind"]] + f": {a['title']} ({when})."
    await c.execute(
        text("""
      INSERT INTO app.web_notifications(id,user_id,intent_id,title,message,created_at)
      VALUES(:id,:u,:intent,:title,:message,:now) ON CONFLICT(user_id,intent_id) DO NOTHING
    """),
        {
            "id": uuid4(),
            "u": user,
            "intent": intent["id"],
            "title": a["title"],
            "message": message,
            "now": now,
        },
    )
    await c.execute(
        text("UPDATE app.notification_intents SET state='delivered' WHERE user_id=:u AND id=:id"),
        {"u": user, "id": intent["id"]},
    )
    for channel in r["channels"]:
        delivery = uuid4()
        row = await one(
            c,
            """
          INSERT INTO app.deliveries(id,user_id,intent_id,channel,state)
          VALUES(:id,:u,:intent,:channel,:state)
          ON CONFLICT(user_id,intent_id,channel) DO NOTHING RETURNING id
        """,
            {
                "id": delivery,
                "u": user,
                "intent": intent["id"],
                "channel": channel,
                "state": "sent" if channel == "web" else "prepared",
            },
        )
        if row and channel != "web":
            await enqueue(c, user, "external_delivery", f"delivery:{delivery}", {"id": delivery})
    return None
