"""Domain acceptance against real PostgreSQL, with deterministic external adapters."""

import io
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from test_identity_integration import member, profile_payload

from aion.db.tenant import tenant_connection
from aion.jobs.runner import execute
from aion.modules.assistant.service import process_run
from aion.modules.planning.notifications import deliver
from aion.modules.planning.service import focus
from aion.providers.llm import Completion

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AION_INTEGRATION") != "1", reason="requires isolated PostgreSQL and Redis"
    ),
]


def check(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def event(client, **changes):
    start = datetime.now(UTC) + timedelta(hours=2)
    return check(
        client.post(
            "/api/v1/activities",
            json={
                "title": "Session",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                **changes,
            },
        ),
        201,
    )


def task(client, **changes):
    return check(
        client.post(
            "/api/v1/tasks", json={"title": "Desired result", "first_step": "Open notes", **changes}
        ),
        201,
    )


def test_plan_versions_local_time_and_cross_owner_links(context):
    with member(context) as (a, account, _), member(context) as (b, _, _):
        p = check(a.post("/api/v1/projects", json={"title": "Project"}), 201)
        t = task(a, project_id=p["id"])
        task(b)
        assert (
            b.post(
                "/api/v1/tasks", json={"title": "Foreign", "first_step": "x", "project_id": p["id"]}
            ).status_code
            == 404
        )
        profile = {**profile_payload(account), "timezone": "Europe/Saratov"}
        check(a.put("/api/v1/profiles/" + account["user"]["id"], json=profile))
        activity = event(
            a, task_id=t["id"], starts_at="2026-11-01T10:00:00", ends_at="2026-11-01T11:00:00"
        )
        assert activity["starts_at"].startswith("2026-11-01T06:00:00")
        fields = {
            k: activity[k]
            for k in (
                "title",
                "notes",
                "task_id",
                "starts_at",
                "ends_at",
                "fixed",
                "keep_on_pause",
                "notification_overrides",
                "version",
            )
        }
        assert b.put("/api/v1/activities/" + activity["id"], json=fields).status_code == 404
        check(a.put("/api/v1/activities/" + activity["id"], json=fields))
        assert a.put("/api/v1/activities/" + activity["id"], json=fields).status_code == 409
        assert len(check(b.get("/api/v1/plan"))["activities"]) == 0
        assert a.post("/api/v1/activities", json={**fields, "version": 1}).status_code == 422


def test_focus_modes_and_elapsed_event_not_success(context):
    with member(context) as (client, account, _):
        for priority in (3, 1, 2):
            task(client, priority=priority)
        old = event(client, starts_at="2026-01-01T10:00Z", ends_at="2026-01-01T11:00Z")
        user = UUID(account["user"]["id"])

        async def read():
            async with tenant_connection(client.app.state.database, user) as c:
                return await focus(c, user, datetime(2026, 10, 16, 10, tzinfo=UTC))

        data = client.portal.call(read)
        assert [t["priority"] for t in data["priorities"]] == [1, 2, 3]
        check(client.put("/api/v1/mode", json={"mode": "light"}))
        assert len(client.portal.call(read)["priorities"]) == 1
        check(client.put("/api/v1/mode", json={"mode": "paused"}))
        assert client.portal.call(read)["priorities"] == []
        assert (
            next(
                a for a in check(client.get("/api/v1/plan"))["activities"] if a["id"] == old["id"]
            )["outcome"]
            == "unknown"
        )


def test_claim_leases_duplicate_execution_and_result_cancels(context):
    with member(context) as (client, account, _):
        user = UUID(account["user"]["id"])
        a = event(client)
        with context[1].begin() as c:
            c.execute(
                text(
                    "UPDATE app.user_profiles SET quiet_start='00:00',quiet_end='00:00' "
                    "WHERE user_id=:u"
                ),
                {"u": user},
            )
            intent = dict(
                c.execute(
                    text(
                        "SELECT * FROM app.notification_intents WHERE user_id=:u "
                        "AND activity_id=:a AND kind='reminder' AND repeat_number=0"
                    ),
                    {"u": user, "a": UUID(a["id"])},
                )
                .mappings()
                .one()
            )
            c.execute(
                text(
                    "UPDATE app.notification_intents SET due_at=now()-interval '1 second' "
                    "WHERE id=:id"
                ),
                {"id": intent["id"]},
            )
            c.execute(
                text(
                    "UPDATE app.background_jobs SET due_at=now()-interval '1 second' "
                    "WHERE user_id=:u AND dedupe_key=:k"
                ),
                {"u": user, "k": f"intent:{intent['id']}"},
            )

        def claim(_):
            with context[1].begin() as c:
                return [
                    dict(r) for r in c.execute(text("SELECT * FROM app.claim_jobs(100)")).mappings()
                ]

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [j for batch in pool.map(claim, range(2)) for j in batch]
        jobs = [j for j in claims if j["user_id"] == user]
        assert len(jobs) == 1
        job = jobs[0]
        client.portal.call(execute, job["id"], user, job["lease_token"], context[0])
        client.portal.call(execute, job["id"], user, job["lease_token"], context[0])
        notifications = check(client.get("/api/v1/notifications"))
        assert len(notifications) == 1
        n = notifications[0]
        check(
            client.post(
                "/api/v1/activities/" + a["id"] + "/result",
                json={
                    "version": a["version"],
                    "intent_id": n["intent_id"],
                    "outcome": "partial",
                    "content": "Notes",
                },
            )
        )
        assert (
            client.post(
                "/api/v1/activities/" + a["id"] + "/result",
                json={"version": a["version"], "intent_id": n["intent_id"], "outcome": "done"},
            ).status_code
            == 409
        )
        with context[1].connect() as c:
            assert (
                c.scalar(
                    text(
                        "SELECT count(*) FROM app.notification_intents WHERE user_id=:u "
                        "AND state='pending'"
                    ),
                    {"u": user},
                )
                == 0
            )


def test_notifications_quiet_pause_rules_and_move(context):
    with member(context) as (client, account, _):
        user = UUID(account["user"]["id"])
        a = event(client)
        with context[1].connect() as c:
            intent = dict(
                c.execute(
                    text(
                        "SELECT * FROM app.notification_intents WHERE user_id=:u "
                        "AND kind='reminder' AND repeat_number=0"
                    ),
                    {"u": user},
                )
                .mappings()
                .one()
            )

        async def send(now):
            async with tenant_connection(client.app.state.database, user) as c:
                return await deliver(
                    c, user, {"activity_id": a["id"], "intent_id": str(intent["id"])}, now
                )

        early = intent["due_at"] + timedelta(seconds=1)
        with context[1].begin() as c:
            c.execute(
                text(
                    "UPDATE app.user_profiles SET timezone='UTC',quiet_start='00:00',"
                    "quiet_end='23:59' WHERE user_id=:u"
                ),
                {"u": user},
            )
        assert client.portal.call(send, early) is not None
        assert check(client.get("/api/v1/notifications")) == []
        fields = {
            k: a[k]
            for k in (
                "title",
                "notes",
                "task_id",
                "starts_at",
                "ends_at",
                "fixed",
                "keep_on_pause",
                "notification_overrides",
                "version",
            )
        }
        check(client.put("/api/v1/activities/" + a["id"], json=fields))
        assert client.portal.call(send, early) is None
        r = check(client.get("/api/v1/notifications/rules"))
        r.pop("user_id")
        r["reminders"] = False
        check(client.put("/api/v1/notifications/rules", json=r))
        assert client.put("/api/v1/notifications/rules", json=r).status_code == 409


def test_articles_search_revisions_files_and_export(context, tmp_path):
    settings = context[0].model_copy(update={"storage_path": str(tmp_path)})
    local = (settings, *context[1:])
    with member(local) as (a, account, _), member(local) as (b, _, _):
        article = check(
            a.post(
                "/api/v1/articles",
                json={
                    "title": "Исследования памяти",
                    "body": "Рабочие заметки",
                    "source_text": "Точный исходный материал",
                    "tags": ["учёба"],
                },
            ),
            201,
        )
        path = "/api/v1/articles/" + article["id"]
        assert len(check(a.get("/api/v1/articles", params={"q": "исследование"}))) == 1
        assert check(b.get("/api/v1/articles", params={"q": "память"})) == []
        assert b.get(path).status_code == 404
        payload = {
            k: article[k]
            for k in (
                "title",
                "body",
                "source_text",
                "tags",
                "project_id",
                "task_id",
                "activity_id",
                "version",
            )
        }
        payload["body"] = "Обновлённый текст"
        check(a.put(path, json=payload))
        assert a.put(path, json=payload).status_code == 409
        assert len(check(a.get(path + "/revisions"))) == 2
        attachment = check(
            a.post(
                path + "/attachments",
                json={"filename": "../notes.txt", "size_bytes": 5, "media_type": "text/plain"},
            ),
            201,
        )
        upload = "/api/v1/attachments/" + attachment["id"] + "/content"
        assert b.put(upload, content=b"hello").status_code == 404
        assert a.put(upload, content=b"longer").status_code == 413
        check(a.put(upload, content=b"hello"), 204)
        assert a.get(upload).content == b"hello"
        assert b.get(upload).status_code == 404
        export = check(a.post("/api/v1/exports"), 202)
        from aion.modules.knowledge.service import build_export

        a.portal.call(
            build_export,
            a.app.state.database,
            settings,
            UUID(account["user"]["id"]),
            UUID(export["id"]),
        )
        archive = "/api/v1/exports/" + export["id"] + "/content"
        assert b.get(archive).status_code == 404
        response = a.get(archive)
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            assert any(name.endswith("/notes.txt") for name in z.namelist())
            assert all(".." not in name for name in z.namelist())
            assert (
                "Точный исходный материал" in z.read("sources/" + article["id"] + ".txt").decode()
            )
        assert b.delete("/api/v1/attachments/" + attachment["id"]).status_code == 404
        check(a.delete("/api/v1/attachments/" + attachment["id"]), 204)
        assert a.get(upload).status_code == 404
        check(a.delete(path), 204)
        assert a.get(upload).status_code == 404
        assert check(a.get("/api/v1/articles", params={"q": "исследование"})) == []


def test_assistant_atomic_commands_idempotency_memory_and_quota(context):
    settings = context[0].model_copy(
        update={"llm_enabled": True, "llm_model": "local-test", "llm_daily_budget_usd": 10}
    )
    with member((settings, *context[1:])) as (client, account, _):
        user = UUID(account["user"]["id"])
        content = {"content": "Save memory and create a task", "request_key": str(uuid4())}
        message = check(client.post("/api/v1/conversation/messages", json=content), 202)
        assert (
            check(client.post("/api/v1/conversation/messages", json=content), 202)["id"]
            == message["id"]
        )
        with context[1].connect() as c:
            run = c.scalar(
                text("SELECT id FROM app.assistant_runs WHERE message_id=:m"),
                {"m": UUID(message["id"])},
            )
        calls = []

        async def provider(config, messages):
            calls.append(messages)
            return Completion(
                {
                    "reply": "Saved",
                    "operations": [
                        {
                            "operation": "create_task",
                            "data": {"title": "Essay", "first_step": "Open editor"},
                        },
                        {"operation": "remember", "data": {"content": "Prefers mornings"}},
                    ],
                },
                100,
                100,
            )

        client.portal.call(process_run, client.app.state.database, settings, user, run, provider)
        client.portal.call(process_run, client.app.state.database, settings, user, run, provider)
        assert len(calls) == 1
        assert len(check(client.get("/api/v1/plan"))["tasks"]) == 1
        memory = check(client.get("/api/v1/memory"))[0]
        assert memory["source_message_id"] == message["id"]
        check(
            client.put(
                "/api/v1/memory/" + memory["id"], json={"content": "Corrected", "version": 1}
            )
        )
        assert client.delete("/api/v1/memory/" + memory["id"] + "?version=1").status_code == 409
        check(client.delete("/api/v1/memory/" + memory["id"] + "?version=2"), 204)
        # A valid first operation followed by an invalid reference rolls back the entire batch.
        bad = check(
            client.post(
                "/api/v1/conversation/messages",
                json={"content": "Invalid batch", "request_key": str(uuid4())},
            ),
            202,
        )
        with context[1].connect() as c:
            run = c.scalar(
                text("SELECT id FROM app.assistant_runs WHERE message_id=:m"),
                {"m": UUID(bad["id"])},
            )

        async def invalid(config, messages):
            return Completion(
                {
                    "reply": "Should never appear",
                    "operations": [
                        {"operation": "remember", "data": {"content": "Must roll back"}},
                        {
                            "operation": "create_task",
                            "data": {"title": "Bad", "first_step": "x", "project_id": str(uuid4())},
                        },
                    ],
                },
                100,
                100,
            )

        client.portal.call(process_run, client.app.state.database, settings, user, run, invalid)
        assert check(client.get("/api/v1/memory")) == []
        assert not any(
            m["content"] == "Should never appear"
            for m in check(client.get("/api/v1/conversation"))["messages"]
        )
        check(
            context[3].put(
                "/api/v1/admin/users/" + str(user) + "/limits",
                json={"ai_daily_requests": 0, "audio_daily_seconds": 0, "storage_limit_mb": 0},
            )
        )
        capped = check(
            client.post(
                "/api/v1/conversation/messages",
                json={"content": "Over quota", "request_key": str(uuid4())},
            ),
            202,
        )
        with context[1].connect() as c:
            run = c.scalar(
                text("SELECT id FROM app.assistant_runs WHERE message_id=:m"),
                {"m": UUID(capped["id"])},
            )
        client.portal.call(process_run, client.app.state.database, settings, user, run, provider)
        assert len(calls) == 1


def test_telegram_binding_webhook_dedupe_and_shared_history(context):
    settings = context[0].model_copy(
        update={
            "telegram_bot_token": SecretStr("test-token"),
            "telegram_bot_username": "local_test_bot",
            "telegram_webhook_secret": SecretStr("test-secret"),
        }
    )
    with member((settings, *context[1:])) as (client, account, _):
        tg = int(uuid4().int % 1000000000) + 1000
        link = check(client.post("/api/v1/telegram/link"), 201)
        token = link["link"].split("start=")[1]
        update = {
            "update_id": int(uuid4().int % 1000000000000),
            "message": {
                "from": {"id": tg},
                "chat": {"id": tg, "type": "private"},
                "text": "/start " + token,
            },
        }
        headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
        assert client.post("/api/v1/telegram/webhook", json=update).status_code == 403
        check(client.post("/api/v1/telegram/webhook", json=update, headers=headers))
        check(client.post("/api/v1/telegram/link/" + link["id"] + "/confirm"))
        update["update_id"] += 1
        update["message"]["text"] = "Shared message"
        for _ in range(2):
            check(client.post("/api/v1/telegram/webhook", json=update, headers=headers))
        messages = check(client.get("/api/v1/conversation"))["messages"]
        assert len(messages) == 1 and messages[0]["channel"] == "telegram"
        check(client.delete("/api/v1/telegram/link"), 204)
        update["update_id"] += 1
        check(client.post("/api/v1/telegram/webhook", json=update, headers=headers))
        assert len(check(client.get("/api/v1/conversation"))["messages"]) == 1


def test_runtime_rls_on_every_domain_table(context):
    with member(context) as (client, account, _):
        user = UUID(account["user"]["id"])
        task(client)
        event(client)
        check(client.post("/api/v1/articles", json={"title": "Private"}), 201)
        check(client.post("/api/v1/memory", json={"content": "Private"}), 201)

        async def inspect():
            engine = client.app.state.database
            async with engine.begin() as c:
                for table in (
                    "projects",
                    "tasks",
                    "activities",
                    "notification_intents",
                    "background_jobs",
                    "articles",
                    "article_revisions",
                    "attachments",
                    "exports",
                    "messages",
                    "memories",
                    "usage_records",
                    "telegram_links",
                    "push_subscriptions",
                ):
                    assert await c.scalar(text(f"SELECT count(*) FROM app.{table}")) == 0
            async with tenant_connection(engine, user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.tasks")) == 1

        client.portal.call(inspect)


def test_expired_job_lease_is_reclaimed_but_old_token_cannot_run(context):
    with member(context) as (client, account, _):
        user = UUID(account["user"]["id"])
        event(client)
        with context[1].begin() as c:
            job = dict(
                c.execute(
                    text("SELECT * FROM app.background_jobs WHERE user_id=:u LIMIT 1"), {"u": user}
                )
                .mappings()
                .one()
            )
            old = uuid4()
            c.execute(
                text(
                    "UPDATE app.background_jobs SET state='running',attempts=1,"
                    "lease_token=:old,lease_until=now()-interval '1 second',due_at=now() "
                    "WHERE id=:id"
                ),
                {"old": old, "id": job["id"]},
            )
            claimed = [
                dict(r) for r in c.execute(text("SELECT * FROM app.claim_jobs(100)")).mappings()
            ]
            current = next(j for j in claimed if j["id"] == job["id"])
            assert current["lease_token"] != old
        client.portal.call(execute, job["id"], user, old, context[0])
        with context[1].connect() as c:
            assert (
                c.scalar(
                    text("SELECT state FROM app.background_jobs WHERE id=:id"), {"id": job["id"]}
                )
                == "queued"
            )


def test_external_timeout_is_uncertain_and_not_blindly_retried(context, monkeypatch):
    import aion.channels.delivery as module

    settings = context[0].model_copy(update={"telegram_bot_token": SecretStr("synthetic")})
    with member((settings, *context[1:])) as (client, account, _):
        user = UUID(account["user"]["id"])
        item = uuid4()
        conv = uuid4()
        msg = uuid4()
        with context[1].begin() as c:
            c.execute(
                text("INSERT INTO app.conversations(id,user_id) VALUES(:id,:u)"),
                {"id": conv, "u": user},
            )
            c.execute(
                text(
                    "INSERT INTO app.messages(id,user_id,conversation_id,role,content,channel,"
                    "request_key) VALUES(:id,:u,:conv,'assistant','Reply','telegram','answer:test')"
                ),
                {"id": msg, "u": user, "conv": conv},
            )
            c.execute(
                text(
                    "INSERT INTO app.deliveries(id,user_id,channel,external_id) "
                    "VALUES(:id,:u,'telegram','answer:test')"
                ),
                {"id": item, "u": user},
            )
            tg = int(uuid4().int % 1000000000000)
            c.execute(
                text(
                    "INSERT INTO app.telegram_links(user_id,telegram_id,chat_id) VALUES(:u,:tg,:tg)"
                ),
                {"u": user, "tg": tg},
            )
        calls = []

        async def timeout(*args):
            calls.append(1)
            raise TimeoutError()

        monkeypatch.setattr(module, "telegram", timeout)
        for _ in range(2):
            client.portal.call(
                module.send_delivery, client.app.state.database, settings, user, item
            )
        assert len(calls) == 1
        with context[1].connect() as c:
            assert (
                c.scalar(text("SELECT state FROM app.deliveries WHERE id=:id"), {"id": item})
                == "uncertain"
            )


def test_voice_transcription_and_document_use_private_storage(context, tmp_path, monkeypatch):
    import httpx

    import aion.channels.telegram as module

    settings = context[0].model_copy(
        update={
            "storage_path": str(tmp_path),
            "stt_enabled": True,
            "stt_model": "test-stt",
            "telegram_bot_token": SecretStr("synthetic"),
            "telegram_webhook_secret": SecretStr("synthetic-secret"),
        }
    )
    with member((settings, *context[1:])) as (client, account, _):
        user = UUID(account["user"]["id"])
        tg = int(uuid4().int % 10000000000)
        with context[1].begin() as c:
            c.execute(
                text(
                    "INSERT INTO app.telegram_links(user_id,telegram_id,chat_id) VALUES(:u,:tg,:tg)"
                ),
                {"u": user, "tg": tg},
            )
        voice = {
            "update_id": int(uuid4().int % 1000000000000),
            "message": {
                "from": {"id": tg},
                "chat": {"id": tg, "type": "private"},
                "voice": {"file_id": "synthetic", "file_size": 5, "duration": 10},
            },
        }
        check(
            client.post(
                "/api/v1/telegram/webhook",
                json=voice,
                headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic-secret"},
            )
        )
        with context[1].connect() as c:
            payload = c.scalar(
                text(
                    "SELECT payload FROM app.background_jobs WHERE user_id=:u "
                    "AND kind='telegram_voice'"
                ),
                {"u": user},
            )

        async def file_info(*args):
            return {"file_path": "voice/test.ogg"}

        monkeypatch.setattr(module, "telegram", file_info)
        calls = []

        def vendor(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/audio/transcriptions"):
                return httpx.Response(200, json={"text": "Exact spoken text"})
            return httpx.Response(200, content=b"hello")

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            module.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(vendor)),
        )
        client.portal.call(module.transcribe, client.app.state.database, settings, user, payload)
        client.portal.call(module.transcribe, client.app.state.database, settings, user, payload)
        history = check(client.get("/api/v1/conversation"))["messages"]
        assert history[0]["content"] == "Exact spoken text" and history[0]["state"] == "ready"
        assert sum(p.endswith("/audio/transcriptions") for p in calls) == 1
        assert (tmp_path / payload["storage_key"]).read_bytes() == b"hello"
        document = {
            **voice,
            "update_id": voice["update_id"] + 1,
            "message": {
                "from": {"id": tg},
                "chat": {"id": tg, "type": "private"},
                "document": {
                    "file_id": "synthetic-doc",
                    "file_name": "note.txt",
                    "file_size": 5,
                    "mime_type": "text/plain",
                },
            },
        }
        check(
            client.post(
                "/api/v1/telegram/webhook",
                json=document,
                headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic-secret"},
            )
        )
        with context[1].connect() as c:
            payload = c.scalar(
                text(
                    "SELECT payload FROM app.background_jobs WHERE user_id=:u "
                    "AND kind='telegram_document'"
                ),
                {"u": user},
            )
        client.portal.call(
            module.download_document, client.app.state.database, settings, user, payload
        )
        file = client.get("/api/v1/attachments/" + payload["attachment_id"] + "/content")
        assert file.status_code == 200 and file.content == b"hello"


def test_notification_limit_snooze_and_exact_activity_answer(context):
    with member(context) as (client, account, _):
        user = UUID(account["user"]["id"])
        a = event(client)
        b = event(client)
        with context[1].begin() as c:
            c.execute(
                text(
                    "UPDATE app.user_profiles SET quiet_start='00:00',quiet_end='00:00' "
                    "WHERE user_id=:u"
                ),
                {"u": user},
            )
            c.execute(
                text("UPDATE app.notification_rules SET daily_limit=1 WHERE user_id=:u"),
                {"u": user},
            )
            intents = [
                dict(r)
                for r in c.execute(
                    text(
                        "SELECT * FROM app.notification_intents "
                        "WHERE user_id=:u AND kind='reminder' "
                        "AND repeat_number=0 ORDER BY activity_id"
                    ),
                    {"u": user},
                ).mappings()
            ]
            c.execute(
                text(
                    "UPDATE app.notification_intents SET due_at=now()-interval '1 second' "
                    "WHERE user_id=:u"
                ),
                {"u": user},
            )

        async def send(intent):
            async with tenant_connection(client.app.state.database, user) as c:
                return await deliver(
                    c,
                    user,
                    {"activity_id": str(intent["activity_id"]), "intent_id": str(intent["id"])},
                )

        assert client.portal.call(send, intents[0]) is None
        assert client.portal.call(send, intents[1]) is not None
        n = check(client.get("/api/v1/notifications"))[0]
        wrong = b if n["activity_id"] == a["id"] else a
        assert (
            client.post(
                "/api/v1/activities/" + wrong["id"] + "/result",
                json={"version": 1, "intent_id": n["intent_id"], "outcome": "done"},
            ).status_code
            == 409
        )
        check(
            client.post("/api/v1/notifications/" + n["intent_id"] + "/later", json={"minutes": 60})
        )
        assert check(client.get("/api/v1/notifications"))[0]["state"] == "pending"


def test_start_reminder_and_repeated_schedule_are_idempotent(context):
    from aion.modules.planning.service import schedule

    with member(context) as (client, account, _):
        user = UUID(account["user"]["id"])
        r = check(client.get("/api/v1/notifications/rules"))
        r.pop("user_id")
        check(
            client.put(
                "/api/v1/notifications/rules",
                json={**r, "lead_minutes": 0, "repeat_count": 0, "results": False},
            )
        )
        a = event(client, starts_at=(datetime.now(UTC) - timedelta(seconds=10)).isoformat())
        with context[1].begin() as c:
            c.execute(
                text(
                    "UPDATE app.user_profiles SET quiet_start='00:00',quiet_end='00:00' "
                    "WHERE user_id=:u"
                ),
                {"u": user},
            )

        async def verify():
            async with tenant_connection(client.app.state.database, user) as c:
                activity = dict(
                    (
                        await c.execute(
                            text("SELECT * FROM app.activities WHERE user_id=:u"), {"u": user}
                        )
                    )
                    .mappings()
                    .one()
                )
                await schedule(c, user, activity)
                assert (
                    await c.scalar(
                        text("SELECT count(*) FROM app.background_jobs WHERE user_id=:u"),
                        {"u": user},
                    )
                    == 1
                )
                intent = (
                    await c.execute(
                        text("SELECT id FROM app.notification_intents WHERE user_id=:u"),
                        {"u": user},
                    )
                ).scalar_one()
                await deliver(c, user, {"activity_id": a["id"], "intent_id": str(intent)})

        client.portal.call(verify)
        assert len(check(client.get("/api/v1/notifications"))) == 1
