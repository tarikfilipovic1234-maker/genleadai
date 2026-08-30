"""A provider backed by recorded data.

Exists so that tests, the replay runtime and offline development never touch
the network. The fixtures are not invented - they are real Overpass responses
captured by ``python -m app.cli record``, so code exercised against them meets
the same messiness as production: missing websites, absent addresses,
inconsistent tagging.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.obs.logging import get_logger
from app.providers.categories import resolve_category
from app.schemas.business import BusinessQuery, BusinessStub

log = get_logger(__name__)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "businesses"


class FixtureProvider:
    """Serves ``fixtures/businesses/<category>__<location>.json``."""

    name = "fixture"

    def __init__(self, root: Path = FIXTURE_ROOT) -> None:
        self._root = root

    @staticmethod
    def fixture_name(category: str, location: str) -> str:
        profile, _ = resolve_category(category)
        slug = "".join(c if c.isalnum() else "_" for c in location.lower()).strip("_")
        return f"{profile.name}__{slug}.json"

    def path_for(self, query: BusinessQuery) -> Path:
        return self._root / self.fixture_name(query.category, query.location)

    async def find_businesses(self, query: BusinessQuery) -> list[BusinessStub]:
        path = self.path_for(query)
        if not path.exists():
            # Deliberately not an error: a missing fixture means "this query
            # matched nothing here", which is a normal outcome the agent
            # already knows how to report.
            log.warning("fixture.missing", path=str(path))
            return []

        records = json.loads(path.read_text(encoding="utf-8"))
        stubs = [BusinessStub.model_validate(r) for r in records][: query.limit]
        log.info("fixture.loaded", path=path.name, count=len(stubs))
        return stubs

    def save(self, query: BusinessQuery, stubs: list[BusinessStub]) -> Path:
        """Record a live result set for later offline use."""
        path = self.path_for(query)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([s.model_dump(mode="json") for s in stubs], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path
