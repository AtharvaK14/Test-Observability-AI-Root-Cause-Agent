"""Score a classifier against the seeded ground truth.

    python scripts/seed_demo.py --reset
    python scripts/score_classifier.py                  # rule-based baseline
    python scripts/score_classifier.py --mode claude    # the LLM agent (costs money)

Prints per-scenario verdicts and an accuracy figure. Run both and the headline
number for the project falls out:

    "Deterministic rules classify N% of seeded failures correctly.
     The Claude agent classifies M%."

One is a claim; two is a result. A single accuracy number has no scale — nobody
reading it knows whether 70% is good, and the honest answer depends entirely on
what the trivial approach already achieves.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252, which raises UnicodeEncodeError on the
# em dashes in this script's own output. Forcing UTF-8 is better than stripping
# the punctuation, and errors="replace" means a console that still cannot render
# a character degrades to a placeholder instead of killing the run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.analysis.context_retriever import ContextRetriever
from backend.config import get_settings
from backend.db.repository import TestResultRepository
from backend.db.session import init_engine, session_scope
from backend.models.analysis import AgentClassification
from scripts.seed_demo import SCENARIOS


def score(mode: str, verbose: bool) -> int:
    settings = get_settings().model_copy(
        update={"agent_enabled": True, "analysis_mode": mode}
    )
    init_engine(settings)

    rows: list[tuple[str, str, AgentClassification | None, str]] = []

    with session_scope() as session:
        repo = TestResultRepository(session)
        retriever = ContextRetriever(session, settings)

        if mode == "heuristic":
            from backend.analysis.heuristics import HeuristicClassifier

            classifier = HeuristicClassifier(settings)

            def classify(test_result_id: str) -> tuple[AgentClassification | None, str]:
                context = retriever.get_failure_context(test_result_id)
                if context is None:
                    return None, "context unavailable"
                return classifier.classify(context), ""
        else:
            from backend.analysis.agent import RootCauseAnalysisAgent

            agent = RootCauseAnalysisAgent(retriever, settings)

            def classify(test_result_id: str) -> tuple[AgentClassification | None, str]:
                context = retriever.get_failure_context(test_result_id)
                if context is None:
                    return None, "context unavailable"
                result = agent._run(context.to_dict())
                return result.classification, result.error or ""

        for key, scenario in SCENARIOS.items():
            # The most recent failure for this test — seed_demo guarantees the
            # final run fails, so this is the planted one.
            failures = repo.get_recent_failures(scenario.test_name, limit=1)
            if not failures:
                rows.append((key, scenario.expected, None, "no seeded failure found"))
                continue
            verdict, error = classify(failures[0].id)
            rows.append((key, scenario.expected, verdict, error))

    return _report(mode, rows, verbose)


def _report(
    mode: str,
    rows: list[tuple[str, str, AgentClassification | None, str]],
    verbose: bool,
) -> int:
    print(f"\n{'=' * 78}\nClassifier: {mode}\n{'=' * 78}")
    print(f"{'scenario':<18} {'expected':<20} {'predicted':<20} {'conf':<6} ok")
    print("-" * 78)

    correct = scored = 0
    for key, expected, verdict, error in rows:
        if verdict is None:
            print(f"{key:<18} {expected:<20} {'(no verdict)':<20} {'-':<6} FAIL  {error}")
            scored += 1
            continue
        predicted = verdict.category.value
        hit = predicted == expected
        scored += 1
        correct += hit
        mark = "PASS" if hit else "FAIL"
        print(
            f"{key:<18} {expected:<20} {predicted:<20} "
            f"{verdict.confidence:<6.2f} {mark}"
        )
        if verbose:
            print(f"    reasoning: {verdict.reasoning}")
            for item in verdict.key_evidence:
                print(f"      - {item}")

    print("-" * 78)
    accuracy = correct / scored if scored else 0.0
    print(f"{correct}/{scored} correct — {accuracy:.0%} accuracy\n")

    print(
        "!! This score is NOT a portfolio-quotable accuracy figure.\n"
        "   The seeded scenarios and the rules were written by the same person, so\n"
        "   the rules match the plants by construction — the number mostly measures\n"
        "   whether the pipeline is wired up, not whether classification works.\n"
        "   A real figure needs real failures from real suites, adjudicated through\n"
        "   POST /api/analysis/analyses/{id}/feedback. Until then, quote nothing.\n"
    )

    # `unknown` is not a wrong answer in the same way a confident miss is. Both
    # are counted against accuracy above (correctly), but the split matters when
    # deciding whether a classifier is under-confident or actually wrong.
    abstained = sum(
        1 for _, exp, v, _ in rows if v and v.category.value == "unknown" and exp != "unknown"
    )
    if abstained:
        print(f"note: {abstained} of the misses were honest 'unknown' abstentions")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["heuristic", "claude"],
        default="heuristic",
        help="which classifier to score (claude makes real API calls and costs money)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print reasoning and evidence"
    )
    args = parser.parse_args()
    return score(args.mode, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
