import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

logger = logging.getLogger(__name__)
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def error_response(
    request: Request, status: int, code: str, message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", uuid4().hex)
    return JSONResponse(
        status_code=status,
        headers={**(headers or {}), "X-Request-ID": request_id},
        content={"error": {"code": code, "message": message, "request_id": request_id}},
    )


def install_errors(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        request.state.request_id = incoming if REQUEST_ID.fullmatch(incoming) else uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exception: HTTPException) -> JSONResponse:
        return error_response(
            request, exception.status_code, f"http_{exception.status_code}",
            str(exception.detail) if isinstance(exception.detail, str) else "Request failed",
            headers=exception.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exception: RequestValidationError) -> JSONResponse:
        return error_response(request, 422, "validation_error", "Invalid request parameters")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exception: Exception) -> JSONResponse:
        logger.error(
            "Unhandled request error: request_id=%s exception_type=%s",
            getattr(request.state, "request_id", "unknown"), type(exception).__name__,
        )
        return error_response(request, 500, "internal_error", "An unexpected error occurred")
