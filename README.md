# Lead Research Agent

An AI agent that researches real businesses against real sources, scores them
as sales leads, and **records what it could not verify** instead of guessing.

You type a request in plain language:

> Find 10 beauty salons in Sarajevo that don't have online booking

The agent decides for itself which tools to use — searching OpenStreetMap,
fetching business websites, checking for booking systems, searching the open
web when a directory listing has no site — then scores each business against
configurable rules and drafts a personalised outreach message.

Every field it collects carries its provenance: **verified** (read from a named
source), **inferred** (the model's judgement from evidence it saw), or
**unverified** (looked for, not confirmed).

<!-- Replace with your own capture: see "Screenshots" below. -->
<!-- ![Dashboard](docs/screenshots/dashboard.png) -->

---

## The problem

Finding qualified B2B leads is manual. You search a category in a city, open
each business one at a time, check whether they have a website, whether it's
any good, whether they take bookings online, whether they're alive on social
media — then write an email. Hours of tab-switching that produces a
spreadsheet.

The obvious fix is to point an LLM at it, and the obvious fix fails for a
specific reason: **an LLM asked to produce a lead record will produce a
complete one.** Ask for a phone number and you get a plausible phone number.
The failure is invisible until someone dials it, and one fabricated field
destroys trust in every other field on the list.

## The solution

This system is built so that fabrication is *structurally impossible* rather
than discouraged, and so that "I don't know" is a first-class answer.

Three mechanisms do most of that work:

**The model never supplies facts.** Tools record what they observed into a
run workspace, keyed by a short handle. The model refers to businesses by
handle. `save_lead` accepts a handle and three pieces of prose — it has **no
parameter** for a name, phone number, website or booking status, so a
fabricated value has nowhere to enter. What the model contributes is
judgement: why a lead is worth approaching, and what to say.

**A claim can never be stronger than its evidence.** Every value is wrapped in
a `Fact[T]` that fails validation if it claims to be verified without naming a
source, or claims to be unverified while carrying a value. The distinction
between "this business has no phone number" and "we could not establish one"
survives all the way to the CSV export.

**Prose is checked too.** The three free-text fields reach the user verbatim,
so they are scanned for claims the facts don't support — an invented star
rating, a review count, a booking system that was never identified.

---

## Architecture

```
                            Browser
                               │
              fetch (JSON)     │     EventSource (SSE)
                               ▼
         ┌─────────────────────────────────────────┐
         │  Vercel — Next.js 16                    │
         │  Dashboard · live transcript · export   │
         └────────────────────┬────────────────────┘
                              │ HTTPS
                              ▼
         ┌─────────────────────────────────────────┐
         │  Render — FastAPI                       │
         │                                         │
         │  API routes + SSE streaming             │
         │  AgentRuntime (one interface)           │
         │    ├─ AgentSDKRuntime      [local]      │
         │    ├─ ReplayRuntime        [deployed]   │
         │    └─ MessagesAPIRuntime   [reference]  │
         │  Tool registry (7 tools)                │
         │  Providers · Enrichment · Scoring       │
         └────────────────────┬────────────────────┘
                              ▼
         ┌─────────────────────────────────────────┐
         │  Neon — PostgreSQL                      │
         │  tasks · agent_runs · leads             │
         │  sources · run_events                   │
         └─────────────────────────────────────────┘
```

### Three runtimes, one interface

Nothing above `app/agent/` imports an LLM client. Everything depends on an
`AgentRuntime` protocol and a shared event vocabulary, which makes the
implementations genuinely interchangeable:

| Runtime | Where | What it does |
|---|---|---|
| `AgentSDKRuntime` | local | The Claude Agent SDK drives the tool loop |
| `ReplayRuntime` | deployed | Re-emits a recorded run — no model, no credentials |
| `MessagesAPIRuntime` | reference | The tool loop written out by hand |

The third exists because the SDK runs the loop *for* you, so the loop is never
visible. It's implemented directly over the same seven tools, is exercised
entirely by scripted responses, and documents the mechanism the SDK hides.

### Why the deployed instance replays

Anthropic's usage policy permits ordinary individual use of the Agent SDK on a
Claude subscription, but prohibits routing plan credentials on behalf of other
users — which is what a public deployment answering strangers' requests would
be. So production serves recorded runs.

That invariant is enforced three independent ways, each with a test:

1. `config.py` refuses to start with the live runtime when `APP_ENV=production`
2. The deployment installs the base dependency set, not the `[agent]` extra —
   the Claude libraries are simply **absent**
3. The container image omits Node and the Claude CLI

The recording is a real run against real Sarajevo businesses: the events,
timings, leads, sources and provenance all come from an actual execution.

---

## Agent workflow

```
User request
     │
     ▼
search_businesses ──────────► OpenStreetMap (Overpass + Nominatim)
     │                        returns handles: b1, b2, b3 …
     ▼
lookup_business_details ────► fetch site · detect booking · read signals
     │                        (WebSearch first if no site is listed)
     ▼
score_lead ─────────────────► deterministic rules from rules.yaml
     │
     ▼
save_lead ──────────────────► facts from the workspace,
     │                        reasoning from the model
     ▼
PostgreSQL ─── SSE ────────► Dashboard
```

An actual run, recorded:

```
39 turns · 38 tool calls · 30 businesses examined · 2 leads saved
ToolSearch 2 · search_businesses 3 · lookup_business_details 5
fetch_website 5 · WebSearch 19 · score_lead 2 · save_lead 2
```

Two leads from thirty businesses, and the agent explained why: only 4 of 30
listings had a website, three of those domains were dead, and four salons
turned out to use a Bosnian booking platform. **It refused to pad the list** —
which is the correct behaviour, and the reason the numbers above are worth
showing rather than hiding.

That same run also found a booking provider the signature list didn't know
(`sredime.ba`) and correctly skipped the salons using it. The provider has
since been added.

---

## Tools

Each is a plain async function with a JSON schema; the SDK adapter is the only
place an LLM library appears.

| Tool | Purpose |
|---|---|
| `search_businesses` | Find businesses by category and city via OpenStreetMap |
| `fetch_website` | Fetch a page — robots.txt honoured, size-capped, streamed |
| `extract_page_content` | Full text of an already-fetched page |
| `detect_booking_system` | Match 20 booking providers plus generic call-to-action patterns |
| `lookup_business_details` | The whole per-business sequence in one call |
| `score_lead` | Score against the configured rules, with a breakdown |
| `save_lead` | Persist — facts from the workspace, prose from the model |

The agent may also use the built-in `WebSearch`. `WebFetch` is **blocked**:
reading must go through `fetch_website`, which honours robots.txt and records
provenance, so anything read is citable. Filesystem and shell tools are denied
explicitly as well as omitted from the allowlist.

### Booking detection is a regex, not a prompt

The premise of the flagship query is finding businesses *without* online
booking, so this is the highest-value signal in the system — and it is
deliberately not delegated to the model. Booking systems announce themselves
in markup (a Calendly iframe, a Fresha script tag), so the answer is a lookup
rather than a judgement: free, and identical on every run.

It returns three answers, and the distinction is the point:

- `true` + a named provider → hard evidence
- `false` → the page was read and neither a provider nor a call to action was found
- `null` → **the page could not be read**, which is not the same as "no booking"

A dead domain must never become the claim "this salon has no online booking" —
that's precisely what the user is acting on.

---

## Scoring

Arithmetic over facts, in Python rather than by the model. The same lead scores
the same every time, every point is attributed to a named rule, and it costs
nothing. What the model contributes is the sentence explaining the score.

Weights live in [`rules.yaml`](backend/app/scoring/rules.yaml) and can be
retuned without a redeploy:

```yaml
- id: no_online_booking
  points: 30
  when: { fact: has_online_booking, equals: false }
```

Rules awarding points for hard signals gate on **verified** provenance, so the
model cannot inflate a score with its own inferences.

Profiles make rules *mandatory*. Under `no_online_booking`, a 90-point lead
that uses Booksy is disqualified rather than merely ranked lower — it's the
wrong answer to the question asked. An *unverified* booking status disqualifies
too: unknown is not absent.

---

## Tech stack

| Layer | Choice |
|---|---|
| Frontend | Next.js 16, React 19, TypeScript, Tailwind v4, SWR |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic |
| Agent | Claude Agent SDK · Anthropic Messages API |
| Database | PostgreSQL (JSONB) |
| Data | OpenStreetMap Overpass + Nominatim, trafilatura, selectolax |
| Hosting | Vercel · Render · Neon — all free tiers |

**Every external data source is free and keyless.** The project runs at zero
external API cost by design.

---

## Database design

```
tasks ──┬─< agent_runs ──< run_events
        └─< leads ──< sources
```

| Table | Holds |
|---|---|
| `tasks` | One natural-language request |
| `agent_runs` | One execution attempt, with turn/token/cost accounting |
| `leads` | A discovered business and its provenance-wrapped fields |
| `sources` | Citations — what makes each claim checkable |
| `run_events` | Append-only event log driving SSE and replay |

Two decisions worth explaining:

**`leads` has no `website`, `phone` or `email` column.** Those live in a JSONB
document so each value carries its own source and provenance. A bare string
cannot express "we read this on their site" versus "the model guessed", and a
test asserts they are never promoted to flat columns.

**`run_events.id` is `BIGSERIAL`, not a UUID.** It's surfaced directly as the
SSE `Last-Event-ID`, so reconnect needs a monotonic, orderable value. A
timestamp would tie on events written in the same millisecond, which happens
constantly.

---

## Example workflow

```bash
# Find businesses, no model involved
python -m app.cli search "beauty salons" "Sarajevo" --limit 30

# Fetch their websites and report what can be established deterministically
python -m app.cli inspect --from-fixture "beauty salons"

# Run the agent for real, recording the run for replay
python -m app.cli run "Find 5 beauty salons in Sarajevo without online booking" \
    --target 5 --record "sarajevo demo"

# Re-score recorded leads after editing rules.yaml — free, no model
python -m app.cli score --profile no_online_booking

# Regenerate outreach with structured outputs
python -m app.cli outreach --limit 3
```

---

## Screenshots

Capture these from a local run and drop them in `docs/screenshots/`:

| File | What to show |
|---|---|
| `dashboard.png` | Lead list with scores and provenance bars |
| `transcript.png` | The live agent transcript mid-run |
| `lead-detail.png` | Evidence panel — facts, provenance chips, sources |

---

## Setup

**Requirements:** Python 3.12+, Node 20+, PostgreSQL 16+ (or a free
[Neon](https://neon.tech) database). For live agent runs: Node and the
[Claude Code CLI](https://code.claude.com), logged in with a Claude
subscription.

```bash
git clone https://github.com/tarikfilipovic1234-maker/genleadai.git
cd genleadai

# Backend
cd backend
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -e ".[agent,dev]"
cp .env.example .env                                # then set DATABASE_URL
alembic upgrade head
uvicorn app.main:app --reload

# Frontend, in a second terminal
cd ..
npm install
cp .env.local.example .env.local
npm run dev
```

Open <http://localhost:3000>. Check <http://127.0.0.1:8000/health> shows
`"database": {"reachable": true}`.

```bash
cd backend && pytest        # 339 tests, no network or database required
```

The suite runs against in-memory SQLite via dialect variants, so it needs no
infrastructure.

### Authentication for live runs

The Agent SDK inherits your Claude Code login — usually nothing to configure.
Verify with:

```bash
python scripts/smoke_agent.py
```

> **Do not set `ANTHROPIC_API_KEY`.** The SDK prefers it over your
> subscription and would silently bill Console credits instead. `config.py`
> refuses to start if it is present.

---

## Environment variables

### Backend

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `local` | `production` enables the deployment guards |
| `DATABASE_URL` | localhost | Paste a provider's string verbatim; scheme and libpq params are normalised |
| `AGENT_RUNTIME` | `sdk` | `sdk` · `replay` · `manual` |
| `CLAUDE_MODEL` | *(unset)* | Unset inherits your plan's default |
| `AGENT_MAX_TURNS` | `40` | Runaway guard |
| `SEARCH_PROVIDER` | `overpass` | `overpass` · `fixture` |
| `CORS_ORIGINS` | `localhost:3000` | Bare URL, comma-separated, or JSON array |
| `HTTP_USER_AGENT` | project string | Required by Nominatim's usage policy |
| `HTTP_USE_CACHE` | `true` | Disk cache so development never re-hits donated infrastructure |
| `LOG_FORMAT` | `console` | `json` in production |

### Frontend

| Variable | Notes |
|---|---|
| `NEXT_PUBLIC_API_URL` | Backend base URL. **Compiled in at build time** — changing it requires a redeploy |

---

## Deployment

**Neon** — create a project, copy the **direct** (not pooled) connection
string. Neon's pooled endpoint runs PgBouncer in transaction mode, which
conflicts with asyncpg's prepared statements.

**Render** — New → Blueprint → select the repo. `render.yaml` configures
everything; supply `DATABASE_URL`, `CORS_ORIGINS` and `HTTP_USER_AGENT`.
Migrations run at start, not at build, so a configuration mistake is reported
as a configuration mistake rather than a build failure.

**Vercel** — import the repo, set `NEXT_PUBLIC_API_URL` to the Render URL
(`https://`, no trailing slash). Then put the Vercel URL into Render's
`CORS_ORIGINS`.

---

## Limitations

Stated plainly, because a portfolio project that hides its gaps is less
convincing than one that names them.

**No Google ratings or review counts.** No free source carries them, so both
fields are permanently unverified. That's the honest outcome under a zero-cost
constraint, and the interface says so rather than leaving a blank cell.

**Instagram activity is inferred, not measured.** There is no free API for it.
A profile link found in OSM or on a business's own site is verified; whether
that profile is *active* is a judgement from weak signals.

**OpenStreetMap coverage is thin.** In one Sarajevo run only 4 of 30 salons
listed a website and three of those domains were dead. That's the dominant
constraint on result quality, and it's why the agent uses web search to fill
gaps.

**The public API has no authentication.** Anyone with the URL can start a
replay or delete a lead. Deliberate — it's a demo that should open without a
login, holds no personal data and no credentials, and the rate limiter caps
abuse at 5 runs per 5 minutes per IP. A real deployment would need auth.

**The rate limiter is in-process.** Correct for a single free-tier instance;
a second instance would enforce half the limit each, and the counter would
belong in Redis.

**Free tiers sleep.** Render idles after ~15 minutes; the first request then
takes 30–60 seconds. The interface warns about this.

**Deduplication is per task, not global.** A business found by two different
searches appears in both result sets, by design.

---

## Future improvements

- **Parallel supervisor.** Per-business work is already isolated behind
  `research_business`, so fanning out under a semaphore is an orchestration
  change rather than a rewrite.
- **Paid data providers** behind the existing `SearchProvider` protocol —
  Google Places or Serper is one new class and one setting.
- **Playwright rendering** for JavaScript-only sites, which currently extract
  almost nothing.
- **Vector deduplication** across runs, to recognise a business seen in a
  previous task.
- **CRM export** — webhook or HubSpot integration rather than CSV.
- **Prompt evaluation harness.** The recorded runs are already deterministic
  fixtures; scoring prompt changes against them is the natural next step.

---

## Repository layout

```
app/                      Next.js dashboard
components/               UI — provenance rendering, transcript, lead detail
lib/                      API client, types, SSE hook
backend/
  app/
    agent/                runtimes, tools, workspace, prompts, recorder
    providers/            OpenStreetMap and fixture data sources
    enrichment/           fetching, extraction, booking detection
    scoring/              rules engine + rules.yaml
    schemas/              provenance types and API shapes
    db/                   models, migrations, repository
    api/                  routes, SSE, background runner, middleware
  fixtures/               recorded runs and business data
  tests/                  339 tests
```

---

## Licence

MIT
