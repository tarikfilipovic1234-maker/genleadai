"""The provider contract.

Any source of businesses - OpenStreetMap today, Google Places or Serper if this
is ever given a budget - implements this one method. The agent, the tools and
the tests all depend on the Protocol rather than on any concrete provider, so
adding a paid source later means writing one class and changing one setting.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.business import BusinessQuery, BusinessStub


@runtime_checkable
class SearchProvider(Protocol):
    """Finds businesses matching a query."""

    #: Short identifier used in logs, events and source attribution.
    name: str

    async def find_businesses(self, query: BusinessQuery) -> list[BusinessStub]:
        """Return matching businesses, best-effort.

        Implementations should return fewer results rather than raise when a
        query simply matches nothing, and raise
        :class:`~app.providers.http.ProviderError` only when the source itself
        failed - the agent handles those two cases very differently.
        """
        ...
