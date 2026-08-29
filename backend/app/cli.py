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

from app.config import get_settings
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

    args = parser.parse_args(argv)
    configure_logging(get_settings())
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
