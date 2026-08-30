"""Developer CLI.

Lets a provider be exercised on its own, without the agent, the database or the
API in the way. When a lead set looks wrong, the first question is always "did
the data source return anything sensible?" - this answers it in one command.

    python -m app.cli search "beauty salons" "Sarajevo"
    python -m app.cli search "beauty salons" "Sarajevo" --record
    python -m app.cli search "beauty salons" "Sarajevo" --provider fixture
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from app.config import get_settings
from app.enrichment.booking import detect_booking
from app.enrichment.extract import extract_signals
from app.enrichment.fetcher import WebsiteFetcher
from app.obs.logging import configure_logging
from app.providers.fixture import FixtureProvider
from app.providers.http import ProviderError
from app.providers.registry import open_search_provider
from app.schemas.business import BusinessQuery, BusinessStub


def _render(stubs: list[BusinessStub]) -> None:
    if not stubs:
        print("No businesses found.")
        return

    for i, s in enumerate(stubs, 1):
        print(f"\n{i:>3}. {s.name}")
        print(f"     {s.category or '-':<28} {s.address or 'no address'}")
        contacts = [
            ("web", s.website),
            ("tel", s.phone),
            ("ig", s.instagram),
            ("fb", s.facebook),
            ("mail", s.email),
        ]
        present = [f"{label}:{value}" for label, value in contacts if value]
        print(f"     {' | '.join(present) if present else 'no contact details'}")
        if s.opening_hours:
            print(f"     hours: {s.opening_hours}")
        print(f"     {s.source_url}")

    # The coverage summary is the point of this command: it shows how much of
    # the data is actually there, which is what determines how many fields end
    # up UNVERIFIED once the agent runs.
    total = len(stubs)
    print(f"\n{'-' * 60}")
    print(f"{total} businesses")
    for label, attr in (
        ("website", "website"),
        ("phone", "phone"),
        ("address", "address"),
        ("hours", "opening_hours"),
        ("instagram", "instagram"),
        ("email", "email"),
    ):
        n = sum(1 for s in stubs if getattr(s, attr))
        print(f"  {label:<10} {n:>3}/{total}  {n * 100 // total:>3}%")


async def _search(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.provider:
        settings = settings.model_copy(update={"search_provider": args.provider})
    if args.no_cache:
        settings = settings.model_copy(update={"http_use_cache": False})

    query = BusinessQuery(category=args.category, location=args.location, limit=args.limit)

    try:
        async with open_search_provider(settings) as provider:
            print(f"provider: {provider.name}  |  query: {args.category} in {args.location}\n")
            stubs = await provider.find_businesses(query)
    except ProviderError as exc:
        print(f"Provider failed ({exc.source}): {exc}", file=sys.stderr)
        return 1

    _render(stubs)

    if args.record and stubs:
        path = FixtureProvider().save(query, stubs)
        print(f"\nRecorded {len(stubs)} businesses to {path}")

    return 0


async def _inspect(args: argparse.Namespace) -> int:
    """Fetch websites and report what the deterministic layer can establish.

    Deliberately runs with no model involved. Everything printed here is
    reached by fetching and pattern matching, which is the point: it shows
    exactly how much is known before the agent is asked to reason at all.
    """
    settings = get_settings()

    urls: list[str] = list(args.urls)
    if args.from_fixture:
        stubs = await FixtureProvider().find_businesses(
            BusinessQuery(category=args.from_fixture, location=args.location, limit=100)
        )
        urls += [s.website for s in stubs if s.website]
        print(f"{len(urls)} of {len(stubs)} fixture businesses list a website\n")

    if not urls:
        print("No URLs to inspect.")
        return 0

    async with WebsiteFetcher(
        user_agent=settings.http_user_agent, respect_robots=not args.ignore_robots
    ) as fetcher:
        pages = await fetcher.fetch_many(urls)

    for page in pages:
        print(f"\n{page.requested_url}")
        if not page.ok:
            print(f"  outcome : {page.outcome.value} ({page.error})")
            continue

        signals = extract_signals(page.html, final_url=page.final_url, text=page.text)
        booking = detect_booking(page)

        answer = {True: "yes", False: "no", None: "unknown"}[booking.has_booking]
        strength = "verified" if booking.is_direct_evidence else "inferred"

        print(f"  title   : {page.title}")
        print(f"  booking : {answer} ({strength}) - {booking.evidence[:90]}")
        print(
            f"  quality : https={signals.https} mobile={signals.mobile_friendly} "
            f"copyright={signals.copyright_year} text={signals.text_length}c"
        )
        if signals.emails or signals.phones:
            print(f"  contact : {', '.join(signals.emails + signals.phones)}")
        if signals.outbound_social:
            print(f"  social  : {', '.join(signals.outbound_social[:3])}")

    reachable = sum(1 for p in pages if p.ok)
    print(f"\n{'-' * 60}\n{reachable}/{len(pages)} reachable")
    return 0


async def _run(args: argparse.Namespace) -> int:
    """Execute a lead-generation task and print the event stream."""
    from app.agent.events import EventBus
    from app.agent.recorder import RunRecorder
    from app.agent.runtime import EventType
    from app.agent.sdk_runtime import AgentSDKRuntime
    from app.agent.tools.context import ToolContext

    settings = get_settings()
    if args.provider:
        settings = settings.model_copy(update={"search_provider": args.provider})

    saved: list[dict[str, Any]] = []
    recorder = RunRecorder(args.prompt, args.target, "sdk")
    bus = EventBus()
    bus.add_sink(recorder.on_event)

    async def collect(payload: dict[str, Any]) -> None:
        saved.append(payload)
        recorder.on_lead(payload)

    async with (
        open_search_provider(settings) as provider,
        WebsiteFetcher(user_agent=settings.http_user_agent) as fetcher,
    ):
        ctx = ToolContext(
            provider=provider,
            fetcher=fetcher,
            save_lead_fn=collect,
            scoring_profile=args.profile,
        )
        runtime = AgentSDKRuntime(ctx, settings)

        print(f"runtime: {runtime.name}  |  provider: {provider.name}")
        print(f"task   : {args.prompt}\n")

        icons = {
            EventType.RUN_STARTED: "*",
            EventType.AGENT_MESSAGE: " ",
            EventType.TOOL_CALLED: ">",
            EventType.WARNING: "!",
            EventType.RUN_COMPLETED: "=",
            EventType.RUN_FAILED: "x",
        }

        async for ev in runtime.run(args.prompt, args.target):
            await bus.publish(ev)
            mark = icons.get(ev.type, "-")
            secs = ev.offset_ms / 1000
            if ev.type is EventType.TOOL_CALLED:
                detail = ", ".join(f"{k}={v}" for k, v in ev.payload.get("input", {}).items())
                print(f"[{secs:6.1f}s] {mark} {ev.payload['tool']}({detail})")
            elif ev.type is EventType.TOOL_RESULT:
                ms = ev.payload.get("duration_ms")
                took = f"{ms}ms" if ms is not None else "?"
                flag = "" if ev.payload.get("ok") else " FAILED"
                summary = ev.payload.get("summary") or {}
                bits = " ".join(f"{k}={v}" for k, v in summary.items())
                print(f"[{secs:6.1f}s] < {ev.payload['tool']} {took}{flag} {bits}")
            elif ev.type is EventType.AGENT_MESSAGE:
                for line in ev.payload["text"].splitlines():
                    if line.strip():
                        print(f"[{secs:6.1f}s] {mark}   {line.strip()[:110]}")
            else:
                summary = ", ".join(f"{k}={v}" for k, v in ev.payload.items() if k != "prompt")
                print(f"[{secs:6.1f}s] {mark} {ev.type.value} {summary}")

    await bus.close()

    if saved:
        print(f"\n{'-' * 62}\nSaved {len(saved)} leads\n")
        for lead in sorted(saved, key=lambda x: -x["score"]):
            print(f"  {lead['score']:>3}/100  {lead['name']}")
            print(f"           {lead['qualification_reason'][:100]}")
            counts = lead["facts"].provenance_counts()
            print(
                f"           verified={counts['verified']} inferred={counts['inferred']} "
                f"unverified={counts['unverified']}  sources={len(lead['sources'])}"
            )

    ledger = runtime.ledger
    print(f"\n{'-' * 62}\nRun ledger")
    for key, value in ledger.summary().items():
        print(f"  {key:<22} {value}")
    if ledger.slowest_calls:
        print("  slowest calls")
        for call in ledger.slowest_calls:
            print(f"    {call.duration_ms:>6}ms  {call.tool}")

    if args.record:
        path = recorder.save(args.record, ledger)
        print(f"\nRecorded {len(recorder.events)} events to {path}")

    return 0


async def _score(args: argparse.Namespace) -> int:
    """Re-score recorded leads against the current rules.

    The tuning loop: edit rules.yaml, run this, see how the ranking moved -
    with no model, no network and no cost. Scoring is arithmetic, so a change
    in weights is fully observable without re-running the agent.
    """
    from app.agent.recorder import list_recordings, load_recording
    from app.schemas.lead import LeadFacts
    from app.schemas.page import SiteSignals
    from app.scoring.engine import default_rules, score_lead

    recordings = list_recordings()
    if not recordings:
        print("No recorded runs. Run: python -m app.cli run '...' --record NAME")
        return 1

    ruleset = default_rules()
    if args.list_profiles:
        print("Profiles:\n")
        for name, profile in sorted(ruleset.profiles.items()):
            requires = ", ".join(profile.requires) or "nothing"
            print(f"  {name:<24} requires: {requires}")
            if profile.description:
                print(f"  {'':<24} {profile.description}")
        return 0

    print(f"rules v{ruleset.version}  |  profile: {args.profile or 'default'}\n")

    for path in recordings:
        data = load_recording(path)
        print(f"{path.name}  ({len(data['leads'])} leads)")

        rescored = []
        missing_signals = 0
        for lead in data["leads"]:
            facts = LeadFacts.model_validate(lead["facts"])
            raw_signals = lead.get("signals")
            signals = SiteSignals.model_validate(raw_signals) if raw_signals else None
            if signals is None:
                # Recorded before signals were persisted. Without them the
                # signal-driven rules cannot fire, so the total will read low -
                # which looks like a weight change unless we say otherwise.
                missing_signals += 1
            result = score_lead(facts, signals, ruleset, profile=args.profile)
            rescored.append((result, lead))

        if missing_signals:
            print(
                f"  note: {missing_signals} lead(s) predate signal recording; "
                "their website-quality rules cannot be re-evaluated"
            )

        for result, lead in sorted(rescored, key=lambda r: -r[0].score):
            flag = (
                ""
                if result.qualifies
                else f"  DISQUALIFIED ({', '.join(result.unmet_requirements)})"
            )
            print(f"  {result.score:>3}  was {lead['score']:>3}  {lead['name']}{flag}")
            for c in result.contributions:
                print(f"        +{c.points:<3} {c.rule}")
        print()

    return 0


async def _outreach(args: argparse.Namespace) -> int:
    """Generate outreach for recorded leads and report verification."""
    from app.agent.recorder import list_recordings, load_recording
    from app.outreach.generator import channel_for, generate_outreach
    from app.schemas.lead import LeadFacts

    recordings = list_recordings()
    if not recordings:
        print("No recorded runs. Run: python -m app.cli run '...' --record NAME")
        return 1

    leads = [lead for path in recordings for lead in load_recording(path)["leads"]][: args.limit]
    print(f"Generating outreach for {len(leads)} recorded leads\n")

    for lead in leads:
        facts = LeadFacts.model_validate(lead["facts"])
        print(f"{'=' * 66}\n{lead['name']}  ({lead['score']}/100)")
        print(f"  verified channels: {[c.value for c in _channels(facts)] or 'none'}")
        print(f"  preferred        : {(channel_for(facts) or 'none')}\n")

        result = await generate_outreach(facts, instruction=args.instruction)

        if not result.ok:
            print(f"  FAILED after {result.attempts} attempt(s): {result.error}")
            for problem in result.problems:
                print(f"    - {problem.field}: {problem.quote!r} - {problem.explanation}")
            print()
            continue

        draft = result.draft
        assert draft is not None
        print(f"  channel  : {draft.channel.value}   language: {draft.language.value}")
        print(f"  anchors  : {', '.join(draft.anchors)}")
        print(f"  rationale: {draft.opening_rationale}")
        print(f"  attempts : {result.attempts}")
        print(f"\n  Subject: {draft.subject}\n")
        for line in draft.message.splitlines():
            print(f"    {line}")
        print()

    return 0


def _channels(facts) -> list:
    from app.outreach.schema import usable_channels

    return usable_channels(facts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="find businesses via the configured provider")
    search.add_argument("category", help="e.g. 'beauty salons'")
    search.add_argument("location", help="e.g. 'Sarajevo'")
    search.add_argument("--limit", type=int, default=30)
    search.add_argument("--provider", choices=["overpass", "fixture"])
    search.add_argument("--no-cache", action="store_true", help="bypass the HTTP disk cache")
    search.add_argument("--record", action="store_true", help="save results as a test fixture")
    search.set_defaults(func=_search)

    inspect = sub.add_parser("inspect", help="fetch websites and report deterministic signals")
    inspect.add_argument("urls", nargs="*", help="URLs to fetch")
    inspect.add_argument("--from-fixture", metavar="CATEGORY", help="use websites in a fixture")
    inspect.add_argument("--location", default="Sarajevo")
    inspect.add_argument("--ignore-robots", action="store_true", help="diagnostics only")
    inspect.set_defaults(func=_inspect)

    run = sub.add_parser("run", help="execute a lead-generation task with the agent")
    run.add_argument("prompt", help="natural-language request")
    run.add_argument("--target", type=int, default=5, help="how many leads to save")
    run.add_argument("--provider", choices=["overpass", "fixture"])
    run.add_argument("--record", metavar="NAME", help="save this run as a replay fixture")
    run.add_argument("--profile", help="scoring profile, e.g. no_online_booking")
    run.set_defaults(func=_run)

    score = sub.add_parser("score", help="re-score recorded leads against the current rules")
    score.add_argument("--profile", help="scoring profile to apply")
    score.add_argument("--list-profiles", action="store_true")
    score.set_defaults(func=_score)

    outreach = sub.add_parser("outreach", help="generate outreach for recorded leads")
    outreach.add_argument("--limit", type=int, default=3)
    outreach.add_argument("--instruction", help="extra steer, e.g. 'write in English'")
    outreach.set_defaults(func=_outreach)

    args = parser.parse_args(argv)
    configure_logging(get_settings())
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
