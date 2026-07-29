# Test Observability + AI Root-Cause Agent

Ingests test results from **Playwright, Cypress, PyTest and Selenium**, then uses
**Claude** to work out *why* each failure happened — app bug, flaky test,
environment, test data, infrastructure, or external dependency — with the
evidence it used and what to do about it.

> **Status:** backend complete — 167 tests, `mypy --strict` clean, verified
> end-to-end against real Playwright, Cypress and PyTest suites. Frontend not
> built yet.

---

## The problem

A CI run goes red. You get a binary signal and a wall of logs. Working out
whether the application broke, the test is flaky, or the environment wobbled
takes an hour of clicking through artefacts — per failure — and the answer is
usually "someone should look at this later".

The naive fix is to paste the error into an LLM. That plateaus fast, because
**the error message is the least discriminating evidence available**:

```
Timeout 30000ms exceeded waiting for locator('#place-order')
```

That exact string is emitted by a genuine application hang, a missing wait, a
CPU-starved runner, and a dead downstream service. No model can tell them apart
from the message alone, because the information is not in the message.

## What this does instead

It answers the questions a good SDET would ask *before* forming an opinion, and
hands the model the answers:

| Question | Signal | Why it discriminates |
|---|---|---|
| Has this test been reliable? | 30-run history, pass rate, streak | 200 green runs then red ⇒ something changed. Alternating for weeks ⇒ the test. |
| Did the code change? | Last passing commit vs failing commit | Passed *and* failed on the **same commit** ⇒ not a regression. Full stop. |
| What else broke? | Every other failure in the same CI run | One test ⇒ that feature. Whole suite ⇒ shared setup. **Different frameworks ⇒ the app or the environment**, because independent test code cannot break together by chance. |
| Was it waiting or dying? | Duration vs this test's passing baseline | Burned the timeout ⇒ waiting (race, slow dependency). Died in 200ms ⇒ genuinely absent. Same message, different owner. |
| Is this failure unique? | Normalised error fingerprint across all tests | One signature in unrelated tests ⇒ one shared cause, not N test defects. |
| Was the machine healthy? | CPU/memory/network at execution time | A 98%-CPU runner explains a timeout that no amount of reading the test will. |

Then the agent can **investigate further on its own** — pull the full stack
trace, grep the logs for `ECONNREFUSED`, check a sibling test's history — and
finally submit a schema-validated verdict.

---

> **New here?** [docs/SETUP.md](docs/SETUP.md) is a step-by-step walkthrough
> written for someone who has never run this before — every command, what you
> should see, and what to do when it goes wrong. The quick start below assumes
> you're comfortable with a terminal.

## Quick start — no API key, no Docker, no database to install

```bash
python -m venv .venv
```

Then, using the venv's interpreter directly — no activation needed, and it can't
silently fall back to system Python (`.venv/bin/python` on macOS/Linux):

```powershell
.venv/Scripts/python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env                        # defaults need no editing
.venv/Scripts/python scripts/seed_demo.py --reset  # 3 weeks of history, 10 planted failures
.venv/Scripts/python -m uvicorn backend.main:app --reload
```

Then in a **second** terminal — easy to miss, and without it the dashboard has
data but no verdicts:

```powershell
.venv/Scripts/python scripts/analyze_pending.py    # classify every failure
```

- API + interactive docs: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health/ready>

That runs the **whole pipeline** — ingest, fingerprint, cluster, classify,
trends, feedback — on SQLite with a rule-based classifier. Zero cost, zero
network, no API key, no Postgres.

### With Claude

Set `ANTHROPIC_API_KEY` and `ANALYSIS_MODE=claude`. Roughly $0.05 per analysis
on `claude-sonnet-5`, $0.13 on `claude-opus-5`; `/api/analysis/metrics` reports
your actual token spend.

### With PostgreSQL

```bash
docker compose up --build      # provisions Postgres + the API
```

### Uploading a real report

```bash
curl -X POST http://localhost:8000/ingest/playwright \
  -F "file=@playwright-report.json" \
  -F "ci_run_id=local-1" -F "git_commit=$(git rev-parse HEAD)" \
  -F "git_branch=$(git rev-parse --abbrev-ref HEAD)"

curl "http://localhost:8000/api/analysis/failures?hours=24"
```

## Two classifiers, and why that matters

| | `ANALYSIS_MODE=heuristic` | `ANALYSIS_MODE=claude` |
|---|---|---|
| Needs an API key | no | yes |
| Cost per analysis | £0 | ~$0.05–0.13 |
| Reads the stack trace | no | yes |
| Judges whether a locator is brittle | no | yes |
| Confidence ceiling | 0.80 | unbounded |

The rule-based classifier is not a downgrade — it is the **control group**.
"The agent classifies 82% correctly" is unfalsifiable on its own; "the agent
gets 82% where deterministic rules get 54%" is a result. Verdicts from each are
tagged with distinct `prompt_version` values so their accuracies stay separable.

```bash
python scripts/score_classifier.py                 # rule-based
python scripts/score_classifier.py --mode claude   # the agent (costs money)
```

> ⚠️ The seeded scenarios and the heuristic rules share an author, so the rules
> match the plants by construction and score near-perfectly. That validates the
> **pipeline**, not the classifier. A quotable accuracy figure needs real
> failures adjudicated through the feedback endpoint.

---

## Architecture

```
CI (Playwright / Cypress / PyTest / Selenium)
   │  POST /ingest/{framework}  + git sha, branch, ci_run_id
   ▼
┌──────────────────────────────────────────────────────────┐
│ Parsers          backend/ingest/                         │
│   framework JSON/XML -> one normalised TestResultCreate   │
│   one row per RETRY ATTEMPT (fail→pass = FLAKY)          │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│ Ingestion        backend/ingest/service.py               │
│   fingerprint · dedupe (idempotent) · cluster · queue     │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│ Context          backend/analysis/context_retriever.py   │
│   history · blast radius · duration ratio · signature     │
│   spread · commit range · host metrics                   │
│   ← measured only; never fabricated                      │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│ Agent            backend/analysis/agent.py               │
│   Claude + 5 retrieval tools + strict verdict schema     │
│   loop until submit_classification, max 4 turns          │
└───────────────────────────┬──────────────────────────────┘
                            ▼
     PostgreSQL  →  GET /api/analysis/*  →  dashboard
                            ▲
                            └── POST feedback → accuracy + confusion matrix
```

### Layout

```
backend/
  models/      Pydantic schemas + shared enums  (the wire contract)
  db/          SQLAlchemy models, session, repositories  (the storage contract)
  ingest/      Per-framework parsers + the ingestion service
  analysis/    clustering.py · context_retriever.py · agent.py · heuristics.py
  api/         ingest.py (write) · analysis.py (read)
  tests/       167 tests, SQLite + stubbed client, no network
scripts/       seed_demo · analyze_pending · score_classifier · dump_schema
db/            schema.sql (generated) · migrations/
ci/            Dockerfile + example GitHub Actions workflow
docs/          SETUP.md — step-by-step walkthrough
```

---

## Design decisions worth defending

**Never fabricate context.** The reference design in the spec returns hard-coded
`cpu_percent: 65.2` and an invented `changed_files` list. That is worse than
returning nothing: a model reasoning over fabricated evidence produces a
confident, well-argued, wrong answer, and the confidence score makes it look
trustworthy. Every field here is measured or explicitly marked unavailable, and
the model is told which is which.

**Clustering is deterministic and LLM-free.** 487 red tests overnight is
normally 3–4 actual problems. Collapsing them needs a hash, not a model — so it
keeps working during an API outage, costs nothing, and is unit-testable. It also
cuts spend by two orders of magnitude if you analyse one representative per
cluster.

**One row per retry attempt.** A test that fails then passes on retry is
reported by every runner as a pass, the suite goes green, and nobody looks
again — which is exactly the failure worth investigating. The final attempt is
stored as `FLAKY`, not `PASSED`. Dashboard counts collapse retry groups back to
one row so a single flaky test is not counted three times.

**Analyses are append-only.** Re-running the agent adds a row instead of
overwriting one, so "did prompt v3 classify better than v2 on the same
failures?" is answerable. Every row carries its `prompt_version`, model, token
counts and latency.

**Feedback records the correction, not just the complaint.** "Wrong" teaches
nothing. "You said `flaky_test`, it was `test_data`" is a labelled example, and
the aggregate is a ranked confusion matrix on `/api/analysis/metrics` — the
prompt-improvement backlog, ordered by how often each confusion actually
happens.

**Endpoints are `def`, not `async def`.** They do blocking database work, so
Starlette runs them in a threadpool. `async def` around a blocking driver call
stalls the entire event loop — the most common FastAPI performance bug, and one
the spec's sample code contains.

**Structured output via a `strict` tool, not text parsing.** The spec searches
the response for a marker and slices between the first `{` and last `}`. That
breaks the first time the model writes a brace in its prose, and breaks
silently. Here the verdict arrives as a schema-validated tool call and is
re-validated by Pydantic; an invalid one is handed back to the model to correct.

---

## API

### Ingest (what CI calls)

| Endpoint | Accepts |
|---|---|
| `POST /ingest/playwright` | `playwright test --reporter=json` |
| `POST /ingest/cypress` | module API, mochawesome, or mocha JSON — auto-detected |
| `POST /ingest/pytest` | `pytest --json-report` |
| `POST /ingest/selenium` | JUnit/TestNG XML or normalised JSON — sniffed |
| `POST /ingest/junit?framework=…` | any JUnit XML; framework attribution required |
| `POST /ingest/results` | already-normalised JSON body |

Form fields on every upload: `ci_run_id`, `environment`, `git_commit`,
`git_branch`, `ci_provider`, `ci_job_url`.

Responses report `ingested`, `skipped_duplicates`, `failures_detected`,
`analyses_queued` and any per-entry warnings — a pipeline told `{"status":
"success"}` while 380 of 400 results were dropped is worse than no observability
at all.

### Analysis (what the dashboard calls)

| Endpoint | Returns |
|---|---|
| `GET /api/analysis/summary` | counters, root-cause distribution, framework breakdown |
| `GET /api/analysis/failures` | paginated feed with verdicts; filter by framework, root cause, review status |
| `GET /api/analysis/failures/{id}` | one failure: every verdict, its cluster, its CI-run siblings |
| `GET /api/analysis/tests/{name}/trends` | daily pass rate + a **verdict**: healthy / flaky / broken / degrading |
| `GET /api/analysis/flaky` | flakiness leaderboard (with a `min_runs` floor) |
| `GET /api/analysis/clusters` | recurring patterns, biggest first — the real triage queue |
| `GET /api/analysis/metrics` | accuracy, feedback coverage, confusion matrix, token spend, latency |
| `POST /api/analysis/analyses/{id}/feedback` | adjudicate a verdict (upsert per reviewer) |
| `POST /api/analysis/clusters/{id}/mute` | acknowledge a known issue; stops re-analysis |

`GET /api/analysis/queue` lists failures awaiting analysis — also the recovery
path after a restart, since background tasks die with the process.

---

## Flakiness, measured properly

Flakiness here is **instability**, not failure rate: the share of consecutive
run pairs whose outcome changed.

- alternates pass/fail/pass/fail → score ≈ 1.0 → **flaky**
- fails every single run → score 0.0 → **broken**, not flaky

Both need attention, but they need *different* attention, and calling a
reliably-broken test "50% flaky" sends someone hunting for a race condition that
does not exist. The leaderboard also enforces a `min_runs` floor, because a test
that ran twice and failed once is not 50% flaky — it is unmeasured.

---

## Configuration

Everything is in [.env.example](.env.example). The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Unset ⇒ ingestion works, analysis is skipped |
| `ANTHROPIC_MODEL` | `claude-opus-5` | `claude-sonnet-5` to trade depth for cost at volume |
| `ANTHROPIC_EFFORT` | `high` | `low`…`max` |
| `AGENT_ENABLED` | `true` | Kill switch — zero API spend when false |
| `AUTO_ANALYZE_ON_INGEST` | `true` | Otherwise analyse on demand only |
| `AGENT_MAX_ITERATIONS` | `4` | Tool-use turns before a verdict is forced |
| `GIT_REPO_PATH` | unset | Optional checkout for real commit metadata |

Cost controls, in order of impact: mute known clusters, disable
`AUTO_ANALYZE_ON_INGEST` and analyse per cluster, lower `ANTHROPIC_EFFORT`, or
switch model. Non-final retry attempts are never analysed.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest                                        # 167 tests, no network, no key
mypy                                          # strict, clean
ruff check backend/ scripts/
python scripts/dump_schema.py > db/schema.sql # after any model change
```

Scripts:

| | |
|---|---|
| `seed_demo.py --reset` | 3 weeks of labelled history across 4 frameworks |
| `analyze_pending.py` | Classify every unanalysed failure. Also the restart-recovery path — background tasks die with the process |
| `score_classifier.py [--mode claude]` | Score a classifier against the seeded ground truth |
| `dump_schema.py` | Regenerate `db/schema.sql` from the ORM |

The test suite runs entirely on in-memory SQLite with a stubbed Anthropic
client. That is deliberate: a suite that needs Postgres running and a funded API
key is a suite that gets skipped, and a skipped suite protects nothing. A
`TypeDecorator` forces UTC-aware datetimes on both dialects so the SQLite path
behaves like production instead of hiding timezone bugs.

### Migrations

`db/schema.sql` is generated from the ORM and applied automatically by
docker-compose on first start — fresh installs need nothing else. For an
existing database, use Alembic:

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

No initial revision is checked in on purpose: autogenerate reads its dialect
from the connected database, so one generated against SQLite would emit SQLite
types (no `JSONB`, no `TIMESTAMPTZ`) and break on Postgres. Generate it against
the real target.

---

## CI integration

[ci/github-actions/ingest-test-results.yml](ci/github-actions/ingest-test-results.yml)
is a working example. Two things in it are easy to get wrong:

- It uses `workflow_run.head_sha`, **not** `github.sha`. A `workflow_run` event
  fires on a different ref, so `github.sha` is the wrong commit — and a wrong
  commit silently breaks the pass→fail range inference.
- It runs `if: always()`. A pipeline that only uploads results when it passes
  never uploads a single failure.

Ingestion never fails the build. Observability that can break a deploy gets
switched off within a week.

---

## Not built yet

- **Frontend.** The API is complete and documented; the React dashboard is next.
- **Durable job queue.** Analysis runs as a FastAPI background task, which dies
  with the process. `GET /api/analysis/queue` makes stranded work visible and
  `scripts/analyze_pending.py` drains it, but real volume wants Celery or RQ.
- **Auth.** No authentication on any endpoint. Fine behind a VPN, not on the
  open internet.
- **Vector similarity.** Clustering is exact-hash. Near-miss failures that
  normalise differently stay in separate clusters; embeddings would catch them.
- **Real accuracy numbers.** The measurement machinery is built and tested, and
  the parsers are verified against real Playwright, Cypress and PyTest output —
  but publishing an accuracy figure requires adjudicating a meaningful number of
  real failures, which is in progress. `/api/analysis/metrics` reports
  `feedback_coverage` alongside `accuracy`, so 100% over two reviewed analyses is
  visibly what it is rather than a headline.
- **A live Claude call.** The agent's request shape is built against the current
  API and verified against a stubbed client across 26 tests (refusals, rate
  limits, malformed verdicts, loop exhaustion), but the LLM path has not yet been
  exercised against the real API. The rule-based classifier has.
