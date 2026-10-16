"""Operator-only readiness and retention; never prints credentials or private text."""

import argparse
import asyncio
import logging
import sys
from typing import Any

from sqlalchemy import create_engine, text

from aion.channels.delivery import telegram
from aion.providers.storage import BlobStore
from aion.settings import Settings, get_settings


def readiness(settings: Settings) -> dict[str, Any]:
    missing = []
    for name, enabled in (
        ("HTTPS public origin", settings.public_origin.startswith("https://")),
        (
            "runtime DB password",
            settings.db_password.get_secret_value() != "local_runtime_password",
        ),
        (
            "migration DB password",
            settings.db_migration_password
            and settings.db_migration_password.get_secret_value() != "local_migration_password",
        ),
        ("LLM model", settings.llm_enabled and settings.llm_model),
        (
            "Telegram bot",
            settings.telegram_bot_token.get_secret_value()
            and settings.telegram_bot_username
            and settings.telegram_webhook_secret.get_secret_value(),
        ),
        ("speech recognition", settings.stt_enabled and settings.stt_model),
        (
            "Web Push VAPID",
            settings.vapid_public_key
            and settings.vapid_private_key.get_secret_value()
            and settings.vapid_subject,
        ),
        (
            "S3 credentials",
            settings.storage_backend == "s3"
            and settings.s3_access_key.get_secret_value()
            and settings.s3_secret_key.get_secret_value(),
        ),
    ):
        if not enabled:
            missing.append(name)
    return {
        "environment": settings.environment,
        "storage": settings.storage_backend,
        "external_acceptance_pending": missing,
    }


def retain(settings: Settings) -> dict[str, int]:
    """Run daily with migration credentials. Delete objects before marking them removed."""
    store = BlobStore(settings)
    engine = create_engine(settings.migration_database_url, hide_parameters=True)
    counts = {"objects": 0, "articles": 0, "metadata": 0}
    try:
        with engine.begin() as c:
            # Retention is operator work, deliberately outside the tenant runtime role.
            if not c.scalar(text("SELECT pg_try_advisory_xact_lock(6490804)")):
                return counts
            files = (
                c.execute(
                    text("""
              SELECT id,storage_key,'attachment' AS kind FROM app.attachments
                WHERE (state='deleted' OR state='uploading' AND created_at<now()-interval '1 hour'
                  OR article_id IN (SELECT id FROM app.articles
                    WHERE deleted_at<now()-interval '7 days'))
              UNION ALL SELECT id,storage_key,'export' FROM app.exports WHERE state='ready'
                AND expires_at<now() AND storage_key IS NOT NULL
              UNION ALL SELECT id,source_key,'audio' FROM app.messages WHERE source_key IS NOT NULL
                AND source_type='voice' AND created_at<now()-interval '1 day'
            """)
                )
                .mappings()
                .all()
            )
            for row in files:
                store.delete(row["storage_key"])
                counts["objects"] += 1
                if row["kind"] == "attachment":
                    c.execute(
                        text("UPDATE app.attachments SET state='deleted' WHERE id=:id"), dict(row)
                    )
                elif row["kind"] == "export":
                    c.execute(
                        text(
                            "UPDATE app.exports SET state='expired',storage_key=NULL WHERE id=:id"
                        ),
                        dict(row),
                    )
                else:
                    c.execute(
                        text("UPDATE app.messages SET source_key=NULL WHERE id=:id"), dict(row)
                    )
            counts["articles"] = len(
                c.execute(
                    text(
                        "DELETE FROM app.articles WHERE "
                        "deleted_at<now()-interval '7 days' RETURNING id"
                    )
                ).all()
            )
            for query in (
                "DELETE FROM app.telegram_updates WHERE created_at<now()-interval '7 days'",
                "DELETE FROM app.telegram_link_tokens WHERE expires_at<now()-interval '1 day'",
                "DELETE FROM app.background_jobs WHERE state IN ('done','cancelled') "
                "AND finished_at<now()-interval '30 days'",
                "DELETE FROM app.sessions WHERE expires_at<now()-interval '30 days'",
                "DELETE FROM app.usage_records WHERE created_at<now()-interval '90 days'",
                "DELETE FROM app.audio_usage WHERE created_at<now()-interval '90 days'",
            ):
                counts["metadata"] += c.execute(text(query)).rowcount
    finally:
        engine.dispose()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="AION local operations")
    parser.add_argument("command", choices=["readiness", "retention", "telegram-webhook"])
    args = parser.parse_args()
    s = get_settings()
    if args.command == "readiness":
        import json

        print(json.dumps(readiness(s), ensure_ascii=False, indent=2))
    elif args.command == "retention":
        print(retain(s))
    else:
        if not (
            s.public_origin.startswith("https://")
            and s.telegram_webhook_secret.get_secret_value()
            and s.telegram_bot_token.get_secret_value()
        ):
            parser.exit(1, "Configure HTTPS origin, bot token and webhook secret first.\n")
        logging.getLogger("httpx").setLevel(logging.WARNING)

        async def install() -> None:
            await telegram(
                s,
                "setWebhook",
                {
                    "url": s.public_origin + "/api/v1/telegram/webhook",
                    "secret_token": s.telegram_webhook_secret.get_secret_value(),
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": False,
                },
            )

        try:
            with asyncio.Runner(
                loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None
            ) as r:
                r.run(install())
        except Exception as error:
            # HTTP exception strings include the bot token in Telegram's URL.
            parser.exit(1, f"Webhook configuration failed: {type(error).__name__}\n")
        print("Webhook configured.")


if __name__ == "__main__":
    main()
