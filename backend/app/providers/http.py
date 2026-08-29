"""A deliberately polite HTTP client.

Every free data source this project uses is somebody's donated infrastructure.
Overpass and Nominatim both publish usage policies that ask for a real
User-Agent, a low request rate, and caching of repeated queries. Ignoring that
gets you blocked, and rightly so.

Three behaviours, in the order they matter:

  caching     an identical request is answered from disk and never leaves the
              machine. This is also what makes development free and repeatable.
  throttling  a per-host minimum interval, enforced across concurrent callers.
  retrying    exponential backoff on the failures that are actually transient.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.obs.logging import get_logger

log = get_logger(__name__)

# Retried; everything else is reported immediately. A 400 from Overpass means
# the query is malformed and will stay malformed however often it is resent.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ProviderError(RuntimeError):
    """A data source could not answer. Carries context for the event log."""

    def __init__(self, message: str, *, source: str, status: int | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.status = status


@dataclass
class _HostThrottle:
    """Minimum spacing between requests to one host."""

    min_interval_s: float
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_at: float = 0.0

    async def wait(self) -> None:
        # The lock is held across the sleep on purpose: without it, N coroutines
        # would each read the same _last_at, all conclude they may proceed, and
        # fire simultaneously - which is exactly the burst the policy forbids.
        async with self._lock:
            elapsed = time.monotonic() - self._last_at
            if (remaining := self.min_interval_s - elapsed) > 0:
                await asyncio.sleep(remaining)
            self._last_at = time.monotonic()


class PoliteClient:
    """Shared HTTP client with caching, throttling and bounded retries."""

    def __init__(
        self,
        *,
        user_agent: str,
        cache_dir: Path,
        min_interval_s: float = 1.0,
        timeout_s: float = 30.0,
        max_attempts: int = 3,
        use_cache: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._cache_dir = cache_dir
        self._min_interval_s = min_interval_s
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._use_cache = use_cache
        # Injectable so tests can drive the retry, throttle and cache logic
        # against a MockTransport instead of the network. Patching this in
        # after construction does not work: `async with` resolves __aenter__
        # on the type, not the instance, so an instance-level override is
        # silently ignored and the real transport is used.
        self._transport = transport
        self._throttles: dict[str, _HostThrottle] = {}
        self._client: httpx.AsyncClient | None = None

    # --- lifecycle -----------------------------------------------------
    async def __aenter__(self) -> PoliteClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s),
            headers={"User-Agent": self._user_agent},
            follow_redirects=True,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- caching -------------------------------------------------------
    def _cache_path(self, method: str, url: str, body: str | None) -> Path:
        digest = hashlib.sha256(f"{method}\n{url}\n{body or ''}".encode()).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        if not (self._use_cache and path.exists()):
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt cache entry must never break a run; drop it and refetch.
            path.unlink(missing_ok=True)
            return None

    def _write_cache(self, path: Path, payload: dict[str, Any]) -> None:
        if not self._use_cache:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:  # a full or read-only disk is not fatal
            log.warning("http.cache_write_failed", error=str(exc))

    # --- requests ------------------------------------------------------
    async def get_json(self, url: str, *, params: dict[str, Any], source: str) -> Any:
        query = httpx.QueryParams(sorted(params.items()))
        return await self._request_json("GET", url, params=query, body=None, source=source)

    async def post_json(self, url: str, *, data: str, source: str) -> Any:
        return await self._request_json("POST", url, params=None, body=data, source=source)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: httpx.QueryParams | None,
        body: str | None,
        source: str,
    ) -> Any:
        # Sorted params so that logically identical requests hash identically -
        # otherwise dict ordering silently halves the cache hit rate.
        cache_key = str(httpx.URL(url, params=params)) if params is not None else url
        cache_path = self._cache_path(method, cache_key, body)

        if (cached := self._read_cache(cache_path)) is not None:
            log.debug("http.cache_hit", source=source, url=url)
            return cached["json"]

        if self._client is None:
            raise RuntimeError("PoliteClient must be used as an async context manager")

        host = httpx.URL(url).host
        throttle = self._throttles.setdefault(host, _HostThrottle(self._min_interval_s))

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            await throttle.wait()
            try:
                response = await self._client.request(
                    method, url, params=params, content=body.encode() if body else None
                )
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("http.transport_error", source=source, attempt=attempt, error=str(exc))
            else:
                if response.status_code in RETRYABLE_STATUS:
                    last_error = ProviderError(
                        f"{source} returned {response.status_code}",
                        source=source,
                        status=response.status_code,
                    )
                    log.warning(
                        "http.retryable_status",
                        source=source,
                        attempt=attempt,
                        status=response.status_code,
                    )
                elif response.is_error:
                    raise ProviderError(
                        f"{source} returned {response.status_code}: {response.text[:200]}",
                        source=source,
                        status=response.status_code,
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ProviderError(
                            f"{source} returned a non-JSON body", source=source
                        ) from exc
                    self._write_cache(cache_path, {"json": payload})
                    return payload

            if attempt < self._max_attempts:
                # 1s, 2s, 4s. Overpass in particular asks callers to back off
                # rather than hammer when it reports itself busy.
                await asyncio.sleep(2 ** (attempt - 1))

        raise ProviderError(
            f"{source} failed after {self._max_attempts} attempts: {last_error}", source=source
        )
