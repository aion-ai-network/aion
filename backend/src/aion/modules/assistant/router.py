import asyncio
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.responses import StreamingResponse

from aion.db.queries import many
from aion.modules.assistant import schemas as dto
from aion.modules.assistant import service as s
from aion.modules.identity.service import authenticated

router = APIRouter(prefix="/api/v1", tags=["assistant"])


@router.get("/conversation")
async def history(request: Request, after: int = Query(default=0, ge=0)) -> dict[str, Any]:
    async with authenticated(request) as i:
        await s.conversation(i.connection, i.user_id)
        return {
            "messages": await many(
                i.connection,
                "SELECT id,sequence,role,content,channel,state,"
                "source_type,created_at FROM app.messages WHERE user_id=:u "
                "AND sequence>:after ORDER BY sequence DESC LIMIT 100",
                {"u": i.user_id, "after": after},
            ),
            "runs": await many(
                i.connection,
                "SELECT id,message_id,state,error_code FROM "
                "app.assistant_runs WHERE user_id=:u ORDER BY created_at DESC LIMIT 20",
                {"u": i.user_id},
            ),
        }


@router.post("/conversation/messages", status_code=202)
async def send(data: dto.MessageInput, request: Request) -> dict[str, Any]:
    if not request.app.state.settings.llm_enabled or not request.app.state.settings.llm_model:
        raise HTTPException(
            503, "Модель ещё не подключена. Воспользуйтесь планом или базой знаний."
        )
    async with authenticated(request, mutation=True) as i:
        return await s.submit(i.connection, i.user_id, data.content, f"web:{data.request_key}")


@router.get("/conversation/events")
async def events(request: Request) -> StreamingResponse:
    async with authenticated(request):
        pass

    async def stream():  # type: ignore[no-untyped-def]
        previous = None
        for _ in range(15):
            if await request.is_disconnected():
                return
            try:
                async with authenticated(request) as i:
                    rows = await many(
                        i.connection,
                        "SELECT id,state FROM app.assistant_runs "
                        "WHERE user_id=:u ORDER BY created_at DESC LIMIT 20",
                        {"u": i.user_id},
                    )
                    marker = json.dumps(rows, default=str)
            except HTTPException:
                yield "event: session\ndata: {}\n\n"
                return
            if marker != previous:
                yield f"event: change\ndata: {marker}\n\n"
                previous = marker
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@router.get("/memory")
async def memory(request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as i:
        return await many(
            i.connection,
            "SELECT * FROM app.memories WHERE user_id=:u ORDER BY updated_at DESC LIMIT 100",
            {"u": i.user_id},
        )


@router.post("/memory", status_code=201)
async def remember(data: dto.MemoryInput, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.remember(i.connection, i.user_id, data)


@router.put("/memory/{item}")
async def correct(item: UUID, data: dto.MemoryUpdate, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.remember(i.connection, i.user_id, data, item=item, version=data.version)


@router.delete("/memory/{item}", status_code=204)
async def forget(item: UUID, request: Request, version: int = Query(ge=1)) -> Response:
    async with authenticated(request, mutation=True) as i:
        await s.forget(i.connection, i.user_id, item, version)
    return Response(status_code=204)


@router.get("/usage")
async def usage(request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as i:
        return await many(
            i.connection,
            "SELECT local_day,count(*) AS requests,"
            "sum(coalesce(charged_usd,reserved_usd)) AS cost_usd FROM app.usage_records "
            "WHERE user_id=:u GROUP BY local_day ORDER BY local_day DESC LIMIT 30",
            {"u": i.user_id},
        )
