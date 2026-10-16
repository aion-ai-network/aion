import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aion.db.queries import many, one
from aion.modules.planning.schemas import ActivityInput, ResultInput, RulesInput, TaskInput


def absolute(value: datetime, zone: str) -> datetime:
    """A wall time belongs to the profile zone, never to the API host's zone."""
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    tz = ZoneInfo(zone)
    candidates = {
        value.replace(tzinfo=tz, fold=fold).astimezone(UTC)
        for fold in (0, 1)
        if value.replace(tzinfo=tz, fold=fold).astimezone(UTC).astimezone(tz).replace(tzinfo=None)
        == value
    }
    if len(candidates) != 1:
        raise HTTPException(
            422, "Это местное время пропущено или неоднозначно. Укажите UTC-смещение."
        )
    return candidates.pop()


async def profile(c: AsyncConnection, user: UUID) -> dict[str, Any]:
    row = await one(c, "SELECT * FROM app.user_profiles WHERE user_id=:u", {"u": user})
    if row is None:
        raise HTTPException(404, "Профиль не найден.")
    return row


async def owned(c: AsyncConnection, table: str, user: UUID, item: UUID) -> dict[str, Any]:
    # Only internal constants may select a table. All values are bound parameters.
    if table not in {"projects", "tasks", "activities", "notification_intents"}:
        raise ValueError("Unknown domain table")
    row = await one(
        c,
        f"SELECT * FROM app.{table} WHERE user_id=:u AND id=:id FOR UPDATE",
        {"u": user, "id": item},
    )
    if row is None:
        raise HTTPException(404, "Запись не найдена.")
    return row


async def task(
    c: AsyncConnection,
    user: UUID,
    data: TaskInput,
    item: UUID | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    values = data.model_dump(exclude={"version"})
    if data.project_id:
        await owned(c, "projects", user, data.project_id)
    if data.due_at:
        values["due_at"] = absolute(data.due_at, (await profile(c, user))["timezone"])
    values.update(u=user, id=item or uuid4(), version=version)
    if item:
        await owned(c, "tasks", user, item)
        row = await one(
            c,
            """
          UPDATE app.tasks SET title=:title,description=:description,first_step=:first_step,
            project_id=:project_id,due_at=:due_at,priority=:priority,
            estimated_minutes=:estimated_minutes,status=:status,version=version+1,updated_at=now()
          WHERE user_id=:u AND id=:id AND version=:version RETURNING *
        """,
            values,
        )
    else:
        row = await one(
            c,
            """
          INSERT INTO app.tasks(id,user_id,title,description,first_step,project_id,due_at,
            priority,estimated_minutes,status)
          VALUES(:id,:u,:title,:description,:first_step,:project_id,:due_at,
            :priority,:estimated_minutes,:status) RETURNING *
        """,
            values,
        )
    if row is None:
        raise HTTPException(409, "Задача уже изменилась. Обновите план.")
    return row


async def enqueue(
    c: AsyncConnection,
    user: UUID,
    kind: str,
    key: str,
    payload: dict[str, Any],
    due: datetime | None = None,
) -> None:
    await c.execute(
        text("""
      INSERT INTO app.background_jobs(id,user_id,kind,dedupe_key,payload,due_at)
      VALUES(:id,:u,:kind,:key,CAST(:payload AS jsonb),:due)
      ON CONFLICT(user_id,dedupe_key) DO NOTHING
    """),
        {
            "id": uuid4(),
            "u": user,
            "kind": kind,
            "key": key,
            "payload": json.dumps(payload, default=str),
            "due": due or datetime.now(UTC),
        },
    )


async def rules(c: AsyncConnection, user: UUID) -> dict[str, Any]:
    await c.execute(
        text("INSERT INTO app.notification_rules(user_id) VALUES(:u) ON CONFLICT DO NOTHING"),
        {"u": user},
    )
    row = await one(c, "SELECT * FROM app.notification_rules WHERE user_id=:u", {"u": user})
    assert row is not None
    return row


async def cancel_intents(c: AsyncConnection, user: UUID, item: UUID) -> None:
    await c.execute(
        text("""
      UPDATE app.notification_intents SET state='cancelled'
      WHERE user_id=:u AND activity_id=:id AND state IN ('pending','delivered')
    """),
        {"u": user, "id": item},
    )
    await c.execute(
        text("""
      UPDATE app.background_jobs SET state='cancelled',finished_at=now()
      WHERE user_id=:u AND kind='notification' AND payload->>'activity_id'=:id
        AND state IN ('pending','queued','running')
    """),
        {"u": user, "id": str(item)},
    )


async def schedule(c: AsyncConnection, user: UUID, activity: dict[str, Any]) -> None:
    r = await rules(c, user)
    r.update({k: v for k, v in activity["notification_overrides"].items() if v is not None})
    if activity["outcome"] != "unknown":
        return
    moments = {
        "reminder": activity["starts_at"] - timedelta(minutes=r["lead_minutes"]),
        "progress": activity["starts_at"] + timedelta(minutes=r["progress_minutes"]),
        "result": activity["ends_at"] + timedelta(minutes=r["result_delay_minutes"]),
    }
    for kind, due in moments.items():
        if not r[{"reminder": "reminders", "progress": "progress", "result": "results"}[kind]]:
            continue
        if kind == "progress" and due >= activity["ends_at"]:
            continue
        for repeat in range(r["repeat_count"] + 1):
            # A reminder that was missed should not be sent after the event starts.
            moment = due + timedelta(minutes=r["repeat_minutes"] * repeat)
            if kind == "reminder" and moment > activity["starts_at"]:
                continue
            intent = uuid4()
            inserted = await c.scalar(
                text("""
              INSERT INTO app.notification_intents(id,user_id,activity_id,activity_version,
                kind,due_at,repeat_number) VALUES(:id,:u,:a,:v,:kind,:due,:repeat)
              ON CONFLICT DO NOTHING RETURNING id
            """),
                {
                    "id": intent,
                    "u": user,
                    "a": activity["id"],
                    "v": activity["version"],
                    "kind": kind,
                    "due": moment,
                    "repeat": repeat,
                },
            )
            if inserted is None:
                continue
            await enqueue(
                c,
                user,
                "notification",
                f"intent:{intent}",
                {"intent_id": intent, "activity_id": activity["id"]},
                moment,
            )


async def activity(
    c: AsyncConnection,
    user: UUID,
    data: ActivityInput,
    item: UUID | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    p = await profile(c, user)
    values = data.model_dump(exclude={"version"})
    if data.task_id:
        await owned(c, "tasks", user, data.task_id)
    values["starts_at"] = absolute(data.starts_at, p["timezone"])
    values["ends_at"] = absolute(data.ends_at, p["timezone"])
    if values["ends_at"] <= values["starts_at"]:
        raise HTTPException(422, "Конец должен быть позже начала.")
    values["notification_overrides"] = json.dumps(values["notification_overrides"])
    values.update(u=user, id=item or uuid4(), version=version)
    if item:
        old = await owned(c, "activities", user, item)
        if old["outcome"] != "unknown":
            raise HTTPException(409, "Результат уже записан. Создайте новую активность.")
        row = await one(
            c,
            """
          UPDATE app.activities SET title=:title,notes=:notes,task_id=:task_id,starts_at=:starts_at,
            ends_at=:ends_at,fixed=:fixed,keep_on_pause=:keep_on_pause,
            notification_overrides=CAST(:notification_overrides AS jsonb),
            version=version+1,updated_at=now()
          WHERE user_id=:u AND id=:id AND version=:version RETURNING *
        """,
            values,
        )
        if row:
            await cancel_intents(c, user, item)
    else:
        row = await one(
            c,
            """
          INSERT INTO app.activities(id,user_id,title,notes,task_id,starts_at,ends_at,fixed,
            keep_on_pause,notification_overrides)
          VALUES(:id,:u,:title,:notes,:task_id,:starts_at,:ends_at,:fixed,:keep_on_pause,
            CAST(:notification_overrides AS jsonb)) RETURNING *
        """,
            values,
        )
    if row is None:
        raise HTTPException(409, "Активность уже изменилась. Обновите план.")
    await schedule(c, user, row)
    return row


async def result(c: AsyncConnection, user: UUID, item: UUID, data: ResultInput) -> dict[str, Any]:
    old = await owned(c, "activities", user, item)
    if old["version"] != data.version or old["outcome"] != "unknown":
        raise HTTPException(409, "Активность уже изменилась. Обновите план.")
    if data.intent_id:
        intent = await owned(c, "notification_intents", user, data.intent_id)
        if (
            intent["activity_id"] != item
            or intent["activity_version"] != data.version
            or intent["state"] != "delivered"
        ):
            raise HTTPException(409, "Обращение уже закрыто или перенесено.")
    await c.execute(
        text("""
      INSERT INTO app.activity_results(id,user_id,activity_id,intent_id,outcome,content)
      VALUES(:id,:u,:a,:intent,:outcome,:content)
    """),
        {
            "id": uuid4(),
            "u": user,
            "a": item,
            "intent": data.intent_id,
            "outcome": data.outcome,
            "content": data.content,
        },
    )
    row = await one(
        c,
        """
      UPDATE app.activities SET outcome=:outcome,result_text=:content,version=version+1,
        updated_at=now() WHERE user_id=:u AND id=:id RETURNING *
    """,
        {"outcome": data.outcome, "content": data.content, "u": user, "id": item},
    )
    await cancel_intents(c, user, item)
    if data.intent_id:
        await c.execute(
            text("""
          UPDATE app.notification_intents SET state='answered',answered_at=now()
          WHERE user_id=:u AND id=:id
        """),
            {"u": user, "id": data.intent_id},
        )
    assert row is not None
    return row


async def focus(c: AsyncConnection, user: UUID, now: datetime) -> dict[str, Any]:
    p = await profile(c, user)
    zone = ZoneInfo(p["timezone"])
    local = now.astimezone(zone)
    end = datetime.combine(local.date(), p["day_end"], zone).astimezone(UTC)
    if end < now:
        end = now
    events = await many(
        c,
        """
      SELECT * FROM app.activities WHERE user_id=:u AND outcome='unknown'
      AND ends_at>:now AND starts_at<:end ORDER BY starts_at LIMIT 100
    """,
        {"u": user, "now": now, "end": end + timedelta(days=1)},
    )
    fixed = next((e for e in events if e["fixed"]), None)
    available = max(
        0, int(((min(end, fixed["starts_at"]) if fixed else end) - now).total_seconds() // 60)
    )
    tasks = await many(
        c,
        """
      SELECT * FROM app.tasks WHERE user_id=:u AND status='open'
      ORDER BY (due_at IS NOT NULL AND due_at<=:soon) DESC, priority,
        due_at NULLS LAST,created_at LIMIT 100
    """,
        {"u": user, "soon": now + timedelta(days=1)},
    )
    selected = []
    if p["mode"] != "paused":
        for t in tasks:
            if t["estimated_minutes"] <= available:
                reason = f"Приоритет {t['priority']}; займёт около {t['estimated_minutes']} мин."
                if t["due_at"]:
                    reason += " Срок: " + t["due_at"].astimezone(zone).strftime("%d.%m %H:%M") + "."
                selected.append({**t, "reason": reason})
                available -= t["estimated_minutes"]
                if len(selected) >= (1 if p["mode"] == "light" else 3):
                    break
    return {
        "mode": p["mode"],
        "timezone": p["timezone"],
        "priorities": selected,
        "activities": [
            e for e in events if p["mode"] != "paused" or e["fixed"] or e["keep_on_pause"]
        ],
        "explanation": (
            "Пауза: остаются фиксированные события и выбранные исключения."
            if p["mode"] == "paused"
            else "Выбор учитывает сроки, приоритет, длительность и время до события."
        ),
    }


async def save_rules(c: AsyncConnection, user: UUID, data: RulesInput) -> dict[str, Any]:
    await rules(c, user)
    row = await one(
        c,
        """
      UPDATE app.notification_rules SET reminders=:reminders,progress=:progress,
        results=:results,channels=:channels,lead_minutes=:lead_minutes,
        result_delay_minutes=:result_delay_minutes,progress_minutes=:progress_minutes,
        daily_limit=:daily_limit,repeat_count=:repeat_count,repeat_minutes=:repeat_minutes,
        tone=:tone,message_format=:message_format,version=version+1
      WHERE user_id=:u AND version=:version RETURNING *
    """,
        {**data.model_dump(), "u": user},
    )
    if row is None:
        raise HTTPException(409, "Правила уже изменились. Обновите страницу.")
    # Rebuild future work in the same transaction, preserving delivered/answered intent
    activities = await many(
        c,
        "SELECT * FROM app.activities WHERE user_id=:u AND outcome='unknown' FOR UPDATE",
        {"u": user},
    )
    for a in activities:
        await cancel_intents(c, user, a["id"])
        updated = await one(
            c,
            "UPDATE app.activities SET version=version+1 WHERE user_id=:u AND id=:id RETURNING *",
            {"u": user, "id": a["id"]},
        )
        assert updated
        await schedule(c, user, updated)
    return row
