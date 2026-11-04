"""Uniform API error model + handlers."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_config import request_id_var

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Application error rendered as the standard JSON envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.headers = headers or {}


def not_found(code: str, message: str, **details: Any) -> ApiError:
    return ApiError(code, message, status_code=status.HTTP_404_NOT_FOUND, details=details)


def bad_request(code: str, message: str, **details: Any) -> ApiError:
    return ApiError(code, message, status_code=status.HTTP_400_BAD_REQUEST, details=details)


def conflict(code: str, message: str, **details: Any) -> ApiError:
    return ApiError(code, message, status_code=status.HTTP_409_CONFLICT, details=details)


def unavailable(code: str, message: str, *, retry_after: float | None = None, **details: Any) -> ApiError:
    headers = {"Retry-After": str(int(retry_after) + 1)} if retry_after else None
    return ApiError(
        code,
        message,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        details={**details, "retry_after_seconds": retry_after},
        headers=headers,
    )


def envelope_error(error: ApiError) -> dict[str, Any]:
    import time

    return {
        "data": None,
        "error": {"code": error.code, "message": error.message, "details": error.details},
        "meta": {
            "request_id": request_id_var.get(),
            "server_time": int(time.time()),
            "cache": {"hit": False},
        },
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:  # noqa: ANN202
        return JSONResponse(status_code=exc.status_code, content=envelope_error(exc), headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:  # noqa: ANN202
        if isinstance(exc.detail, dict):
            code = str(exc.detail.get("code", "http_error"))
            message = str(exc.detail.get("message", exc.detail))
            details = {k: v for k, v in exc.detail.items() if k not in ("code", "message")}
        else:
            code = "http_error"
            message = str(exc.detail)
            details = {}
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope_error(ApiError(code, message, status_code=exc.status_code, details=details)),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ANN202
        code = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", status.HTTP_422_UNPROCESSABLE_ENTITY)
        return JSONResponse(
            status_code=code,
            content=envelope_error(
                ApiError(
                    "validation_error",
                    "Request validation failed",
                    status_code=code,
                    details={"errors": exc.errors()[:20]},
                )
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN202
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=envelope_error(ApiError("internal_error", "Internal server error", status_code=500)),
        )
