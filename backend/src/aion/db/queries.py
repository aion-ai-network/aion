from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def one(
    connection: AsyncConnection, sql: str, parameters: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    result = await connection.execute(text(sql), parameters or {})
    row = result.mappings().first()
    return dict(row) if row else None


async def many(
    connection: AsyncConnection, sql: str, parameters: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result = await connection.execute(text(sql), parameters or {})
    return [dict(row) for row in result.mappings()]
