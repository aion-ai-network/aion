import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from aion import __version__
from aion.api.errors import install_errors
from aion.api.health import router
from aion.db.connection import async_engine
from aion.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    logging.basicConfig(level=config.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = config
        app.state.database = async_engine(config)
        app.state.redis = Redis.from_url(
            config.redis_url.get_secret_value(),
            socket_connect_timeout=config.dependency_timeout_seconds,
            socket_timeout=config.dependency_timeout_seconds,
        )
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.database.dispose()

    app = FastAPI(
        title="AION API", version=__version__, lifespan=lifespan,
        docs_url="/api/docs" if config.environment != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if config.environment != "production" else None,
    )
    install_errors(app)
    app.include_router(router)
    return app
