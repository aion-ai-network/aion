import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from aion import __version__
from aion.api.errors import install_errors
from aion.api.health import router
from aion.channels.router import router as channels_router
from aion.channels.telegram import router as telegram_router
from aion.db.connection import async_engine
from aion.modules.assistant.router import router as assistant_router
from aion.modules.identity.operations import router as operations_router
from aion.modules.identity.router import router as identity_router
from aion.modules.knowledge.router import router as knowledge_router
from aion.modules.planning.router import router as planning_router
from aion.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    logging.basicConfig(level=config.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = config
        app.state.password_limiter = asyncio.Semaphore(2)
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
    app.include_router(identity_router)
    app.include_router(planning_router)
    app.include_router(knowledge_router)
    app.include_router(assistant_router)
    app.include_router(channels_router)
    app.include_router(telegram_router)
    app.include_router(operations_router)
    return app
