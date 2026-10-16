from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from sqlalchemy import text

from aion.db.queries import many
from aion.modules.identity.service import authenticated
from aion.modules.planning.schemas import Input

router = APIRouter(prefix="/api/v1/admin", tags=["operations"])


class Limits(Input):
    ai_daily_requests: int = Field(ge=0, le=1000)
    audio_daily_seconds: int = Field(ge=0, le=36000)
    storage_limit_mb: int = Field(ge=0, le=100000)


@router.get("/operations")
async def operations(request: Request) -> dict[str, Any]:
    async with authenticated(request) as i:
        i.require_admin()
        result = await i.connection.scalar(text("SELECT app.admin_operations()"))
        return dict(result)


@router.get("/limits")
async def limits(request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as i:
        i.require_admin()
        # No personal content is included in the group control panel.
        return await many(
            i.connection,
            "SELECT user_id,ai_daily_requests,audio_daily_seconds,"
            "storage_limit_mb FROM app.user_limits ORDER BY user_id LIMIT 200",
        )


@router.put("/users/{item}/limits")
async def update(item: UUID, data: Limits, request: Request) -> dict[str, bool]:
    async with authenticated(request, mutation=True) as i:
        i.require_admin()
        changed = await i.connection.scalar(
            text("SELECT app.admin_limits(:id,:ai,:audio,:storage)"),
            {
                "id": item,
                "ai": data.ai_daily_requests,
                "audio": data.audio_daily_seconds,
                "storage": data.storage_limit_mb,
            },
        )
        if not changed:
            raise HTTPException(404, "Аккаунт не найден.")
        return {"ok": True}
