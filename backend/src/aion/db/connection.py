from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import Connection, create_engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aion.db.models import ServiceHeartbeat
from aion.settings import Settings


def async_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=settings.dependency_timeout_seconds,
        connect_args={"connect_timeout": int(settings.dependency_timeout_seconds) + 1},
    )


@contextmanager
def sync_connection(settings: Settings) -> Iterator[Connection]:
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": int(settings.dependency_timeout_seconds) + 1},
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def record_heartbeat(connection: Connection, component: str, instance_id: str) -> None:
    statement = insert(ServiceHeartbeat).values(
        component=component, instance_id=instance_id, observed_at=datetime.now(UTC)
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[ServiceHeartbeat.component],
            set_={"instance_id": statement.excluded.instance_id,
                  "observed_at": statement.excluded.observed_at},
        )
    )
