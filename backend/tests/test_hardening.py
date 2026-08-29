"""Rate limiting, deduplication and the error envelope."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.api.middleware import (
    EXPENSIVE_LIMIT,
    SlidingWindowLimiter,
    client_key,
    limiter,
)
from app.db import repository as repo
from app.schemas.lead import (
    SIMILARITY_THRESHOLD,
    find_near_duplicate,
    key_similarity,
    normalize_for_dedup,
)
from tests.test_api import _lead_payload


@pytest.fixture(autouse=True)
def _reset_limiter():
    """The limiter is process-wide, so tests would otherwise contaminate each
    other through it - and the failures would depend on execution order."""
    limiter._hits.clear()
    yield
    limiter._hits.clear()


# ----------------------------------------------------------------------
class TestSlidingWindow:
    def test_requests_under_the_limit_pass(self) -> None:
        window = SlidingWindowLimiter()

        assert all(window.check("k", 3, 60) is None for _ in range(3))

    def test_the_limit_is_enforced(self) -> None:
        window = SlidingWindowLimiter()
        for _ in range(3):
            window.check("k", 3, 60)

        retry_after = window.check("k", 3, 60)

        assert retry_after is not None
        assert 0 < retry_after <= 61

    def test_keys_are_independent(self) -> None:
        window = SlidingWindowLimiter()
        for _ in range(3):
            window.check("a", 3, 60)

        assert window.check("b", 3, 60) is None

    def test_the_window_slides(self) -> None:
        """A fixed window lets a caller spend one allowance at 11:59:59 and
        another at 12:00:00 - twice the intended burst.

        Time is injected rather than slept: a test that waits a real minute
        to prove a window expires gets deleted by whoever next runs the suite.
        """
        window = SlidingWindowLimiter()
        window.check("k", 2, 60, now=1000.0)
        window.check("k", 2, 60, now=1001.0)

        assert window.check("k", 2, 60, now=1002.0) is not None  # still inside
        assert window.check("k", 2, 60, now=1062.0) is None  # first has aged out

    def test_idle_keys_are_pruned(self) -> None:
        window = SlidingWindowLimiter()
        window.check("k", 5, 60)

        window.prune(older_than=-1)

        assert window._hits == {}


class TestClientKey:
    def test_the_forwarded_header_wins_behind_a_proxy(self) -> None:
        request = type(
            "R",
            (),
            {"headers": {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, "client": None},
        )()

        assert client_key(request) == "203.0.113.7"

    def test_it_falls_back_to_the_socket_address(self) -> None:
        request = type("R", (), {"headers": {}, "client": type("C", (), {"host": "127.0.0.1"})()})()

        assert client_key(request) == "127.0.0.1"


# ----------------------------------------------------------------------
class TestRateLimitedApi:
    async def test_starting_runs_is_limited(self, client) -> None:
        """One unauthenticated POST is all it takes to start background work,
        and the endpoint is public."""
        body = {"prompt": "Find salons in Sarajevo", "target_count": 3}
        allowed = EXPENSIVE_LIMIT[0]

        statuses = [
            (await client.post("/api/tasks", json=body)).status_code for _ in range(allowed + 2)
        ]

        assert statuses[:allowed] == [202] * allowed
        assert statuses[allowed:] == [429, 429]

    async def test_a_rate_limit_says_when_to_retry(self, client) -> None:
        body = {"prompt": "Find salons in Sarajevo", "target_count": 3}
        for _ in range(EXPENSIVE_LIMIT[0]):
            await client.post("/api/tasks", json=body)

        response = await client.post("/api/tasks", json=body)

        assert response.headers["retry-after"].isdigit()
        assert response.json()["error"]["code"] == "rate_limited"

    async def test_reads_have_their_own_budget(self, client) -> None:
        """A dashboard that polls must not exhaust the allowance for work."""
        body = {"prompt": "Find salons in Sarajevo", "target_count": 3}
        for _ in range(EXPENSIVE_LIMIT[0] + 1):
            await client.post("/api/tasks", json=body)

        assert (await client.get("/api/leads")).status_code == 200

    async def test_streams_are_never_rate_limited(self, client, session) -> None:
        """A run holds one open for minutes; limiting it would sever the
        connection it exists to protect."""
        task = await repo.create_task(session, prompt="x", target_count=3)
        await session.commit()

        for _ in range(30):
            response = await client.get(f"/api/tasks/{task.id}/stream")

        assert response.status_code == 200


# ----------------------------------------------------------------------
class TestErrorEnvelope:
    async def test_every_error_uses_one_shape(self, client) -> None:
        """The frontend branches on `code`; branching on a message breaks the
        first time someone improves the wording."""
        response = await client.get(f"/api/tasks/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_validation_failures_are_flattened(self, client) -> None:
        response = await client.post("/api/tasks", json={"prompt": "hi"})
        body = response.json()["error"]

        assert response.status_code == 422
        assert body["code"] == "validation_failed"
        assert "prompt" in body["message"]
        assert body["fields"][0]["field"] == "prompt"

    async def test_responses_carry_a_request_id(self, client) -> None:
        """It is what connects a user's report to the logged traceback."""
        response = await client.get("/api/config")

        assert response.headers["x-request-id"]

    async def test_an_error_body_carries_the_same_id(self, client) -> None:
        response = await client.get(f"/api/tasks/{uuid4()}")

        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


# ----------------------------------------------------------------------
class TestNearDuplicates:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Salon Mia", "Salon Mia Beauty Studio"),
            ("Frizerski salon Nova", "Nova frizerski"),
            ("Studio Anđela", "Studio Andjela Sarajevo"),
        ],
    )
    def test_the_same_business_written_differently_is_matched(self, a: str, b: str) -> None:
        key_a, key_b = normalize_for_dedup(a), normalize_for_dedup(b)

        assert key_similarity(key_a, key_b) >= SIMILARITY_THRESHOLD

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Salon Mia", "Salon Ana"),
            ("Frizerski salon Nova", "Frizerski salon Diva"),
            ("Studio A", "Studio B"),
        ],
    )
    def test_different_businesses_stay_separate(self, a: str, b: str) -> None:
        """Merging two real businesses loses a lead permanently; a surviving
        near-duplicate is one row a person deletes in a second."""
        key_a, key_b = normalize_for_dedup(a), normalize_for_dedup(b)

        assert key_similarity(key_a, key_b) < SIMILARITY_THRESHOLD

    def test_the_closest_match_is_returned(self) -> None:
        key = normalize_for_dedup("Salon Mia Beauty")

        match = find_near_duplicate(
            key,
            [normalize_for_dedup("Salon Ana"), normalize_for_dedup("Salon Mia")],
        )

        assert match == normalize_for_dedup("Salon Mia")

    def test_no_match_returns_none(self) -> None:
        assert find_near_duplicate(normalize_for_dedup("Salon Mia"), []) is None

    async def test_a_near_duplicate_is_not_saved_twice(self, session) -> None:
        """The unique constraint catches exact repeats; OSM produces these."""
        task = await repo.create_task(session, prompt="x", target_count=5)

        first = await repo.save_lead(session, _lead_payload(task.id, "Salon Mia"))
        second = await repo.save_lead(session, _lead_payload(task.id, "Salon Mia Beauty Studio"))

        assert first is not None
        assert second is None

    async def test_distinct_businesses_are_both_saved(self, session) -> None:
        task = await repo.create_task(session, prompt="x", target_count=5)

        first = await repo.save_lead(session, _lead_payload(task.id, "Salon Mia"))
        second = await repo.save_lead(session, _lead_payload(task.id, "Salon Ana"))

        assert first is not None
        assert second is not None

    async def test_the_same_name_in_two_tasks_is_kept(self, session) -> None:
        """Deduplication is per result set, not global - a business found by
        two different searches belongs in both."""
        a = await repo.create_task(session, prompt="x", target_count=5)
        b = await repo.create_task(session, prompt="y", target_count=5)

        assert await repo.save_lead(session, _lead_payload(a.id, "Salon Mia")) is not None
        assert await repo.save_lead(session, _lead_payload(b.id, "Salon Mia")) is not None
