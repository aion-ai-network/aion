import tempfile
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from aion.db.queries import many, one
from aion.modules.identity.service import authenticated
from aion.modules.knowledge import service as s
from aion.modules.planning.service import enqueue
from aion.providers.storage import BlobStore

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


@router.get("/articles")
async def catalog(
    request: Request,
    q: str = Query(default="", max_length=500),
    tag: str | None = None,
    project: UUID | None = None,
    activity: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    async with authenticated(request) as i:
        return await s.search(i.connection, i.user_id, q, tag, project, activity, since, until)


@router.post("/articles", status_code=201)
async def create(data: s.ArticleInput, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.save(i.connection, i.user_id, data)


@router.get("/articles/{item}")
async def read(item: UUID, request: Request) -> dict[str, Any]:
    async with authenticated(request) as i:
        return {
            **await s.article(i.connection, i.user_id, item),
            "attachments": await many(
                i.connection,
                "SELECT id,filename,media_type,size_bytes,"
                "state FROM app.attachments WHERE user_id=:u "
                "AND article_id=:id AND state!='deleted'",
                {"u": i.user_id, "id": item},
            ),
        }


@router.put("/articles/{item}")
async def update(item: UUID, data: s.ArticleUpdate, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.save(i.connection, i.user_id, data, item, data.version)


@router.delete("/articles/{item}", status_code=204)
async def remove(item: UUID, request: Request) -> Response:
    async with authenticated(request, mutation=True) as i:
        await s.article(i.connection, i.user_id, item)
        await i.connection.execute(
            text(
                "UPDATE app.articles SET deleted_at=now(),version=version+1 "
                "WHERE user_id=:u AND id=:id"
            ),
            {"u": i.user_id, "id": item},
        )
    return Response(status_code=204)


@router.get("/articles/{item}/revisions")
async def revisions(item: UUID, request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as i:
        await s.article(i.connection, i.user_id, item)
        return await many(
            i.connection,
            "SELECT version,title,body,source_text,tags,created_at "
            "FROM app.article_revisions WHERE user_id=:u AND article_id=:id "
            "ORDER BY version DESC LIMIT 100",
            {"u": i.user_id, "id": item},
        )


@router.post("/articles/{item}/attachments", status_code=201)
async def reserve(item: UUID, data: s.UploadInput, request: Request) -> dict[str, Any]:
    async with authenticated(request, mutation=True) as i:
        return await s.reserve(i.connection, i.user_id, item, data)


@router.put("/attachments/{item}/content", status_code=204)
async def upload(item: UUID, request: Request) -> Response:
    async with authenticated(request, mutation=True) as i:
        row = await one(
            i.connection,
            "SELECT * FROM app.attachments WHERE user_id=:u AND id=:id AND state='uploading' "
            "AND receiving_at IS NULL",
            {"u": i.user_id, "id": item},
        )
        if row is None:
            raise HTTPException(404, "Загрузка не найдена.")
        await s.article(i.connection, i.user_id, row["article_id"])
        user = i.user_id
    size = 0
    with tempfile.TemporaryFile() as content:
        async for chunk in request.stream():
            size += len(chunk)
            if size > row["size_bytes"]:
                raise HTTPException(413, "Размер превышает объявленный.")
            await run_in_threadpool(content.write, chunk)
        if size != row["size_bytes"]:
            raise HTTPException(422, "Размер файла не совпадает.")
        async with authenticated(request, mutation=True) as i:
            claimed = await one(
                i.connection,
                "UPDATE app.attachments SET receiving_at=now() WHERE user_id=:u "
                "AND id=:id AND state='uploading' AND receiving_at IS NULL RETURNING id",
                {"u": i.user_id, "id": item},
            )
            if not claimed or i.user_id != user:
                raise HTTPException(409, "Загрузка уже обрабатывается.")
        content.seek(0)
        await run_in_threadpool(
            BlobStore(request.app.state.settings).put, row["storage_key"], content
        )
    async with authenticated(request, mutation=True) as i:
        if i.user_id != user:
            raise HTTPException(403, "Аккаунт изменился.")
        await s.article(i.connection, i.user_id, row["article_id"])
        changed = await one(
            i.connection,
            "UPDATE app.attachments SET state='ready' "
            "WHERE user_id=:u AND id=:id AND state='uploading' RETURNING id",
            {"u": i.user_id, "id": item},
        )
        if not changed:
            raise HTTPException(409, "Загрузка уже закрыта.")
    return Response(status_code=204)


@router.get("/attachments/{item}/content")
async def download(item: UUID, request: Request) -> StreamingResponse:
    async with authenticated(request) as i:
        row = await one(
            i.connection,
            "SELECT * FROM app.attachments WHERE user_id=:u AND id=:id AND state='ready'",
            {"u": i.user_id, "id": item},
        )
        if row is None:
            raise HTTPException(404, "Файл не найден.")
        await s.article(i.connection, i.user_id, row["article_id"])
    return StreamingResponse(
        BlobStore(request.app.state.settings).read(row["storage_key"]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["filename"]),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/attachments/{item}", status_code=204)
async def delete_attachment(item: UUID, request: Request) -> Response:
    async with authenticated(request, mutation=True) as i:
        row = await one(
            i.connection,
            "UPDATE app.attachments SET state='deleted' WHERE user_id=:u "
            "AND id=:id AND state!='deleted' RETURNING storage_key",
            {"u": i.user_id, "id": item},
        )
        if not row:
            raise HTTPException(404, "Вложение не найдено.")
        await enqueue(i.connection, i.user_id, "private_delete", f"delete-file:{item}", row)
    return Response(status_code=204)


@router.post("/exports", status_code=202)
async def export(request: Request) -> dict[str, Any] | None:
    async with authenticated(request, mutation=True) as i:
        await i.connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k,0))"),
            {"k": f"export:{i.user_id}"},
        )
        existing = await one(
            i.connection,
            "SELECT * FROM app.exports WHERE user_id=:u AND state='pending' LIMIT 1",
            {"u": i.user_id},
        )
        if existing:
            return existing
        count = await i.connection.scalar(
            text(
                "SELECT count(*) FROM app.exports WHERE user_id=:u "
                "AND created_at>now()-interval '1 day'"
            ),
            {"u": i.user_id},
        )
        if int(count or 0) >= 3:
            raise HTTPException(429, "Можно создать до трёх архивов за сутки.")
        item = uuid4()
        row = await one(
            i.connection,
            "INSERT INTO app.exports(id,user_id) VALUES(:id,:u) RETURNING *",
            {"u": i.user_id, "id": item},
        )
        await enqueue(i.connection, i.user_id, "export", f"export:{item}", {"id": item})
        return row


@router.get("/exports")
async def exports(request: Request) -> list[dict[str, Any]]:
    async with authenticated(request) as i:
        await i.connection.execute(
            text("""
          UPDATE app.exports e SET state='failed' WHERE e.user_id=:u AND e.state='pending'
          AND EXISTS(SELECT 1 FROM app.background_jobs j WHERE j.user_id=e.user_id
            AND j.kind='export' AND j.payload->>'id'=e.id::text AND j.state='failed')
        """),
            {"u": i.user_id},
        )
        return await many(
            i.connection,
            "SELECT id,state,size_bytes,created_at,expires_at "
            "FROM app.exports WHERE user_id=:u ORDER BY created_at DESC LIMIT 20",
            {"u": i.user_id},
        )


@router.get("/exports/{item}/content")
async def export_download(item: UUID, request: Request) -> StreamingResponse:
    async with authenticated(request) as i:
        row = await one(
            i.connection,
            "SELECT * FROM app.exports WHERE user_id=:u AND id=:id "
            "AND state='ready' AND expires_at>now()",
            {"u": i.user_id, "id": item},
        )
        if row is None:
            raise HTTPException(404, "Готовый экспорт не найден или срок скачивания истёк.")
    return StreamingResponse(
        BlobStore(request.app.state.settings).read(row["storage_key"]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="aion-export.zip"'},
    )
