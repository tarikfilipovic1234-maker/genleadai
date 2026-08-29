"""API and streaming tests."""

from __future__ import annotations

import csv
import io
import json
from typing import Any
from uuid import uuid4

import pytest

from app.agent.runtime import AgentEvent, EventType
from app.db import repository as repo
from app.db.models import TaskStatus
from app.schemas.lead import LeadFacts, ScoreContribution, normalize_for_dedup
from app.schemas.provenance import Fact


def _lead_payload(task_id, name: str = "Salon Nova", score: int = 70) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "run_id": None,
        "external_id": f"osm:node/{abs(hash(name)) % 10_000}",
        "name": name,
        "category": "shop=beauty",
        "dedup_key": normalize_for_dedup(name),
        "facts": LeadFacts(
            business_name=Fact.verified(name, source_url="https://osm.org/node/1"),
            phone=Fact.verified("033 123 456", source_url="https://salonnova.ba"),
            has_online_booking=Fact.inferred(False, evidence="no booking widget found"),
        ),
        "score": score,
        "score_breakdown": [
            ScoreContribution(rule="no_online_booking", points=30, reason="none found")
        ],
        "qualification_reason": "No online booking and a dated website.",
        "sales_angle": "Bookings are phone-only.",
        "outreach_message": "Pogledao sam vasu stranicu i vidim da nemate online zakazivanje.",
        "sources": [
            {"url": "https://osm.org/node/1", "kind": "osm"},
            {"url": "https://salonnova.ba", "kind": "website", "excerpt": "Frizerski salon"},
        ],
    }


async def _seed(session, *, leads: int = 2):
    task = await repo.create_task(session, prompt="Find salons in Sarajevo", target_count=5)
    for i in range(leads):
        await repo.save_lead(session, _lead_payload(task.id, f"Salon {i}", 90 - i * 20))
    await session.commit()
    return task


# ----------------------------------------------------------------------
class TestTasks:
    async def test_creating_a_task_returns_immediately(self, client) -> None:
        """202, not 201: a real run takes minutes, so holding the connection
        open would hit every proxy timeout between here and the browser."""
        response = await client.post(
            "/api/tasks", json={"prompt": "Find 5 salons in Sarajevo", "target_count": 5}
        )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        assert body["stream"].endswith("/stream")

    async def test_a_short_prompt_is_rejected(self, client) -> None:
        response = await client.post("/api/tasks", json={"prompt": "hi"})

        assert response.status_code == 422

    async def test_target_count_is_bounded(self, client) -> None:
        response = await client.post(
            "/api/tasks", json={"prompt": "Find salons in Sarajevo", "target_count": 5000}
        )

        assert response.status_code == 422

    async def test_tasks_are_listed_with_their_lead_counts(self, client, session) -> None:
        await _seed(session, leads=3)

        body = (await client.get("/api/tasks")).json()

        assert body[0]["lead_count"] == 3

    async def test_a_missing_task_is_404(self, client) -> None:
        assert (await client.get(f"/api/tasks/{uuid4()}")).status_code == 404

    async def test_deleting_a_task_removes_its_leads(self, client, session) -> None:
        task = await _seed(session)

        assert (await client.delete(f"/api/tasks/{task.id}")).status_code == 204
        assert (await client.get(f"/api/leads?task_id={task.id}")).json() == []


# ----------------------------------------------------------------------
class TestLeads:
    async def test_leads_are_returned_best_first(self, client, session) -> None:
        await _seed(session, leads=3)

        scores = [lead["score"] for lead in (await client.get("/api/leads")).json()]

        assert scores == sorted(scores, reverse=True)

    async def test_provenance_reaches_the_client(self, client, session) -> None:
        """The dashboard cannot render 'Not verified' unless it is told."""
        await _seed(session, leads=1)
        lead = (await client.get("/api/leads")).json()[0]

        assert lead["facts"]["business_name"]["provenance"] == "verified"
        assert lead["facts"]["google_rating"]["provenance"] == "unverified"
        assert lead["facts"]["google_rating"]["value"] is None
        assert lead["provenance_summary"]["unverified"] > 0

    async def test_filtering_by_minimum_score(self, client, session) -> None:
        await _seed(session, leads=3)

        body = (await client.get("/api/leads?min_score=80")).json()

        assert [lead["score"] for lead in body] == [90]

    async def test_searching_by_name(self, client, session) -> None:
        await _seed(session, leads=3)

        body = (await client.get("/api/leads?search=Salon 1")).json()

        assert [lead["name"] for lead in body] == ["Salon 1"]

    async def test_sources_are_returned_on_the_detail_view(self, client, session) -> None:
        await _seed(session, leads=1)
        lead_id = (await client.get("/api/leads")).json()[0]["id"]

        body = (await client.get(f"/api/leads/{lead_id}")).json()

        assert {s["kind"] for s in body["sources"]} == {"osm", "website"}

    async def test_deleting_a_lead(self, client, session) -> None:
        await _seed(session, leads=2)
        lead_id = (await client.get("/api/leads")).json()[0]["id"]

        assert (await client.delete(f"/api/leads/{lead_id}")).status_code == 204
        assert len((await client.get("/api/leads")).json()) == 1

    async def test_a_duplicate_lead_is_skipped_not_raised(self, session) -> None:
        """The same salon appears twice in OSM under slightly different names,
        so this is expected rather than exceptional."""
        task = await repo.create_task(session, prompt="x", target_count=5)
        first = await repo.save_lead(session, _lead_payload(task.id, "Salon Nova"))
        second = await repo.save_lead(session, _lead_payload(task.id, "Salon Nova"))

        assert first is not None
        assert second is None


# ----------------------------------------------------------------------
class TestCsvExport:
    async def test_each_fact_exports_with_its_provenance(self, client, session) -> None:
        """Flattening to values alone would export 'Not verified' as an empty
        cell, and a spreadsheet could not then tell a business with no phone
        from one whose phone was never checked."""
        await _seed(session, leads=1)

        response = await client.get("/api/leads.csv")
        rows = list(csv.reader(io.StringIO(response.text)))
        header, row = rows[0], rows[1]
        cells = dict(zip(header, row, strict=True))

        assert response.headers["content-type"].startswith("text/csv")
        assert cells["phone"] == "033 123 456"
        assert cells["phone_provenance"] == "verified"
        assert cells["google_rating"] == ""
        assert cells["google_rating_provenance"] == "unverified"

    async def test_sources_are_included(self, client, session) -> None:
        await _seed(session, leads=1)

        rows = list(csv.reader(io.StringIO((await client.get("/api/leads.csv")).text)))

        assert "salonnova.ba" in rows[1][-1]

    async def test_it_downloads_as_a_file(self, client, session) -> None:
        await _seed(session, leads=1)

        response = await client.get("/api/leads.csv")

        assert "attachment" in response.headers["content-disposition"]


# ----------------------------------------------------------------------
class TestStreaming:
    @staticmethod
    def _parse(text: str) -> list[dict[str, Any]]:
        events = []
        for block in text.strip().split("\n\n"):
            if not block.strip():
                continue
            fields: dict[str, Any] = {}
            for line in block.splitlines():
                key, _, value = line.partition(": ")
                fields[key] = value
            if "data" in fields:
                fields["data"] = json.loads(fields["data"])
            events.append(fields)
        return events

    async def test_a_finished_run_streams_its_history_from_storage(self, client, session) -> None:
        """Which is what makes a completed task shareable as a link."""
        task = await _seed(session, leads=0)
        for kind in (EventType.RUN_STARTED, EventType.TOOL_CALLED, EventType.RUN_COMPLETED):
            await repo.append_event(
                session, task_id=task.id, run_id=None, event=AgentEvent(type=kind)
            )
        await session.commit()

        response = await client.get(f"/api/tasks/{task.id}/stream")
        events = self._parse(response.text)

        assert response.headers["content-type"].startswith("text/event-stream")
        assert [e["event"] for e in events[:3]] == [
            "run.started",
            "tool.called",
            "run.completed",
        ]
        assert events[-1]["event"] == "stream.end"

    async def test_reconnect_resumes_rather_than_restarts(self, client, session) -> None:
        """A dropped connection must not replay forty tool calls into the UI."""
        task = await _seed(session, leads=0)
        rows = []
        for kind in (EventType.RUN_STARTED, EventType.TOOL_CALLED, EventType.RUN_COMPLETED):
            rows.append(
                await repo.append_event(
                    session, task_id=task.id, run_id=None, event=AgentEvent(type=kind)
                )
            )
        await session.commit()

        response = await client.get(
            f"/api/tasks/{task.id}/stream", headers={"Last-Event-ID": str(rows[0].id)}
        )
        events = [e for e in self._parse(response.text) if e["event"] != "stream.end"]

        assert [e["event"] for e in events] == ["tool.called", "run.completed"]

    async def test_event_ids_are_monotonic(self, client, session) -> None:
        """Ordering by a timestamp would tie on events written in the same
        millisecond, which happens constantly."""
        task = await _seed(session, leads=0)
        for _ in range(5):
            await repo.append_event(
                session, task_id=task.id, run_id=None, event=AgentEvent(type=EventType.TOOL_CALLED)
            )
        await session.commit()

        events = self._parse((await client.get(f"/api/tasks/{task.id}/stream")).text)
        # stream.end deliberately carries no id: it is a marker, not a
        # resumable position.
        ids = [int(e["id"]) for e in events if "id" in e]

        assert len(ids) == 5
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    async def test_the_end_marker_is_not_a_resume_position(self, client, session) -> None:
        """Giving it an id would either duplicate a real event or advertise a
        position a reconnect cannot resume from."""
        task = await _seed(session, leads=0)
        await repo.append_event(
            session, task_id=task.id, run_id=None, event=AgentEvent(type=EventType.RUN_COMPLETED)
        )
        await session.commit()

        events = self._parse((await client.get(f"/api/tasks/{task.id}/stream")).text)
        end = next(e for e in events if e["event"] == "stream.end")

        assert "id" not in end

    @pytest.mark.parametrize("header", ["not-a-number", "", "-5"])
    async def test_a_malformed_last_event_id_restarts_rather_than_fails(
        self, client, session, header: str
    ) -> None:
        task = await _seed(session, leads=0)
        await repo.append_event(
            session, task_id=task.id, run_id=None, event=AgentEvent(type=EventType.RUN_STARTED)
        )
        await session.commit()

        response = await client.get(
            f"/api/tasks/{task.id}/stream", headers={"Last-Event-ID": header}
        )

        assert response.status_code == 200
        assert "run.started" in response.text

    async def test_streaming_an_unknown_task_is_404(self, client) -> None:
        assert (await client.get(f"/api/tasks/{uuid4()}/stream")).status_code == 404

    async def test_buffering_is_disabled(self, client, session) -> None:
        """nginx otherwise holds events until the response completes, which
        defeats streaming entirely."""
        task = await _seed(session, leads=0)

        headers = (await client.get(f"/api/tasks/{task.id}/stream")).headers

        assert headers["x-accel-buffering"] == "no"
        assert "no-cache" in headers["cache-control"]


# ----------------------------------------------------------------------
class TestConfig:
    async def test_reports_whether_live_runs_are_possible(self, client) -> None:
        """The deployed instance serves recordings; the interface should say
        so rather than offering a button that cannot work."""
        body = (await client.get("/api/config")).json()

        assert "live_runs_enabled" in body
        assert body["runtime"] in {"sdk", "replay", "manual"}


# ----------------------------------------------------------------------
class TestRepository:
    async def test_run_status_and_ledger_are_recorded(self, session) -> None:
        from app.db.models import RunStatus

        task = await repo.create_task(session, prompt="x", target_count=5)
        run = await repo.create_run(session, task_id=task.id, runtime="sdk")

        await repo.finish_run(
            session,
            run.id,
            status=RunStatus.COMPLETED,
            ledger={"turns": 39, "input_tokens": 409997, "output_tokens": 7562, "cost_usd": 1.27},
        )
        await session.commit()

        assert run.num_turns == 39
        assert run.input_tokens == 409997
        assert run.status is RunStatus.COMPLETED

    async def test_task_failure_is_recorded_with_its_reason(self, session) -> None:
        task = await repo.create_task(session, prompt="x", target_count=5)

        await repo.update_task_status(session, task.id, TaskStatus.FAILED, error="claude not found")
        await session.commit()

        assert task.status is TaskStatus.FAILED
        assert "claude not found" in task.error
