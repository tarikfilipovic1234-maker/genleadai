"""Provider selection.

The single place that knows which concrete provider is in use. Everything else
depends on the :class:`SearchProvider` Protocol, so swapping OpenStreetMap for
a paid API later touches this file and nothing else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import Settings, get_settings
from app.providers.base import SearchProvider
from app.providers.fixture import FixtureProvider
from app.providers.http import PoliteClient
from app.providers.nominatim import NominatimGeocoder
from app.providers.overpass import OverpassProvider


@asynccontextmanager
async def open_search_provider(
    settings: Settings | None = None,
) -> AsyncIterator[SearchProvider]:
    """Yield the configured provider, with its HTTP client scoped to the block.

    A context manager rather than a plain factory because the live provider
    owns a connection pool that must be closed. The fixture provider owns
    nothing, so it short-circuits without opening one.
    """
    settings = settings or get_settings()

    if settings.search_provider == "fixture":
        yield FixtureProvider()
        return

    async with PoliteClient(
        user_agent=settings.http_user_agent,
        cache_dir=settings.http_cache_dir,
        min_interval_s=settings.http_min_interval_s,
        timeout_s=settings.http_timeout_s,
        use_cache=settings.http_use_cache,
    ) as client:
        yield OverpassProvider(
            client,
            NominatimGeocoder(client, url=settings.nominatim_url),
            url=settings.overpass_url,
        )
