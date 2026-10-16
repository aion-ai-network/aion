import json
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.concurrency import run_in_threadpool

from aion.db.queries import many, one
from aion.db.tenant import tenant_connection
from aion.modules.planning.schemas import Input
from aion.modules.planning.service import enqueue, owned
from aion.providers.storage import BlobStore
from aion.settings import Settings


class ArticleInput(Input):
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(default="", max_length=200000)
    source_text: str = Field(default="", max_length=200000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    project_id: UUID | None = None
    task_id: UUID | None = None
    activity_id: UUID | None = None


class ArticleUpdate(ArticleInput):
    version: int = Field(ge=1)


class UploadInput(Input):
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(default="application/octet-stream", max_length=120)
    size_bytes: int = Field(gt=0, le=10485760)


async def article(c: AsyncConnection, user: UUID, item: UUID) -> dict[str, Any]:
    row = await one(
        c,
        "SELECT id,user_id,title,body,source_text,source_type,source_id,tags,"
        "project_id,task_id,activity_id,version,created_at,updated_at "
        "FROM app.articles WHERE user_id=:u AND id=:id AND deleted_at IS NULL",
        {"u": user, "id": item},
    )
    if row is None:
        raise HTTPException(404, "Статья не найдена.")
    return row


async def save(
    c: AsyncConnection,
    user: UUID,
    data: ArticleInput,
    item: UUID | None = None,
    version: int | None = None,
    source_type: str = "manual",
    source_id: str | None = None,
) -> dict[str, Any]:
    await c.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k,0))"), {"k": f"storage:{user}"}
    )
    used = await c.scalar(
        text("""
      SELECT (SELECT coalesce(sum(octet_length(body)+octet_length(source_text)),0)
        FROM app.articles WHERE user_id=:u AND deleted_at IS NULL
          AND (CAST(:id AS uuid) IS NULL OR id!=:id))+
        (SELECT coalesce(sum(size_bytes),0) FROM app.attachments
          WHERE user_id=:u AND state IN ('uploading','ready'))
    """),
        {"u": user, "id": item},
    )
    limit = await c.scalar(
        text("SELECT storage_limit_mb*1048576 FROM app.user_limits WHERE user_id=:u"), {"u": user}
    )
    if int(used or 0) + len(data.body.encode()) + len(data.source_text.encode()) > int(limit or 0):
        raise HTTPException(413, "Лимит хранилища исчерпан.")
    for field, table in (
        ("project_id", "projects"),
        ("task_id", "tasks"),
        ("activity_id", "activities"),
    ):
        if value := getattr(data, field):
            await owned(c, table, user, value)
    if any(not tag.strip() or len(tag) > 60 for tag in data.tags):
        raise HTTPException(422, "Метки должны содержать от 1 до 60 символов.")
    values = {
        **data.model_dump(exclude={"version"}),
        "u": user,
        "id": item or uuid4(),
        "version": version,
        "source_type": source_type,
        "source_id": source_id,
    }
    if item:
        await article(c, user, item)
        row = await one(
            c,
            """
          UPDATE app.articles SET title=:title,body=:body,source_text=:source_text,tags=:tags,
            project_id=:project_id,task_id=:task_id,activity_id=:activity_id,
            version=version+1,updated_at=now()
          WHERE user_id=:u AND id=:id AND version=:version AND deleted_at IS NULL RETURNING id
        """,
            values,
        )
    else:
        row = await one(
            c,
            """
          INSERT INTO app.articles(id,user_id,title,body,source_text,tags,project_id,task_id,
            activity_id,source_type,source_id)
          VALUES(:id,:u,:title,:body,:source_text,:tags,:project_id,:task_id,:activity_id,
            :source_type,:source_id) RETURNING id
        """,
            values,
        )
    if row is None:
        raise HTTPException(409, "Статья уже изменилась. Обновите её перед сохранением.")
    await c.execute(
        text("""
      INSERT INTO app.article_revisions(id,user_id,article_id,version,title,body,source_text,tags)
      SELECT :revision,user_id,id,version,title,body,source_text,tags FROM app.articles
      WHERE user_id=:u AND id=:id
    """),
        {"revision": uuid4(), "u": user, "id": values["id"]},
    )
    return await article(c, user, values["id"])


async def search(
    c: AsyncConnection,
    user: UUID,
    query: str = "",
    tag: str | None = None,
    project: UUID | None = None,
    activity: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    return await many(
        c,
        """
      SELECT id,title,tags,project_id,task_id,activity_id,version,created_at,updated_at,
        left(body,300) AS preview FROM app.articles
      WHERE user_id=:u AND deleted_at IS NULL
        AND (:q='' OR search_document @@ websearch_to_tsquery('russian',:q))
        AND (CAST(:tag AS text) IS NULL OR :tag=ANY(tags))
        AND (CAST(:project AS uuid) IS NULL OR project_id=:project)
        AND (CAST(:activity AS uuid) IS NULL OR activity_id=:activity)
        AND (CAST(:since AS timestamptz) IS NULL OR created_at>=:since)
        AND (CAST(:until AS timestamptz) IS NULL OR created_at<:until)
      ORDER BY updated_at DESC LIMIT 100
    """,
        {
            "u": user,
            "q": query,
            "tag": tag,
            "project": project,
            "activity": activity,
            "since": since,
            "until": until,
        },
    )


async def reserve(c: AsyncConnection, user: UUID, item: UUID, data: UploadInput) -> dict[str, Any]:
    await article(c, user, item)
    await c.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k,0))"), {"k": f"storage:{user}"}
    )
    remaining = await c.scalar(
        text("""
      SELECT storage_limit_mb*1048576-(SELECT coalesce(sum(size_bytes),0) FROM app.attachments
        WHERE user_id=:u AND state IN ('uploading','ready'))-
        (SELECT coalesce(sum(octet_length(body)+octet_length(source_text)),0) FROM app.articles
          WHERE user_id=:u AND deleted_at IS NULL)
      FROM app.user_limits WHERE user_id=:u
    """),
        {"u": user},
    )
    if data.size_bytes > int(remaining or 0):
        raise HTTPException(413, "Лимит хранилища исчерпан.")
    attachment = uuid4()
    filename = PurePosixPath(data.filename.replace("\\", "/")).name
    if not filename or any(ord(ch) < 32 for ch in filename):
        raise HTTPException(422, "Недопустимое имя файла.")
    row = await one(
        c,
        """
      INSERT INTO app.attachments(id,user_id,article_id,filename,media_type,size_bytes,storage_key)
      VALUES(:id,:u,:article,:filename,:media_type,:size,:key) RETURNING *
    """,
        {
            "id": attachment,
            "u": user,
            "article": item,
            "filename": filename,
            "media_type": data.media_type,
            "size": data.size_bytes,
            "key": f"{user}/{attachment}",
        },
    )
    await enqueue(
        c,
        user,
        "upload_cleanup",
        f"upload:{attachment}",
        {"id": attachment},
        datetime.now(UTC) + timedelta(hours=1),
    )
    assert row
    return row


async def clean_upload(engine: AsyncEngine, settings: Settings, user: UUID, item: UUID) -> None:
    async with tenant_connection(engine, user) as c:
        row = await one(
            c,
            "UPDATE app.attachments SET state='deleted' WHERE user_id=:u "
            "AND id=:id AND state='uploading' RETURNING storage_key",
            {"u": user, "id": item},
        )
    if row:
        await run_in_threadpool(BlobStore(settings).delete, row["storage_key"])


def zip_export(
    store: BlobStore, key: str, articles: list[dict[str, Any]], files: list[dict[str, Any]]
) -> int:
    with tempfile.TemporaryFile() as output:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "metadata.json", json.dumps(articles, ensure_ascii=False, default=str, indent=2)
            )
            archive.writestr(
                "attachments.json", json.dumps(files, ensure_ascii=False, default=str, indent=2)
            )
            for a in articles:
                archive.writestr(f"articles/{a['id']}.md", f"# {a['title']}\n\n{a['body']}")
                archive.writestr(f"sources/{a['id']}.txt", a["source_text"])
            for f in files:
                with archive.open(f"attachments/{f['id']}/{f['filename']}", "w") as target:
                    for chunk in store.read(f["storage_key"]):
                        target.write(chunk)
        size = output.tell()
        output.seek(0)
        store.put(key, output)
        return size


async def build_export(engine: AsyncEngine, settings: Settings, user: UUID, item: UUID) -> None:
    async with tenant_connection(engine, user) as c:
        row = await one(
            c,
            "SELECT * FROM app.exports WHERE user_id=:u AND id=:id "
            "AND state='pending' AND expires_at>now()",
            {"u": user, "id": item},
        )
        if row is None:
            return
        articles = await many(
            c,
            "SELECT id,title,body,source_text,source_type,source_id,tags,"
            "project_id,task_id,activity_id,version,created_at,updated_at "
            "FROM app.articles WHERE user_id=:u AND deleted_at IS NULL",
            {"u": user},
        )
        files = await many(
            c,
            "SELECT f.* FROM app.attachments f JOIN app.articles a ON "
            "a.user_id=f.user_id AND a.id=f.article_id WHERE f.user_id=:u "
            "AND f.state='ready' AND a.deleted_at IS NULL",
            {"u": user},
        )
    key = f"{user}/{item}.zip"
    size = await run_in_threadpool(zip_export, BlobStore(settings), key, articles, files)
    async with tenant_connection(engine, user) as c:
        await c.execute(
            text(
                "UPDATE app.exports SET state='ready',storage_key=:key,size_bytes=:size "
                "WHERE user_id=:u AND id=:id AND state='pending'"
            ),
            {"key": key, "size": size, "u": user, "id": item},
        )
