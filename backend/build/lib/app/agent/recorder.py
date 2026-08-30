"""Run recording.

Captures a complete run - every event, with its original timing, plus the
leads it produced and what it cost - to a JSON file.

One mechanism, three uses, which is why it earns its place:

  tests       a recorded run is a deterministic fixture, so the replay runtime
              and the API can be exercised with no model and no network.
  production  the deployed application serves recorded runs. It holds no
              Claude credentials and calls no model, which is what keeps the
              public deployment both free and within Anthropic's usage policy.
  README      the screenshots come from a real run rather than a mock-up.

Because ``offset_ms`` is preserved, replay reproduces the original pacing:
a recorded demo unfolds like the live one instead of appearing all at once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.ledger import RunLedger
from app.agent.runtime import AgentEvent
from app.obs.logging import get_logger

log = get_logger(__name__)

RECORDING_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "runs"
RECORDING_VERSION = 1


class RunRecorder:
    """Collects a run's events and leads for later replay."""

    def __init__(self, prompt: str, target_count: int, runtime: str) -> None:
        self.prompt = prompt
        self.target_count = target_count
        self.runtime = runtime
        self.recorded_at = datetime.now(UTC)
        self.events: list[AgentEvent] = []
        self.leads: list[dict[str, Any]] = []

    # --- collection ----------------------------------------------------
    async def on_event(self, event: AgentEvent) -> None:
        """Register as an EventBus sink."""
        self.events.append(event)

    def on_lead(self, payload: dict[str, Any]) -> None:
        """Register as the save_lead sink.

        Facts are Pydantic models, so they are dumped in JSON mode here rather
        than at write time - the alternative is a str() fallback that silently
        produces a repr nothing can load back.
        """
        record = dict(payload)
        for key in ("facts", "signals"):
            value = record.get(key)
            if value is not None and hasattr(value, "model_dump"):
                record[key] = value.model_dump(mode="json")
        record["score_breakdown"] = [
            c.model_dump(mode="json") if hasattr(c, "model_dump") else c
            for c in record.get("score_breakdown", [])
        ]
        for key in ("task_id", "run_id"):
            if record.get(key) is not None:
                record[key] = str(record[key])
        self.leads.append(record)

    # --- output --------------------------------------------------------
    def to_dict(self, ledger: RunLedger | None = None) -> dict[str, Any]:
        return {
            "version": RECORDING_VERSION,
            "recorded_at": self.recorded_at.isoformat(),
            "runtime": self.runtime,
            "prompt": self.prompt,
            "target_count": self.target_count,
            "ledger": ledger.summary() if ledger else None,
            "events": [
                {
                    "type": e.type.value,
                    "payload": e.payload,
                    "offset_ms": e.offset_ms,
                }
                for e in self.events
            ],
            "leads": self.leads,
        }

    def save(self, name: str, ledger: RunLedger | None = None, root: Path = RECORDING_ROOT) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{_slug(name)}.json"
        path.write_text(
            json.dumps(self.to_dict(ledger), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        log.info("recorder.saved", path=str(path), events=len(self.events), leads=len(self.leads))
        return path


def load_recording(path: Path) -> dict[str, Any]:
    """Read a recording, refusing one this build cannot interpret."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != RECORDING_VERSION:
        # Replaying an unknown format would produce a subtly wrong demo, which
        # is worse than an explicit refusal.
        raise ValueError(
            f"{path.name} is recording version {data.get('version')}, "
            f"but this build reads version {RECORDING_VERSION}"
        )
    return data


def list_recordings(root: Path = RECORDING_ROOT) -> list[Path]:
    return sorted(root.glob("*.json")) if root.exists() else []


def _slug(name: str) -> str:
    cleaned = "".join(c if c.isalnum() else "_" for c in name.lower())
    return "_".join(filter(None, cleaned.split("_")))[:80] or "run"
