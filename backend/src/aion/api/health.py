import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aion import __version__
from aion.settings import Settings

router = APIRouter(tags=["system"])


class Check(BaseModel):
    status: Literal["up", "down", "starting", "unknown"]


class SystemStatus(BaseModel):
    service: str = "aion"
    version: str = __version__
    ready: bool
    operational: bool
    checks: dict[str, Check]


async def collect_health(engine: AsyncEngine, redis: Redis, settings: Settings) -> SystemStatus:
    checks = {
        name: Check(status="unknown")
        for name in (
            "database",
            "schema",
            "redis",
            "scheduler",
            "worker",
            "worker_ai",
            "worker_delivery",
        )
    }

    async def check_database() -> None:
        try:
            async with asyncio.timeout(settings.dependency_timeout_seconds):
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                    checks["database"] = Check(status="up")
                    checks["schema"] = Check(status="down")
                    rows = await connection.execute(
                        text("SELECT component, observed_at FROM app.service_heartbeats")
                    )
                    await connection.execute(text("SELECT user_id FROM app.user_profiles LIMIT 0"))
                    await connection.execute(text("SELECT id FROM app.assistant_runs LIMIT 0"))
                    await connection.execute(
                        text("SELECT receiving_at FROM app.attachments LIMIT 0")
                    )
                    checks["schema"] = Check(status="up")
                    observed = {component: timestamp for component, timestamp in rows}
                    now = datetime.now(UTC)
                    for component in ("scheduler", "worker", "worker_ai", "worker_delivery"):
                        last_seen = observed.get(component)
                        if last_seen is None:
                            checks[component] = Check(status="starting")
                        else:
                            age = (now - last_seen).total_seconds()
                            checks[component] = Check(
                                status="up"
                                if 0 <= age <= settings.heartbeat_max_age_seconds
                                else "down"
                            )
        except Exception:
            if checks["database"].status != "up":
                checks["database"] = Check(status="down")

    async def check_redis() -> None:
        try:
            async with asyncio.timeout(settings.dependency_timeout_seconds):
                await redis.execute_command("PING")
                checks["redis"] = Check(status="up")
        except Exception:
            checks["redis"] = Check(status="down")

    await asyncio.gather(check_database(), check_redis())
    ready = all(checks[name].status == "up" for name in ("database", "schema", "redis"))
    return SystemStatus(
        ready=ready,
        operational=ready
        and all(
            checks[name].status == "up"
            for name in ("scheduler", "worker", "worker_ai", "worker_delivery")
        ),
        checks=checks,
    )


async def status_for(request: Request) -> SystemStatus:
    return await collect_health(
        request.app.state.database, request.app.state.redis, request.app.state.settings
    )


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", response_model=SystemStatus)
async def ready(request: Request) -> JSONResponse:
    status = await status_for(request)
    return JSONResponse(status.model_dump(), status_code=200 if status.ready else 503)


@router.get("/api/v1/system/status", response_model=SystemStatus)
async def system_status(request: Request) -> SystemStatus:
    return await status_for(request)
