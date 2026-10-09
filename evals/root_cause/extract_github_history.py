"""Rebuild a real CI history fixture from a repository's GitHub Actions runs.

    python -m evals.root_cause.extract_github_history \\
        --repo AtharvaK14/api-quality-gate --workflow "API Quality Gate" \\
        --out evals/root_cause/data/api_quality_gate_history.json

Real failures are the only cases that can support an accuracy number (see
scripts/seed_demo.py for why the seeded ones cannot). This turns a repository's
Actions history into a fixture the eval replays into the database, so the agent
sees the history a production ingest would have built — nothing invented:

- Run order, dates, commit SHAs and conclusions come from the Actions API.
- Per-test outcomes, assertion messages and tracebacks come from the failed
  runs' own pytest output (``gh run view --log-failed``).
- A *successful* run has no failed-step log. Its tests are recorded as passed,
  using the test list observed in that commit's logged runs. That is the one
  inference made, and the fixture marks those runs ``reconstructed: true``.
- A failed run whose log has expired (GitHub keeps them ~90 days) is kept as a
  ``log_expired`` gap rather than guessed at.
- Per-test durations are not in this output (no ``--durations``), so none are
  recorded. The world builder states that to the model instead of implying a
  baseline exists.

Downloaded logs are cached under ``--cache`` so re-running costs no API calls.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# GitHub Actions log lines are "<job>\t<step>\t<ISO timestamp> <text>"; the
# first line of a step may carry a byte-order mark before the timestamp.
_LOG_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t﻿?\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")

# Serial:  tests/a.py::Cls::test_x PASSED [ 12%]
# xdist:   [gw3] [ 88%] PASSED tests/a.py::Cls::test_x
_SERIAL = re.compile(r"^(tests/\S+::\S+) (PASSED|FAILED|ERROR|SKIPPED)\b")
_XDIST = re.compile(r"^\[gw\d+\] \[\s*\d+%\] (PASSED|FAILED|ERROR|SKIPPED) (tests/\S+::\S+)")
_SUMMARY = re.compile(r"^(FAILED|ERROR) (tests/\S+::\S+)(?: - (.*))?$")
# pytest shortens the underscore banner for long names, down to a single "_"
# each side, so the count is not a usable signal; the Class.test shape is.
_SECTION = re.compile(r"^_+ ([A-Za-z_]\w*\.\w+)(?:\[.*\])? _+$")
_BANNER = re.compile(r"^={5,} (.*?) ={5,}$")

_STATUS = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error", "SKIPPED": "skipped"}


def strip_prefixes(raw: str) -> list[str]:
    return [_LOG_PREFIX.sub("", line) for line in raw.splitlines()]


def parse_pytest_log(raw: str) -> dict[str, dict[str, Any]]:
    """Per-test outcomes from one run's pytest output, keyed by node id."""
    lines = strip_prefixes(raw)
    tests: dict[str, dict[str, Any]] = {}

    for line in lines:
        if m := _SERIAL.match(line):
            tests.setdefault(m.group(1), {})["status"] = _STATUS[m.group(2)]
        elif m := _XDIST.match(line):
            tests.setdefault(m.group(2), {})["status"] = _STATUS[m.group(1)]
        elif m := _SUMMARY.match(line):
            entry = tests.setdefault(m.group(2), {})
            entry.setdefault("status", _STATUS[m.group(1)])
            if m.group(3):
                entry["error_message"] = m.group(3).strip()

    # Tracebacks: each "___ Cls.test_name ___" section runs until the next
    # section or banner. Matched back to node ids by their Cls::test suffix.
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        if m := _SECTION.match(line):
            current = m.group(1)
            sections[current] = []
        elif _BANNER.match(line):
            current = None
        elif current is not None:
            sections[current].append(line)

    for nodeid, entry in tests.items():
        suffix = ".".join(nodeid.split("::")[1:])
        body = sections.get(suffix)
        if body is None:
            continue
        text = "\n".join(body).strip()
        captured = text.find("- Captured ")
        if captured != -1:
            entry["logs"] = text[captured:].strip()
            text = text[:captured].strip()
        entry["stack_trace"] = text
        if "error_message" not in entry:
            e_lines = [ln[1:].strip() for ln in body if ln.startswith("E ")]
            if e_lines:
                entry["error_message"] = e_lines[0]
    return tests


def _gh(*args: str) -> str:
    done = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or done.stdout.strip())
    return done.stdout


def fetch_log(repo: str, run_id: int, cache: Path) -> str | None:
    """A failed run's log, from cache or the API. None when GitHub no longer has it."""
    path = cache / f"{run_id}.log"
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        try:
            text = _gh("run", "view", str(run_id), "-R", repo, "--log-failed")
        except RuntimeError as exc:
            text = f"__unavailable__ {exc}"
        cache.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if text.startswith("__unavailable__") or "failed to get run log" in text[:200]:
        return None
    return text


def build_history(repo: str, workflow: str, cache: Path, limit: int) -> dict[str, Any]:
    runs_json = _gh(
        "run", "list", "-R", repo, "--workflow", workflow, "--limit", str(limit),
        "--json", "databaseId,conclusion,createdAt,headSha,event,displayTitle",
    )
    runs = sorted(json.loads(runs_json), key=lambda r: r["createdAt"])

    out_runs: list[dict[str, Any]] = []
    for run in runs:
        record: dict[str, Any] = {
            "run_id": run["databaseId"],
            "created_at": run["createdAt"],
            "sha": run["headSha"],
            "event": run["event"],
            "title": run["displayTitle"],
            "conclusion": run["conclusion"],
        }
        if run["conclusion"] == "failure":
            log = fetch_log(repo, run["databaseId"], cache)
            if log is None:
                record["log_expired"] = True
            else:
                record["tests"] = parse_pytest_log(log)
        out_runs.append(record)

    # The test list per commit, from whatever logged runs exist for it. Used to
    # expand successful runs, whose own logs are never downloaded.
    commit_tests: dict[str, set[str]] = {}
    for record in out_runs:
        for nodeid in record.get("tests", {}):
            commit_tests.setdefault(record["sha"], set()).add(nodeid)
    for record in out_runs:
        if record["conclusion"] == "success":
            known = commit_tests.get(record["sha"])
            record["reconstructed"] = True
            record["tests"] = {n: {"status": "passed"} for n in sorted(known or [])}

    return {
        "source": f"github.com/{repo}",
        "workflow": workflow,
        "extracted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "notes": [
            "Per-test outcomes of failed runs are parsed from their pytest output.",
            "Successful runs carry no log; their tests are recorded as passed using the "
            "test list observed for the same commit (marked reconstructed).",
            "Failed runs whose logs GitHub has expired are kept as log_expired gaps.",
            "No per-test durations: the suite does not run pytest with --durations.",
        ],
        "runs": out_runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path(".cache/gh-logs"))
    parser.add_argument("--limit", type=int, default=400)
    args = parser.parse_args(argv)

    cache = args.cache / args.repo.replace("/", "_")
    history = build_history(args.repo, args.workflow, cache, args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(history, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    runs = history["runs"]
    failed = [r for r in runs if r["conclusion"] == "failure"]
    expired = [r for r in failed if r.get("log_expired")]
    print(f"{len(runs)} runs ({len(failed)} failed, {len(expired)} with expired logs)"
          f" -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
