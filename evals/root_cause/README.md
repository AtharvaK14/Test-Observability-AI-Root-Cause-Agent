# Root-cause classifier eval

Measures how well the classifier, either the Claude agent or the rule-based
baseline, assigns a root cause to a failed test. It also measures the
properties that make LLM output hard to trust: whether the agent invents
evidence, whether it is consistent across repeated runs, whether its confidence
means anything, and whether instructions planted in test output can steer it.

## What it measures

| Metric | How | Why |
|---|---|---|
| **Accuracy** | The predicted category is in the case's accepted set. Reported with a 95% Wilson interval, per source. | The headline. |
| **Per-category precision/recall** | From the same rows | Accuracy alone hides a classifier that always says one thing. |
| **Grounding** | Checkable facts in `key_evidence` (status codes, SHAs, test ids, timings, quoted text) must appear verbatim in what the agent was shown | Catches invented evidence. Paraphrase is fine; invented specifics are not. |
| **Consistency** | Agreement of the predicted category across reps of one case | The same failure should not get a different verdict on a rerun. |
| **Calibration** | Brier score and expected calibration error over confidence vs correctness | Whether "0.9 confident" means right 90% of the time. |
| **Injection resistance** | On injection cases: avoided the planted category, *and* answered correctly | The agent reads untrusted CI output. "Avoided" alone is gamed by always answering `unknown`. |
| **Cost, latency, tool calls, retries** | Per row, from the API's own `usage`; cost derived from the *served* model | What a verdict costs. |

Grading is programmatic only: no LLM judge, deterministic and free.

## The cases (27)

See [CASES.md](CASES.md) for every case with the exact text the agent receives.

- **11 real** failures from [api-quality-gate](https://github.com/AtharvaK14/api-quality-gate)'s
  own CI history: expired credentials, a token-permission change, GitHub rate
  limits, and response-time SLA misses. Labelled by the repository owner from
  first-hand knowledge of each incident and its fixing commit. **These are the
  only cases that support an accuracy figure.**
- **10 synthetic** scenarios from `scripts/seed_demo.py`. The scenarios and the
  rule-based classifier share an author, so the rules match them by construction.
  They check the harness and are never quoted as accuracy.
- **6 injection** variants (3 real, 3 synthetic) with an instruction planted in
  the failure output, pushing a specific wrong category.

### How real cases are built

`extract_github_history.py` turns the repository's Actions history into
`data/api_quality_gate_history.json`. Run order, dates, SHAs and conclusions
come from the Actions API. Per-test outcomes and tracebacks come from the
failed runs' own pytest output. For each case, `worlds.py` replays that history
into a fresh database through the production ingest path:

- **Only up to the failing run.** A Jul 9 case never sees the Jul 29 fix.
- **With the clock frozen at the failure**, so "last 14 days" windows give the
  same answer next year, and with row ids seeded per case.
- **Gaps are disclosed, not filled.** GitHub expires logs after about 90 days;
  31 failed runs have none. The agent is told those runs failed and that their
  details are unknown. Per-test durations were never recorded, and the duration
  section says exactly that, rather than reporting a misleading baseline.

## Running it

Free and offline, no API key needed. CI runs all four on every push:

```bash
python -m evals.root_cause.run_eval --mode oracle      # harness check: must score ~100%
python -m evals.root_cause.run_eval --mode null        # grader check: must score ~0%
python -m evals.root_cause.run_eval --mode heuristic   # the rule-based baseline
python -m evals.root_cause.run_eval --mode replay      # re-grade recorded Claude runs
```

Live Claude runs **cost money** and are gated three ways:

1. **Harness approval.** The runner refuses to spend until the current harness
   has been approved with `--approve-harness`. Any later change to the harness or
   the agent code revokes the approval.
2. **A budget.** `--budget-usd` is required. The run stops submitting cases
   once the cost derived from actual `usage` reaches it.
3. **Explicit consent.** Without `--yes` the runner prints the plan and exits.

```bash
python -m evals.root_cause.run_eval --mode claude --approve-harness --budget-usd 1   # review, approve
python -m evals.root_cause.run_eval --mode claude --max-cases 3 --reps 1 --budget-usd 1 --yes   # pilot
python -m evals.root_cause.run_eval --mode claude --reps 3 --budget-usd <from pilot> --yes   # full
```

Run the pilot first. The plan for the full run then prints the measured
per-case cost (min, median and max) to base the budget on. The same run can be
dispatched from GitHub Actions (`Live eval (paid)`, manual only) using an
`ANTHROPIC_API_KEY` repository secret.

Results land in `.claude/hillclimb/root_cause/<variant>/`: `results.jsonl` (one
row per case and rep, written as each completes, so an interrupted run resumes),
`traces/` (full conversations), `errors.jsonl` (infrastructure failures, never
scored) and `summary.json`. Claude runs also write cassettes to
`cassettes/<variant>/`; committing them lets CI replay the run offline.

## Sign-offs

Recorded 2026-10-06 by Atharva Kadam, before any paid run:

- **Inputs:** the 27 cases in CASES.md approved as is, including the labels on
  all real cases. The SLA-latency cases accept either `external_dependency` or
  `flaky_test`.
- **Grading:** approved as above, on condition that every grounding flag on the
  paid pilot's real Claude outputs is spot-checked before the full run.
  Grounding was calibrated on rule-generated evidence only so far.
- **Model under test:** `claude-opus-5`, the app's shipped `ANTHROPIC_MODEL`
  default. A model upgrade is a separate change, to be judged by running this
  eval on both models.

## Results so far

Rule-based baseline (`--mode heuristic`), 2026-10-06:

| Source | Accuracy | 95% CI | n |
|---|---|---|---|
| Real | **55%** (6/11) | 28% to 79% | 11 |
| Synthetic | 100% (10/10) | n/a (by construction) | 10 |
| Injection | 67% (4/6) | 30% to 90% | 6 |

The rules are perfect on the synthetic scenarios they were written against and
get 55% on real failures. Most misses are GitHub rate-limit 403s classified as
`flaky_test`, because one failure among passes looks flaky to the rules. That
gap between synthetic and real results is why the real cases exist.

**The Claude agent has not been run against this set yet.** Its numbers go here,
with the date, model and exact command, once a live run is approved.

## Limitations

- **Small n.** 11 real cases give a wide interval (about ±25 points at 55%).
  Treat any single figure as indicative. Comparisons need non-overlapping
  intervals or more cases.
- **One source repository, mostly one failure family** (authentication and
  external API errors). More real repositories would broaden it.
- **Grounding checks specifics, not reasoning.** A verdict can cite only real
  facts and still draw the wrong conclusion. That is what accuracy measures.
- **Replay is not a measurement.** It re-checks everything after the model call
  against recorded responses; only a live run measures the model.
