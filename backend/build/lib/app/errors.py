"""Error taxonomy.

Every failure the API can produce resolves to one of these, and every one
carries a stable machine-readable ``code`` alongside its prose. The prose is
for the person reading the dashboard; the code is what the frontend branches
on, because branching on a message string breaks the first time someone
improves the wording.

Two rules the handlers below enforce:

  Never leak internals. An unhandled exception becomes a generic 500 with a
  request id. The traceback goes to the log, where it is useful, not to a
  browser, where it is a disclosure.

  Always say what to do next. A rate limit reports when to retry; a missing
  record says what was missing. An error the caller cannot act on is only
  marginally better than a hang.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.obs.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    """Base class for failures with a defined shape."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {"code": self.code, "message": self.message, **self.details}
        }
        if request_id:
            payload["error"]["request_id"] = request_id
        return payload


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class InvalidRequest(AppError):
    status_code = 400
    code = "invalid_request"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message, retry_after_seconds=retry_after)
        self.retry_after = retry_after


class ProviderUnavailable(AppError):
    """A data source failed. Distinct from a bug in this application.

    Worth its own code because the caller's response differs: a provider
    outage is worth retrying in a minute, an internal error is not.
    """

    status_code = 503
    code = "provider_unavailable"


class RuntimeUnavailable(AppError):
    """The requested agent runtime cannot run in this deployment."""

    status_code = 409
    code = "runtime_unavailable"


# ----------------------------------------------------------------------
def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        log.warning(
            "api.error",
            code=exc.code,
            status=exc.status_code,
            message=exc.message,
            request_id=request_id,
        )
        headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimited) else None
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(request_id),
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Normalised into the same envelope so the frontend has one shape to
        # parse rather than two that differ by which layer raised.
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": str(exc.detail),
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Flattened to "field: reason". FastAPI's nested loc/msg/ctx structure
        # is precise and unreadable; the dashboard shows this text directly.
        fields = [
            {
                "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
                "problem": err.get("msg", "invalid"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": "; ".join(f"{f['field']}: {f['problem']}" for f in fields),
                    "fields": fields,
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        # Logged with the traceback, returned without it. The request id is
        # what connects the two.
        log.exception("api.unhandled", path=request.url.path, request_id=request_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong. The failure has been logged.",
                    "request_id": request_id,
                }
            },
        )
