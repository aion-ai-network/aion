import hmac
import json
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.concurrency import run_in_threadpool

from aion.channels.delivery import telegram
from aion.db.queries import one
from aion.db.tenant import tenant_connection
from aion.modules.assistant.service import finish, submit
from aion.modules.identity.security import token_hash
from aion.modules.knowledge.service import ArticleInput, UploadInput, reserve, save
from aion.modules.planning.schemas import ResultInput
from aion.modules.planning.service import enqueue, result
from aion.providers.storage import BlobStore
from aion.settings import Settings

router = APIRouter(tags=["telegram"])


@router.post("/api/v1/telegram/webhook")
async def webhook(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    secret = settings.telegram_webhook_secret.get_secret_value()
    if not secret or not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret
    ):
        raise HTTPException(403, "Webhook rejected")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 1048576:
            raise HTTPException(413, "Update too large")
    try:
        update = json.loads(content)
        update_id = int(update["update_id"])
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback["from"] if callback else message.get("from", {})
        chat = message.get("chat", {})
        telegram_id, chat_id = int(sender["id"]), int(chat["id"])
    except (ValueError, TypeError, KeyError):
        return {"ok": True}
    if chat.get("type") != "private" or sender.get("is_bot"):
        return {"ok": True}
    async with request.app.state.database.begin() as c:
        fresh = await c.scalar(text("SELECT app.telegram_dedupe(:id)"), {"id": update_id})
        if not fresh:
            return {"ok": True}
        text_value = str(message.get("text", ""))
        if text_value.startswith("/start "):
            await c.execute(
                text("SELECT app.telegram_offer(:hash,:tg,:chat)"),
                {
                    "hash": token_hash(text_value.split(" ", 1)[1]),
                    "tg": telegram_id,
                    "chat": chat_id,
                },
            )
            return {"ok": True}
        user = await c.scalar(
            text("SELECT app.telegram_owner(:tg,:chat)"), {"tg": telegram_id, "chat": chat_id}
        )
        if user is None:
            return {"ok": True}
        await c.execute(text("SELECT set_config('aion.user_id',:u,true)"), {"u": str(user)})
        if not await c.scalar(text("SELECT app.active_account(:u)"), {"u": user}):
            return {"ok": True}
        if callback:
            try:
                intent_id, action = callback["data"].split(":", 1)
                intent = await one(
                    c,
                    "SELECT * FROM app.notification_intents WHERE user_id=:u AND id=:id",
                    {"u": user, "id": UUID(intent_id)},
                )
                if intent and action in ("done", "partial", "cancelled"):
                    # A stale callback is acknowledged, never applied to a different event.
                    async with c.begin_nested():
                        await result(
                            c,
                            user,
                            intent["activity_id"],
                            ResultInput(
                                version=intent["activity_version"],
                                outcome=action,
                                intent_id=UUID(intent_id),
                            ),
                        )
            except (ValueError, KeyError, HTTPException):
                pass
            return {"ok": True}
        voice = message.get("voice")
        if voice:
            duration, size = int(voice.get("duration", 0)), int(voice.get("file_size", 0))
            if not (
                settings.stt_enabled
                and settings.stt_model
                and 0 < duration <= 600
                and 0 < size <= 10485760
            ):
                await submit(
                    c,
                    user,
                    "[Голос не принят: распознавание не настроено или превышен лимит.]",
                    f"telegram:{update_id}",
                    "telegram",
                )
                return {"ok": True}
            audio_id = uuid4()
            allowed = await c.scalar(
                text("SELECT app.reserve_audio(:id,:seconds,:cap)"),
                {"id": audio_id, "seconds": duration, "cap": settings.stt_global_daily_seconds},
            )
            if not allowed:
                await submit(
                    c,
                    user,
                    "[Дневной лимит голосового ввода исчерпан.]",
                    f"telegram:{update_id}",
                    "telegram",
                )
                return {"ok": True}
            row = await submit(
                c,
                user,
                "[Голос: ожидает расшифровки]",
                f"telegram:{update_id}",
                "telegram",
                "transcribing",
                f"{user}/{audio_id}.ogg",
            )
            await enqueue(
                c,
                user,
                "telegram_voice",
                f"voice:{audio_id}",
                {
                    "message_id": row["id"],
                    "file_id": voice["file_id"],
                    "storage_key": f"{user}/{audio_id}.ogg",
                    "size": size,
                },
            )
            await enqueue(
                c,
                user,
                "private_cleanup",
                f"audio-cleanup:{row['id']}",
                {"storage_key": f"{user}/{audio_id}.ogg", "message_id": row["id"]},
                datetime.now(UTC) + timedelta(hours=24),
            )
        elif text_value.strip():
            await submit(c, user, text_value[:12000], f"telegram:{update_id}", "telegram")
        elif message.get("document"):
            document = message["document"]
            size = int(document.get("file_size", 0))
            if not 0 < size <= 10485760:
                return {"ok": True}
            a = await save(
                c,
                user,
                ArticleInput(
                    title=str(document.get("file_name", "Документ"))[:240],
                    source_text=str(message.get("caption", ""))[:200000],
                ),
                source_type="telegram",
                source_id=str(update_id),
            )
            try:
                async with c.begin_nested():
                    attachment = await reserve(
                        c,
                        user,
                        a["id"],
                        UploadInput(
                            filename=str(document.get("file_name", "document"))[:240],
                            size_bytes=size,
                            media_type=str(document.get("mime_type", "application/octet-stream"))[
                                :120
                            ],
                        ),
                    )
            except HTTPException:
                return {"ok": True}
            await enqueue(
                c,
                user,
                "telegram_document",
                f"document:{update_id}",
                {
                    "attachment_id": attachment["id"],
                    "file_id": document["file_id"],
                    "storage_key": attachment["storage_key"],
                    "size": size,
                },
            )
            await submit(
                c,
                user,
                f"[Документ добавлен в статью /knowledge/{a['id']}. "
                "Файл загружается; содержимое автоматически не прочитано.]",
                f"telegram:{update_id}",
                "telegram",
            )
    return {"ok": True}


async def transcribe(
    engine: AsyncEngine, settings: Settings, user: UUID, payload: dict[str, Any]
) -> None:
    async with tenant_connection(engine, user) as c:
        message = await one(
            c,
            "SELECT * FROM app.messages WHERE user_id=:u AND id=:id "
            "AND state IN ('transcribing','recognizing') FOR UPDATE",
            {"u": user, "id": UUID(payload["message_id"])},
        )
        if not message:
            return
        if message["state"] == "recognizing":
            run = await one(
                c,
                "SELECT * FROM app.assistant_runs WHERE user_id=:u AND message_id=:id",
                {"u": user, "id": message["id"]},
            )
            assert run
            await c.execute(
                text(
                    "UPDATE app.messages SET state='failed',source_key=NULL "
                    "WHERE user_id=:u AND id=:id"
                ),
                {"u": user, "id": message["id"]},
            )
            await finish(
                c,
                user,
                run,
                message,
                "Распознавание прервалось. Отправьте голос ещё раз.",
                "interrupted",
            )
            return
        await c.execute(
            text("UPDATE app.messages SET state='recognizing' WHERE user_id=:u AND id=:id"),
            {"u": user, "id": message["id"]},
        )
    store = BlobStore(settings)
    try:
        info = await telegram(settings, "getFile", {"file_id": payload["file_id"]})
        path = str(info["file_path"])
        if ".." in path or path.startswith("/"):
            raise ValueError("Invalid voice path")
        token = settings.telegram_bot_token.get_secret_value()
        with tempfile.TemporaryFile() as source:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                async with client.stream(
                    "GET", f"https://api.telegram.org/file/bot{token}/{path}"
                ) as r:
                    r.raise_for_status()
                    size = 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > min(payload["size"], 10485760):
                            raise ValueError("Audio too large")
                        await run_in_threadpool(source.write, chunk)
                source.seek(0)
                await run_in_threadpool(store.put, payload["storage_key"], source)
                source.seek(0)
                headers = {}
                if key := settings.stt_api_key.get_secret_value():
                    headers["Authorization"] = f"Bearer {key}"
                response = await client.post(
                    settings.stt_base_url.rstrip("/") + "/audio/transcriptions",
                    headers=headers,
                    data={"model": settings.stt_model},
                    files={"file": ("voice.ogg", source, "audio/ogg")},
                )
                response.raise_for_status()
                transcript = str(response.json()["text"]).strip()
                if not transcript or len(transcript) > 12000:
                    raise ValueError("Invalid transcript")
        async with tenant_connection(engine, user) as c:
            await c.execute(
                text(
                    "UPDATE app.messages SET content=:text,state='ready' WHERE user_id=:u "
                    "AND id=:id AND state='recognizing'"
                ),
                {"text": transcript, "u": user, "id": message["id"]},
            )
            await enqueue(
                c,
                user,
                "private_cleanup",
                f"audio-cleanup:{message['id']}",
                {"storage_key": payload["storage_key"], "message_id": message["id"]},
                datetime.now(UTC) + timedelta(hours=24),
            )
    except Exception as error:
        await run_in_threadpool(store.delete, payload["storage_key"])
        async with tenant_connection(engine, user) as c:
            await c.execute(
                text(
                    "UPDATE app.messages SET state='failed',content=:text,source_key=NULL "
                    "WHERE user_id=:u AND id=:id"
                ),
                {"text": "[Не удалось расшифровать голос]", "u": user, "id": message["id"]},
            )
            run = await one(
                c,
                "SELECT * FROM app.assistant_runs WHERE user_id=:u AND message_id=:id",
                {"u": user, "id": message["id"]},
            )
            assert run
            await finish(
                c,
                user,
                run,
                message,
                "Не удалось распознать голос. Отправьте текст.",
                type(error).__name__[:80],
            )


async def download_document(
    engine: AsyncEngine, settings: Settings, user: UUID, payload: dict[str, Any]
) -> None:
    async with tenant_connection(engine, user) as c:
        row = await one(
            c,
            "SELECT * FROM app.attachments WHERE user_id=:u AND id=:id AND state='uploading'",
            {"u": user, "id": UUID(payload["attachment_id"])},
        )
        if not row:
            return
    info = await telegram(settings, "getFile", {"file_id": payload["file_id"]})
    path = str(info["file_path"])
    if ".." in path or path.startswith("/"):
        raise ValueError("Invalid file path")
    token = settings.telegram_bot_token.get_secret_value()
    with tempfile.TemporaryFile() as source:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            async with client.stream(
                "GET", f"https://api.telegram.org/file/bot{token}/{path}"
            ) as r:
                r.raise_for_status()
                size = 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > payload["size"]:
                        raise ValueError("Document too large")
                    await run_in_threadpool(source.write, chunk)
        if size != payload["size"]:
            raise ValueError("Document size mismatch")
        source.seek(0)
        await run_in_threadpool(BlobStore(settings).put, payload["storage_key"], source)
    async with tenant_connection(engine, user) as c:
        await c.execute(
            text(
                "UPDATE app.attachments SET state='ready' WHERE user_id=:u AND id=:id "
                "AND state='uploading'"
            ),
            {"u": user, "id": UUID(payload["attachment_id"])},
        )
