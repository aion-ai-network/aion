import asyncio
import logging
import sys
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aion.db.connection import async_engine
from aion.db.queries import one
from aion.db.tenant import tenant_connection
from aion.modules.planning.notifications import deliver
from aion.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def handle(
    engine: AsyncEngine, settings: Settings, user: UUID, job: dict[str, Any]
) -> datetime | None:
    if job["kind"] == "notification":
        async with tenant_connection(engine, user) as c:
            # Processing and marking completion share a transaction for local delivery.
            return await deliver(c, user, job["payload"])
    # New modules register their application operations here, not in transport handlers.
    if job["kind"] == "export":
        from aion.modules.knowledge.service import build_export

        await build_export(engine, settings, user, UUID(job["payload"]["id"]))
        return None
    if job["kind"] == "assistant":
        from aion.modules.assistant.service import process_run

        return await process_run(engine, settings, user, UUID(job["payload"]["id"]))
    if job["kind"] == "external_delivery":
        from aion.channels.delivery import send_delivery

        return await send_delivery(engine, settings, user, UUID(job["payload"]["id"]))
    if job["kind"] == "telegram_voice":
        from aion.channels.telegram import transcribe

        await transcribe(engine, settings, user, job["payload"])
        return None
    if job["kind"] == "telegram_document":
        from aion.channels.telegram import download_document

        await download_document(engine, settings, user, job["payload"])
        return None
    if job["kind"] == "upload_cleanup":
        from aion.modules.knowledge.service import clean_upload

        await clean_upload(engine, settings, user, UUID(job["payload"]["id"]))
        return None
    if job["kind"] == "private_cleanup":
        from starlette.concurrency import run_in_threadpool

        from aion.providers.storage import BlobStore

        key = job["payload"]["storage_key"]
        if not key.startswith(f"{user}/"):
            raise ValueError("Wrong storage owner")
        await run_in_threadpool(BlobStore(settings).delete, key)
        async with tenant_connection(engine, user) as c:
            await c.execute(
                text("UPDATE app.messages SET source_key=NULL WHERE user_id=:u AND id=:id"),
                {"u": user, "id": UUID(job["payload"]["message_id"])},
            )
        return None
    if job["kind"] == "private_delete":
        from starlette.concurrency import run_in_threadpool

        from aion.providers.storage import BlobStore

        key = job["payload"]["storage_key"]
        if not key.startswith(f"{user}/"):
            raise ValueError("Wrong storage owner")
        await run_in_threadpool(BlobStore(settings).delete, key)
        return None
    raise ValueError("Unknown job kind")


async def execute(job_id: UUID, user: UUID, token: UUID, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    engine = async_engine(settings)
    try:
        async with tenant_connection(engine, user) as c:
            job = await one(
                c,
                """
              UPDATE app.background_jobs SET state='running'
              WHERE user_id=:u AND id=:id AND lease_token=:token AND state='queued'
                AND lease_until>now() RETURNING *
            """,
                {"u": user, "id": job_id, "token": token},
            )
        if not job:
            return
        try:
            due = await handle(engine, settings, user, job)
            async with tenant_connection(engine, user) as c:
                await c.execute(
                    text("""
                  UPDATE app.background_jobs SET state=:state,finished_at=now(),
                    due_at=coalesce(:due,due_at),lease_until=NULL,error_code=NULL
                  WHERE user_id=:u AND id=:id AND lease_token=:token AND state='running'
                """),
                    {
                        "state": "pending" if due else "done",
                        "due": due,
                        "u": user,
                        "id": job_id,
                        "token": token,
                    },
                )
                if due:
                    # Waiting for quiet hours/limits is not a failed execution attempt.
                    await c.execute(
                        text(
                            "UPDATE app.background_jobs SET attempts=0 "
                            "WHERE user_id=:u AND id=:id AND lease_token=:token"
                        ),
                        {"u": user, "id": job_id, "token": token},
                    )
        except PermissionError:
            return
        except Exception as error:
            logger.error(
                "Background operation failed: kind=%s code=%s", job["kind"], type(error).__name__
            )
            async with tenant_connection(engine, user) as c:
                await c.execute(
                    text("""
                  UPDATE app.background_jobs SET state=CASE WHEN attempts>=5 THEN 'failed'
                    ELSE 'pending' END,due_at=now()+interval '30 seconds',lease_until=NULL,
                    error_code=:code WHERE user_id=:u AND id=:id AND lease_token=:token
                    AND state='running'
                """),
                    {"code": type(error).__name__[:80], "u": user, "id": job_id, "token": token},
                )
    except PermissionError:
        return
    finally:
        await engine.dispose()


def run(job_id: str, user: str, token: str) -> None:
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(execute(UUID(job_id), UUID(user), UUID(token)))
