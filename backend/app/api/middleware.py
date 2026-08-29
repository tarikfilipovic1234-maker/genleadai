"""Request correlation and rate limiting.

The rate limiter is in-process and keyed by client address, which is the
right shape for this deployment and the wrong shape for a larger one. Say so
plainly: a single free-tier instance has one process, so a dictionary is an
accurate view of all traffic. Run two instances and each enforces its own
half of the limit, at which point the counter belongs in Redis. Reaching for
Redis now would add an external dependency to a system that has none, to
solve a problem this deployment does not have.

What it does protect against is real. The public instance replays recorded
runs, but each replay is a background task holding a database connection and
an event bus, and the endpoint is one unauthenticated POST away from anyone
who finds the URL.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.errors import RateLimited
from app.obs.logging import get_logger

log = get_logger(__name__)

# Starting a run is expensive; reading is not. Separate budgets so a dashboard
# that polls cannot exhaust the allowance for actually doing work.
EXPENSIVE_LIMIT = (5, 300)  # 5 runs per 5 minutes
GENERAL_LIMIT = (240, 60)  # 240 reads per minute

# Streams are long-lived by design and must never be counted: a single run
# holds one open for minutes, and rate-limiting it would sever the connection
# it is meant to protect.
EXEMPT_SUFFIXES = ("/stream",)


class SlidingWindowLimiter:
    """A sliding-window counter.

    Chosen over a fixed window because a fixed one lets a caller spend the
    whole allowance at 11:59:59 and the whole next allowance at 12:00:00 -
    twice the intended burst, at the least convenient moment.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(
        self, key: str, limit: int, window_seconds: int, now: float | None = None
    ) -> int | None:
        """Return None if allowed, or seconds until the caller may retry.

        ``now`` is injectable so the sliding behaviour can be tested without
        sleeping - a test that waits a real minute to prove a window expires
        will be deleted by whoever next runs the suite.
        """
        now = time.monotonic() if now is None else now
        hits = self._hits[key]

        cutoff = now - window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()

        if len(hits) >= limit:
            return max(1, int(hits[0] + window_seconds - now) + 1)

        hits.append(now)
        return None

    def prune(self, older_than: float = 3600.0) -> None:
        """Drop idle keys so the map does not grow for the process lifetime."""
        cutoff = time.monotonic() - older_than
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            del self._hits[key]


limiter = SlidingWindowLimiter()


def client_key(request: Request) -> str:
    """Identify the caller.

    X-Forwarded-For is trusted because this deployment always sits behind a
    platform proxy that sets it. On a directly-exposed server the header is
    caller-controlled and trusting it would make the limiter trivially
    bypassable, so this assumption belongs in a comment rather than in
    silence.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns each request an id and records how long it took."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)

        response.headers["X-Request-ID"] = request_id
        # Streams complete only when the run does, so their duration measures
        # the run rather than the handler and would distort the latency log.
        if not request.url.path.endswith(EXEMPT_SUFFIXES):
            log.info(
                "api.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                request_id=request_id,
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path

        if path.endswith(EXEMPT_SUFFIXES) or not path.startswith("/api"):
            return await call_next(request)

        key = client_key(request)
        starting_a_run = request.method == "POST" and path.rstrip("/").endswith("/tasks")
        limit, window = EXPENSIVE_LIMIT if starting_a_run else GENERAL_LIMIT

        # Namespaced so a burst of reads cannot consume the run allowance.
        retry_after = limiter.check(f"{'run' if starting_a_run else 'read'}:{key}", limit, window)
        if retry_after is not None:
            log.warning("api.rate_limited", key=key, path=path, retry_after=retry_after)
            # Returned, not raised. Starlette's BaseHTTPMiddleware wraps the
            # application, and FastAPI's exception handlers run inside it - so
            # an exception raised here escapes past them and surfaces as an
            # unhandled 500 rather than the 429 it is.
            error = RateLimited(
                f"Too many requests. Try again in {retry_after} seconds.",
                retry_after=retry_after,
            )
            return JSONResponse(
                status_code=error.status_code,
                content=error.to_payload(getattr(request.state, "request_id", None)),
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)


def install_middleware(app: FastAPI) -> None:
    # Added in reverse order of execution: the context middleware must wrap
    # the limiter so a 429 still carries a request id.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
