from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from aion.db.queries import many, one
from aion.modules.identity.service import authenticated
from aion.modules.planning import service as s
from aion.modules.planning.schemas import (
    ActivityInput,
    ActivityUpdate,
    ModeInput,
    ProjectInput,
    ResultInput,
    RulesInput,
    SnoozeInput,
    TaskInput,
    TaskUpdate,
)

router = APIRouter(prefix="/api/v1", tags=["planning"])


@router.get("/plan")
async def plan(request: Request) -> dict[str, Any]:
    async with authenticated(request) as i:
        args = {"u": i.user_id}
        return {
            "projects": await many(
                i.connection,
                "SELECT * FROM app.projects WHERE user_id=:u ORDER BY created_at DESC LIMIT 500",
                args,
            ),
            "tasks": await many(
                i.connection,
                "SELECT * FROM app.tasks WHERE user_id=:u ORDER BY created_at DESC LIMIT 500",
                args,
            ),
            "activities": await many(
                i.connection,
                "SELECT * FROM app.activities WHERE user_id=:u ORDER BY starts_at DESC LIMIT 500",
                args,
            ),
            "focus": await s.focus(i.connection, i.user_id, datetime.now(UTC)),
        }


@router.post("/projects", status_code=201)
async def project(data: ProjectInput, request: Request) -> dict[str, Any] | None:
    async with authenticated(request, mutation=True) as i:
        return await one(
            i.connection,
            "INSERT INTO app.projects(id,user_id,title) VALUES(:id,:u,:title) RETURNING *",
            {"id": uuid4(), "u": i.user_id, "title": data.title},
        )


@router.post("/tasks", status_code=201)
async def create_task(data: TaskInput, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.task(i.connection, i.user_id, data)


@router.put("/tasks/{item}")
async def update_task(item: UUID, data: TaskUpdate, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.task(i.connection, i.user_id, data, item, data.version)


@router.post("/activities", status_code=201)
async def create_activity(data: ActivityInput, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.activity(i.connection, i.user_id, data)


@router.put("/activities/{item}")
async def update_activity(item: UUID, data: ActivityUpdate, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.activity(i.connection, i.user_id, data, item, data.version)


@router.post("/activities/{item}/result")
async def record_result(item: UUID, data: ResultInput, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.result(i.connection, i.user_id, item, data)


@router.put("/mode")
async def mode(data: ModeInput, request: Request) -> dict[str, Any] | None:
    async with authenticated(request, mutation=True) as i:
        return await one(
            i.connection,
            "UPDATE app.user_profiles SET mode=:mode,version=version+1 "
            "WHERE user_id=:u RETURNING *",
            {"mode": data.mode, "u": i.user_id},
        )


@router.get("/notifications/rules")
async def get_rules(request: Request) -> dict[str, Any]:
    async with authenticated(request) as i:
        return await s.rules(i.connection, i.user_id)


@router.put("/notifications/rules")
async def set_rules(data: RulesInput, request: Request) -> dict[str, Any] | None:
    async with authenticated(request, mutation=True) as i:
        return await s.save_rules(i.connection, i.user_id, data)


@router.get("/notifications")
async def notifications(request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as i:
        return await many(
            i.connection,
            """
          SELECT n.*,i.activity_id,i.activity_version,i.kind,i.state,a.outcome
          FROM app.web_notifications n JOIN app.notification_intents i
            ON i.user_id=n.user_id AND i.id=n.intent_id
          JOIN app.activities a ON a.user_id=i.user_id AND a.id=i.activity_id
          WHERE n.user_id=:u ORDER BY n.created_at DESC LIMIT 100
        """,
            {"u": i.user_id},
        )


@router.post("/notifications/{item}/later")
async def later(item: UUID, data: SnoozeInput, request: Request) -> dict[str, bool]:
    async with authenticated(request, mutation=True) as i:
        intent = await one(
            i.connection,
            "SELECT * FROM app.notification_intents WHERE user_id=:u AND id=:id",
            {"u": i.user_id, "id": item},
        )
        if not intent:
            raise HTTPException(404, "Обращение не найдено.")
        a = await s.owned(i.connection, "activities", i.user_id, intent["activity_id"])
        intent = await s.owned(i.connection, "notification_intents", i.user_id, item)
        if intent["state"] != "delivered" or a["version"] != intent["activity_version"]:
            raise HTTPException(409, "Обращение уже закрыто.")
        due = datetime.now(UTC) + timedelta(minutes=data.minutes)
        await i.connection.execute(
            text(
                "UPDATE app.notification_intents SET state='pending',"
                "due_at=:due WHERE user_id=:u AND id=:id"
            ),
            {"due": due, "u": i.user_id, "id": item},
        )
        await i.connection.execute(
            text("""
          UPDATE app.background_jobs SET state='pending',due_at=:due,attempts=0,
            lease_token=NULL,lease_until=NULL,finished_at=NULL
          WHERE user_id=:u AND dedupe_key=:key
        """),
            {"due": due, "u": i.user_id, "key": f"intent:{item}"},
        )
        return {"ok": True}
