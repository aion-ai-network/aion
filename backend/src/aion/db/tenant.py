from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@asynccontextmanager
async def tenant_connection(engine: AsyncEngine, user_id: UUID) -> AsyncIterator[AsyncConnection]:
    """Use the same transaction-local tenant boundary in HTTP and future workers."""
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('aion.user_id', :user_id, true)"),
            {"user_id": str(user_id)},
        )
        if not await connection.scalar(text("SELECT app.active_account(:id)"), {"id": user_id}):
            raise PermissionError("Account is not active")
        yield connection
