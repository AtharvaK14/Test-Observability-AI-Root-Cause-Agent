# Setup & Run Guide

Written for Windows + PowerShell, assuming nothing beyond "Python is installed".
Every command is copy-pasteable. Each step tells you what you should see, so you
can tell success from silent failure.

**Time:** ~10 minutes for the demo, ~20 including your own test suites.

---

## What you're about to run

Three things that stay **completely separate**:

```
Your test suites                    This project                  Your browser
─────────────────                   ────────────                  ────────────
Playwright / Cypress                A web service that       →    localhost:8000/docs
PyTest                         →    reads report files,
                                    stores them, and
produce a report file               explains failures
```

Your test projects are **never modified**. They produce a file; you upload the
file here. That's the only connection.

Two roles to keep straight:

| Terminal | What it does | When to close it |
|---|---|---|
| **Terminal 1** | Runs the server. Prints logs. | Leave open the whole time |
| **Terminal 2** | Where you type commands | Reuse freely |

---

# Part 1 — One-time setup

Do this once. Skip to Part 2 next time.

### 1.1 Open PowerShell in the project

```powershell
cd "F:\Projects\Test Observability + AI Root-Cause Agent"
```

The quotes matter — the folder name has spaces and `+` in it.

### 1.2 Create the virtual environment

A virtual environment keeps this project's packages separate from every other
Python project, so nothing you install here can break anything else.

```powershell
python -m venv .venv
```

No output means it worked. You now have a `.venv` folder.

> **Already have one?** If `.venv` exists, skip this. To start completely fresh:
> `Remove-Item -Recurse -Force .venv` then run the command above.

### 1.3 Install the packages

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Takes 1–2 minutes. Ends with `Successfully installed ...`.

> Note `.venv\Scripts\python -m pip` rather than plain `pip`. This guarantees
> you install into *this* project's environment even if you haven't "activated"
> anything. It is the reliable form; use it throughout.

### 1.4 Create your settings file

```powershell
Copy-Item .env.example .env
```

**No edits needed.** The defaults are the zero-setup path. Confirm they landed:

```powershell
Get-Content .env | Select-String -Pattern "^(DATABASE_URL|ANALYSIS_MODE)="
```

You should see:

```
DATABASE_URL=sqlite+pysqlite:///./demo.db
ANALYSIS_MODE=heuristic
```

What those two mean:

- **`sqlite+pysqlite:///./demo.db`** — the database is a single file, created
  automatically on first run. Nothing to install, no server.
- **`heuristic`** — failures are classified by deterministic rules. **No API key
  needed, nothing costs money.** Leave `ANTHROPIC_API_KEY` empty.

> ⚠️ If `DATABASE_URL` says `postgresql+psycopg://...`, you have an older copy of
> `.env.example`. That points at a PostgreSQL **server** you'd have to install
> and run — the app will fail at startup with a connection error. Replace that
> line with the SQLite one above. (PostgreSQL is the production setup and
> `docker compose up` configures it for you; you don't need it to run locally.)

### 1.5 Confirm everything works

```powershell
.venv\Scripts\python -m pytest
```

**Expected:** three rows of dots reaching `[100%]`, then `167 passed`, in about
4 seconds. Each dot is a passing test.

```
........................................................................ [ 43%]
........................................................................ [ 86%]
.......................                                                  [100%]
167 passed, 1 warning in 3.8s
```

The one warning is from a third-party library, not this project — ignore it.

If the summary scrolls past, this always tells you the truth:

```powershell
.venv\Scripts\python -m pytest; if ($LASTEXITCODE -eq 0) { "ALL TESTS PASSED" } else { "SOMETHING FAILED" }
```

Seeing `ALL TESTS PASSED` means your setup is correct and the rest of this guide
will work. If not, stop here — something is wrong with the install, and every
later step will fail confusingly.

---

# Part 2 — Run it with demo data

Start here every time.

### 2.1 Load demo data

```powershell
.venv\Scripts\python scripts\seed_demo.py --reset
```

**Expected:**
```
seeded 240 executions across 24 CI runs (38 failures)
10 scenarios, each ending in a failure with a known cause:

  app_bug              checkout_total   playwright
  app_bug              discount_api     pytest
  flaky_test           cart_badge       cypress
  ...
```

This creates three weeks of realistic test history across four frameworks, with
ten deliberately planted failures — one of each root-cause type. `--reset` wipes
anything already there.

### 2.2 Start the server — **Terminal 1**

```powershell
.venv\Scripts\python -m uvicorn backend.main:app --reload
```

**Expected:**
```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
service started   [analysis_mode=heuristic] [model=rules]
```

Check `analysis_mode=heuristic` — that confirms it isn't looking for an API key.

**Leave this terminal running.** Closing it stops the server. Open a **second**
PowerShell window for everything below.

### 2.3 Classify the failures — **Terminal 2**

```powershell
cd "F:\Projects\Test Observability + AI Root-Cause Agent"
.venv\Scripts\python scripts\analyze_pending.py
```

**Expected:**
```
38 failure(s) pending, mode=heuristic

  [1/38] app_bug              0.77  checkout.spec.ts > guest checkout > calculates
  [2/38] flaky_test           0.78  cart.cy.js > Cart > updates the item count badge
  ...
classified 38, errored 0
```

That's the system reading each failure's history, blast radius, and error text,
then deciding *why* it failed.

---

# Part 3 — Look at the results

### The easy way

Open **<http://localhost:8000/docs>** in your browser.

This is an interactive API explorer generated from the code. For any endpoint:
click it → **Try it out** → **Execute** → read the response.

Worth trying first:

| Endpoint | Shows you |
|---|---|
| `GET /api/analysis/summary` | Counts + the root-cause pie-chart data |
| `GET /api/analysis/failures` | Every failure with its verdict |
| `GET /api/analysis/clusters` | Recurring patterns — the real triage queue |
| `GET /api/analysis/flaky` | Which tests are least trustworthy |
| `GET /api/analysis/metrics` | Accuracy, token spend, confusion matrix |

For `summary` and most others, set `hours` to `999` — the demo data spans three
weeks and the default window is 24 hours.

### From PowerShell

Use `Invoke-RestMethod`, **not** `curl`. In PowerShell, `curl` is a confusing
alias for a different tool (see Troubleshooting).

```powershell
(Invoke-RestMethod "http://localhost:8000/api/analysis/summary?hours=999").root_cause_distribution | Format-Table
```

```powershell
(Invoke-RestMethod "http://localhost:8000/api/analysis/failures?hours=999&limit=10").items |
  ForEach-Object { "{0,-20} {1}" -f $_.analysis.root_cause, $_.test.test_name }
```

Read one full explanation, including the evidence the verdict rests on:

```powershell
$f = (Invoke-RestMethod "http://localhost:8000/api/analysis/failures?hours=999&limit=1").items[0]
$f.test.test_name
$f.analysis.reasoning
$f.analysis.key_evidence
$f.analysis.suggestions
```

---

# Part 4 — Use your own test suites

This is the step that makes the project real. Your suites need to produce a
**machine-readable report** — neither currently does, but you don't have to
change any config, just add a flag when running.

## 4.1 Playwright

```powershell
cd "F:\Projects\Dual-Framework-E2E-Test-Suite-Playwright-Cypress"
$env:PLAYWRIGHT_JSON_OUTPUT_NAME="results.json"
npx playwright test --config=playwright/playwright.config.ts --reporter=json
```

The env var is required — without it Playwright prints the JSON to the screen
instead of saving it.

⚠️ **The file lands in `playwright\results.json`, not the folder you ran from.**
`PLAYWRIGHT_JSON_OUTPUT_NAME` resolves relative to the *config's* directory. Find
it rather than assuming:

```powershell
Get-ChildItem -Recurse -Filter results.json | Where-Object { $_.FullName -notmatch "node_modules" } | Select-Object FullName
```

## 4.2 PyTest

```powershell
cd "F:\Projects\api-quality-gate"
.\venv\Scripts\python -m pytest --junitxml=results.xml
```

`--junitxml` is built into pytest — nothing to install, and your existing Allure
reporting keeps working alongside it.

## 4.3 Cypress

```powershell
cd "F:\Projects\Dual-Framework-E2E-Test-Suite-Playwright-Cypress"
npx cypress run --reporter junit --reporter-options "mochaFile=cypress-results/results-[hash].xml"
```

⚠️ **The `[hash]` is not optional.** Cypress runs each spec file in its own
process, and every one writes to the filename you gave. Without `[hash]` they
all target the same file and each overwrites the last — you silently end up with
only the final spec's results. (Tested: a 19-test run produced a file containing
5.) With `[hash]` you get one XML per spec.

Check the count matches what Cypress reported:

```powershell
Get-ChildItem cypress-results | Select-Object Name, Length
```

> If Cypress says it can't find the `junit` reporter:
> `npm install --save-dev mocha-junit-reporter` then re-run.

## 4.4 Upload them

Now use **`curl.exe`** — with the `.exe`, which matters (see Troubleshooting).

Give both uploads the **same `ci_run_id`**. That is what lets the system notice
"a Playwright test and a PyTest test failed together, so it probably isn't the
tests" — the strongest signal it has.

```powershell
cd "F:\Projects\Test Observability + AI Root-Cause Agent"
$run = "local-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

# Playwright — note the playwright/ subfolder
curl.exe -X POST "http://localhost:8000/ingest/playwright" `
  -F "file=@F:/Projects/Dual-Framework-E2E-Test-Suite-Playwright-Cypress/playwright/results.json" `
  -F "ci_run_id=$run" -F "environment=local"

# PyTest
curl.exe -X POST "http://localhost:8000/ingest/junit?framework=pytest" `
  -F "file=@F:/Projects/api-quality-gate/results.xml" `
  -F "ci_run_id=$run" -F "environment=local"

# Cypress — one file per spec, so loop over them
Get-ChildItem "F:\Projects\Dual-Framework-E2E-Test-Suite-Playwright-Cypress\cypress-results\*.xml" |
  ForEach-Object {
    $path = $_.FullName -replace '\\','/'
    curl.exe -X POST "http://localhost:8000/ingest/junit?framework=cypress" `
      -F "file=@$path" -F "ci_run_id=$run" -F "environment=local"
  }
```

**Expected:** JSON containing `"status":"success"` and `"ingested":<a number>`.

Two things that bite here:

- **Forward slashes in file paths.** Backslashes are escape characters to curl
  and break the path. The `-replace '\\','/'` in the loop handles this for you.
- **Add up the `ingested` numbers** and compare against what the test runner
  reported. If they disagree, a report file is missing or was overwritten — go
  back and check.

## 4.5 Make some failures

Your suites presumably pass, so you'll have ingested only green runs — and the
system's job is explaining *failures*.

Break something on purpose:

- **Playwright/Cypress:** change an expected text value, or point a selector at
  an element that doesn't exist
- **PyTest:** change an expected status code from `200` to `201`

Then re-run and re-upload. **Do this three or four times across a few different
changes**, so each test builds up a history. A single failure with no history is
the one case where every classifier struggles — the interesting signals
(*"this passed 20 times then broke"*, *"this alternates"*) need past runs to exist.

Uploads classify automatically. To catch anything missed:

```powershell
.venv\Scripts\python scripts\analyze_pending.py
```

---

# Part 5 — Optional: turn on Claude

Everything above is free. To use the AI agent instead of the rules:

1. Get a key from <https://console.anthropic.com>
2. In `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ANALYSIS_MODE=claude
   ANTHROPIC_MODEL=claude-sonnet-5
   ```
3. Restart Terminal 1 (`Ctrl+C`, then the uvicorn command again)
4. Re-analyse: `.venv\Scripts\python scripts\analyze_pending.py`

Roughly **$0.05 per analysis** on `claude-sonnet-5`. Check real spend at
`GET /api/analysis/metrics`.

**The comparison is the point.** Run both classifiers over the same failures:

```powershell
.venv\Scripts\python scripts\score_classifier.py                  # rules
.venv\Scripts\python scripts\score_classifier.py --mode claude    # agent
```

"The agent gets 82% where rules get 54%" is a result worth putting on a CV. A
single accuracy number with nothing to compare it against is not.

---

# Troubleshooting

### `curl: (26) Failed to open/read local data`

The file path doesn't exist. Check it:

```powershell
Test-Path "F:\Projects\api-quality-gate\results.xml"
```

`False` means you haven't generated the report yet (Part 4), or the path is
wrong. Also make sure you replaced any example path with your real one.

### `A parameter cannot be found that matches parameter name 'X'`

You used `curl` instead of `curl.exe`. In PowerShell, `curl` is an alias for
`Invoke-WebRequest`, a completely different tool that doesn't understand `-X`
or `-F`.

- **Uploading a file?** Use `curl.exe` (with `.exe`)
- **Just reading data?** Use `Invoke-RestMethod` — nicer output anyway

### `Security Warning: Script Execution Risk` when reading an endpoint

Same cause. `Invoke-WebRequest` is trying to parse JSON as a web page. Use
`Invoke-RestMethod` instead.

### `"ingested":0,"skipped_duplicates":5`

**Not an error.** You uploaded a file already stored under that `ci_run_id`. The
system deduplicates so a retried CI upload doesn't double every number. To
ingest it again as a separate run, use a new `ci_run_id`.

### `nothing pending — every failure already has an analysis`

Also not an error. Everything is already classified. Look at the results
(Part 3) or upload new failures.

### Port 8000 already in use

Something else is on that port, or a previous server is still running:

```powershell
Get-Process -Name python | Stop-Process -Force
```

Or run on a different port: `--port 8001` (and change the URLs to match).

### The server won't start / import errors

You're probably outside the virtual environment. Always use the full path form:

```powershell
.venv\Scripts\python -m uvicorn backend.main:app --reload
```

### Start completely over

```powershell
Remove-Item demo.db -ErrorAction SilentlyContinue
.venv\Scripts\python scripts\seed_demo.py --reset
```

---

# Command reference

| Task | Command |
|---|---|
| Run tests | `.venv\Scripts\python -m pytest` |
| Load demo data | `.venv\Scripts\python scripts\seed_demo.py --reset` |
| Start server | `.venv\Scripts\python -m uvicorn backend.main:app --reload` |
| Classify pending | `.venv\Scripts\python scripts\analyze_pending.py` |
| Preview without classifying | `.venv\Scripts\python scripts\analyze_pending.py --dry-run` |
| Score the classifier | `.venv\Scripts\python scripts\score_classifier.py` |
| Browse the API | <http://localhost:8000/docs> |
