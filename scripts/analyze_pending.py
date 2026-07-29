"""Classify every failure that has no analysis yet.

    python scripts/analyze_pending.py              # all pending
    python scripts/analyze_pending.py --limit 10   # just the first 10
    python scripts/analyze_pending.py --dry-run    # list them, classify nothing

Talks to the database directly rather than over HTTP, which makes it the same
command on every shell — no curl-vs-Invoke-WebRequest, no quoting rules, no
server needed.

It is also the **restart-recovery path**. Analysis normally runs as a FastAPI
background task, and those die with the process: a restart mid-batch strands
however many were queued. `GET /api/analysis/queue` makes that visible; this
makes it fixable. Run it after any restart, or on a cron, and stranded work
gets picked up.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.analysis.agent import AgentUnavailableError, RootCauseAnalysisAgent
from backend.analysis.context_retriever import ContextRetriever
from backend.analysis.heuristics import HeuristicAnalyzer
from backend.config import get_settings
from backend.db.repository import TestResultRepository
from backend.db.session import init_engine, session_scope
from backend.logging_config import configure_logging


def run(limit: int | None, mode: str | None, dry_run: bool) -> int:
    settings = get_settings()
    overrides: dict[str, object] = {}
    if mode:
        overrides |= {"agent_enabled": True, "analysis_mode": mode}
    # This script prints a progress line per analysis, so the analyzer's own
    # INFO log would print the same event twice and bury the readable output.
    # Warnings and errors still come through.
    overrides["log_level"] = "WARNING"
    settings = settings.model_copy(update=overrides)

    configure_logging(settings)
    init_engine(settings)

    if not settings.agent_enabled:
        print("AGENT_ENABLED is false — nothing to do. Set it to true in .env.")
        return 1
    if settings.analysis_mode == "claude" and not settings.anthropic_api_key:
        print(
            "ANALYSIS_MODE=claude but ANTHROPIC_API_KEY is not set.\n"
            "Set the key, or use --mode heuristic to classify with rules instead."
        )
        return 1

    with session_scope() as session:
        pending = TestResultRepository(session).get_unanalyzed_failures(
            limit=limit or 10_000
        )
        if not pending:
            print("nothing pending — every failure already has an analysis")
            return 0

        print(f"{len(pending)} failure(s) pending, mode={settings.analysis_mode}\n")
        if dry_run:
            for row in pending:
                print(f"  {row.framework.value:<11} {row.test_name[:70]}")
            print("\n(dry run — nothing classified)")
            return 0

        retriever = ContextRetriever(session, settings)
        analyzer: RootCauseAnalysisAgent | HeuristicAnalyzer = (
            HeuristicAnalyzer(retriever, settings)
            if settings.analysis_mode == "heuristic"
            else RootCauseAnalysisAgent(retriever, settings)
        )

        done = failed = 0
        for index, row in enumerate(pending, start=1):
            try:
                analysis = analyzer.analyze(row.id, session)
            except AgentUnavailableError as exc:
                print(f"stopped: {exc}")
                return 1
            except Exception as exc:
                # One bad row must not abandon the rest of the batch — the whole
                # point of this script is to drain a backlog.
                print(f"  [{index}/{len(pending)}] ERROR {row.test_name[:50]}: {exc!r}")
                failed += 1
                continue

            verdict = analysis.root_cause.value if analysis.root_cause else "failed"
            confidence = (
                f"{analysis.confidence_score:.2f}" if analysis.confidence_score else "-"
            )
            flag = " [needs review]" if analysis.requires_human_review else ""
            print(
                f"  [{index}/{len(pending)}] {verdict:<20} {confidence:<5} "
                f"{row.test_name[:52]}{flag}"
            )
            done += 1

    print(f"\nclassified {done}, errored {failed}")
    print("read the verdicts at http://localhost:8000/api/analysis/failures?hours=999")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="max failures to process")
    parser.add_argument(
        "--mode",
        choices=["heuristic", "claude"],
        default=None,
        help="override ANALYSIS_MODE for this run",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be classified"
    )
    args = parser.parse_args()
    return run(args.limit, args.mode, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
