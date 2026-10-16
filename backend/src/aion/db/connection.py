from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aion.settings import Settings


def async_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_timeout=settings.dependency_timeout_seconds,
        connect_args={"connect_timeout": int(settings.dependency_timeout_seconds) + 1},
    )


@contextmanager
def sync_connection(settings: Settings) -> Iterator[Connection]:
    engine = create_engine(
        settings.database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"connect_timeout": int(settings.dependency_timeout_seconds) + 1},
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def record_heartbeat(connection: Connection, component: str, instance_id: str) -> None:
    connection.execute(
        text("""
            INSERT INTO app.service_heartbeats (component, instance_id, observed_at)
            VALUES (:component, :instance_id, :observed_at)
            ON CONFLICT (component) DO UPDATE
              SET instance_id = EXCLUDED.instance_id, observed_at = EXCLUDED.observed_at
        """),
        {"component": component, "instance_id": instance_id, "observed_at": datetime.now(UTC)},
    )
