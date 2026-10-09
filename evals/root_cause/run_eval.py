"""Run the root-cause eval.

Free, offline modes (no API key, no cost):

    python -m evals.root_cause.run_eval --mode heuristic   # rule-based baseline
    python -m evals.root_cause.run_eval --mode oracle      # must score ~100%: harness check
    python -m evals.root_cause.run_eval --mode null        # must score ~0%:   grader check
    python -m evals.root_cause.run_eval --mode replay      # re-grade recorded Claude runs

Paid mode (calls the Anthropic API; nothing is spent without --yes):

    python -m evals.root_cause.run_eval --mode claude --reps 3 --budget-usd 5          # prints plan
    python -m evals.root_cause.run_eval --mode claude --reps 3 --budget-usd 5 --yes    # runs

Output, per variant, in .claude/hillclimb/root_cause/<variant>/:
    results.jsonl          one row per (case, rep), written as each completes
    traces/<id>_rep<k>.json  full conversation for that row
    errors.jsonl           attempts that produced nothing scorable (never scored)
    summary.json           headline numbers, recomputed from results.jsonl

Claude runs also save cassettes to evals/root_cause/cassettes/<variant>/ so the
run can be replayed offline (``--mode replay``) in CI.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import sys
import threading
import time
import traceback
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = Path(__file__).resolve().parent
FLOW_DIR = ROOT / ".claude" / "hillclimb" / "root_cause"
CASSETTE_DIR = EVAL_DIR / "cassettes"

FREE_MODES = ("heuristic", "oracle", "null", "replay")

# First-party prices, $ per million tokens (input, output). Cache writes bill at
# 1.25x input, cache reads at 0.1x. Cost is derived from each row's *served*
# model, so a model swap can never carry a stale rate.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Files whose change invalidates an approval to spend money on this harness.
HARNESS_PATHS = [
    "evals/root_cause/run_eval.py",
    "evals/root_cause/worlds.py",
    "evals/root_cause/grading.py",
    "evals/root_cause/clients.py",
    "evals/root_cause/cases.json",
    "backend/analysis/agent.py",
    "backend/analysis/context_retriever.py",
]

STATE_TEMPLATE: dict[str, Any] = {
    "flow": "root_cause",
    "description": "Root-cause classification of failed tests (Claude agent vs rule baseline).",
    "metrics": [
        {"id": "correct", "label": "Correct", "kind": "binary"},
        {"id": "grounding", "label": "Grounded", "kind": "continuous"},
    ],
    "perf_fields": ["latency_s", "tool_calls", "iterations", "retries", "usage"],
    "harness_paths": HARNESS_PATHS,
    "prices": {m: {"in": p[0], "out": p[1]} for m, p in PRICES.items()},
}


# --- Cost ---------------------------------------------------------------------


def cost_usd(model: str | None, usage: dict[str, Any] | None) -> float:
    # Offline classifiers (rules, oracle, null, replay of nothing) make no API
    # calls and genuinely cost nothing; only rows with token usage are priced.
    if not model or not usage or not any(usage.values()):
        return 0.0
    price = next((p for m, p in sorted(PRICES.items(), key=lambda kv: -len(kv[0]))
                  if model.startswith(m)), None)
    if price is None:
        return float("nan")
    p_in, p_out = price[0] / 1e6, price[1] / 1e6
    return float(
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.1
    )


# --- Cases ----------------------------------------------------------------------


def load_cases(selectors: list[str] | None = None) -> list[dict[str, Any]]:
    with (EVAL_DIR / "cases.json").open(encoding="utf-8") as fh:
        cases: list[dict[str, Any]] = json.load(fh)["cases"]
    if selectors:
        cases = [
            c for c in cases
            if any(c["id"] == s or c["id"].startswith(s) or s in c["tags"] for s in selectors)
        ]
    return cases


# --- One (case, rep) -----------------------------------------------------------


def _usage_from_calls(calls: list[dict[str, Any]]) -> dict[str, int]:
    total = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for call in calls:
        usage = call["response"].get("usage") or {}
        for key in total:
            total[key] += int(usage.get(key) or 0)
    return total


def _classify_failure(error: str) -> tuple[str, str]:
    """(kind, failure_class). kind is 'graded', 'truncated' or 'error'."""
    if error.startswith(("rate limited", "could not reach", "Anthropic API error")):
        return "error", "api_error"
    if "ServedModelMismatch" in error:
        return "error", "served_model_mismatch"
    if "ReplayError" in error:
        return "error", "replay_error"
    if error.startswith("request declined by safety"):
        return "graded", "refusal"
    if "max_tokens" in error:
        return "truncated", "max_tokens"
    if error.startswith("agent did not reach a verdict"):
        return "graded", "no_verdict"
    return "error", "harness_error"


def run_one(task: dict[str, Any]) -> dict[str, Any]:
    """Build the case's world, run the classifier, grade. Never raises."""
    from backend.analysis.agent import RootCauseAnalysisAgent
    from backend.analysis.heuristics import HeuristicAnalyzer
    from backend.config import Settings
    from backend.models.enums import AnalysisStatus
    from evals.root_cause import grading
    from evals.root_cause.clients import RecordingClient, ReplayClient, transcript_from_calls
    from evals.root_cause.worlds import open_world

    case, rep, mode = task["case"], task["rep"], task["mode"]
    started = time.perf_counter()
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        environment="ci",
        agent_enabled=True,
        auto_analyze_on_ingest=False,
        analysis_mode="heuristic" if mode == "heuristic" else "claude",
        anthropic_model=task["model"],
        anthropic_effort=task["effort"],
        anthropic_timeout_seconds=task["call_timeout_s"],
    )
    base = {"prompt_id": case["id"], "rep": rep, "tags": case["tags"]}

    client: Any = None
    try:
        with open_world(case, settings) as world:
            context = world.retriever.get_failure_context(world.test_result_id)
            assert context is not None
            agent = RootCauseAnalysisAgent(world.retriever, settings, client=None)
            prompt = agent._build_initial_message(context.to_dict())

            calls: list[dict[str, Any]] = []
            verdict: dict[str, Any] | None = None
            failure: tuple[str, str] | None = None
            iterations = 0

            if mode in ("oracle", "null"):
                category = case["expected"][0] if mode == "oracle" else "unknown"
                verdict = {
                    "category": category,
                    "confidence": 1.0 if mode == "oracle" else 0.0,
                    "reasoning": f"{mode} classifier",
                    "key_evidence": [context.current_failure.get("error_message") or ""]
                    if mode == "oracle" else [],
                    "suggestions": [],
                    "requires_human_review": False,
                }
                model = mode
            else:
                if mode == "heuristic":
                    analyzer: Any = HeuristicAnalyzer(world.retriever, settings)
                else:
                    if mode == "claude":
                        client = RecordingClient.from_environment(task["call_timeout_s"])
                    else:
                        cassette = json.loads(Path(task["cassette"]).read_text(encoding="utf-8"))
                        client = ReplayClient(cassette["calls"])
                    analyzer = RootCauseAnalysisAgent(world.retriever, settings, client=client)

                row = analyzer.analyze(world.test_result_id, world.session)
                iterations = row.iterations or 0
                if client is not None:
                    calls = client.calls
                if row.status == AnalysisStatus.COMPLETED:
                    verdict = {
                        "category": row.root_cause.value if row.root_cause else None,
                        "confidence": row.confidence_score,
                        "reasoning": row.reasoning,
                        "key_evidence": list(row.key_evidence or []),
                        "suggestions": list(row.suggestions or []),
                        "requires_human_review": row.requires_human_review,
                    }
                else:
                    failure = _classify_failure(row.error_message or "")
                    if client is not None and client.model_mismatch:
                        failure = ("error", "served_model_mismatch")
                model = (calls[-1]["response"].get("model") if calls else None) or (
                    "rules" if mode == "heuristic" else task["model"])

            usage = _usage_from_calls(calls)
            wall_s = round(time.perf_counter() - started, 3)
            if failure and failure[0] == "error":
                return {**base, "kind": "error", "failure_class": failure[1],
                        "detail": (row.error_message or "")[:2000], "model": model,
                        "usage": usage, "retries": getattr(client, "retries", 0),
                        "calls": calls}

            if calls:
                trace = transcript_from_calls(calls)
                corpus = "\n".join(t["content"] for t in trace
                                   if t["role"] in ("user", "tool_result"))
            else:
                trace = [{"role": "user", "content": prompt}]
                if verdict:
                    trace.append({"role": "assistant", "content": json.dumps(verdict, indent=2)})
                corpus = prompt

            predicted = verdict["category"] if verdict else None
            grade, meta = grading.grade_case(
                case, predicted, (verdict or {}).get("key_evidence", []), corpus)
            meta.update({
                "confidence": (verdict or {}).get("confidence"),
                "requires_human_review": (verdict or {}).get("requires_human_review"),
                "reasoning": (verdict or {}).get("reasoning"),
                "key_evidence": (verdict or {}).get("key_evidence"),
                "suggestions": (verdict or {}).get("suggestions"),
                "outcome": failure[1] if failure else "verdict",
                "labeled_by": case.get("labeled_by"),
                "world_notes": world.notes,
                "frozen_at": world.frozen_at.isoformat(),
                "wall_s": wall_s,
            })
            if mode == "replay":
                meta["request_drift"] = client.request_drift
            return {
                **base,
                "kind": "row",
                "prompt": prompt,
                "status": "truncated" if failure and failure[0] == "truncated" else "ok",
                "stop_reason": (calls[-1]["response"].get("stop_reason") if calls else "end_turn"),
                "grade": grade,
                "model": model,
                "usage": usage,
                "latency_s": round(sum(c["latency_s"] for c in calls), 3) if calls else 0.0,
                "tool_calls": sum(1 for t in trace if t["role"] == "tool_call"),
                "iterations": iterations,
                "retries": getattr(client, "retries", 0),
                "meta": meta,
                "trace": trace,
                "calls": calls,
            }
    except Exception as exc:
        return {**base, "kind": "error", "failure_class": "harness_error",
                "detail": "".join(traceback.format_exception(exc))[-4000:],
                "usage": _usage_from_calls(getattr(client, "calls", []) or []),
                "model": task["model"], "calls": getattr(client, "calls", []) or []}


def run_with_ceiling(task: dict[str, Any]) -> dict[str, Any]:
    """Hard wall-clock ceiling. A stuck call cannot be aborted from here, but
    the slot is reclaimed and the attempt is recorded as a timeout, not a zero."""
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.update(result=run_one(task)), daemon=True)
    thread.start()
    thread.join(task["timeout_s"])
    if thread.is_alive():
        return {"prompt_id": task["case"]["id"], "rep": task["rep"], "tags": task["case"]["tags"],
                "kind": "error", "failure_class": "timeout",
                "detail": f"exceeded {task['timeout_s']}s wall-clock ceiling",
                "model": task["model"], "usage": {}, "calls": []}
    result: dict[str, Any] = box["result"]
    return result


# --- Run management ----------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def harness_sha() -> str:
    digest = hashlib.sha256()
    for rel in HARNESS_PATHS:
        digest.update(rel.encode())
        digest.update((ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def _load_state() -> dict[str, Any]:
    path = FLOW_DIR / "_state.json"
    if not path.exists():
        FLOW_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(STATE_TEMPLATE, indent=2) + "\n", encoding="utf-8")
    state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return state


def _save_state(state: dict[str, Any]) -> None:
    (FLOW_DIR / "_state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _estimate(rows: Iterable[dict[str, Any]]) -> str | None:
    costs = sorted(cost_usd(r.get("model"), r.get("usage")) for r in rows
                   if r.get("usage", {}).get("input_tokens"))
    if not costs:
        return None
    median = costs[len(costs) // 2]
    return f"measured ${costs[0]:.4f} / ${median:.4f} / ${costs[-1]:.4f} per case (min/median/max)"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Root-cause classifier eval.")
    parser.add_argument("--mode", choices=(*FREE_MODES, "claude"), required=True)
    parser.add_argument("--variant", help="output dir name (default: baseline for claude, "
                        "diag-<mode> otherwise)")
    parser.add_argument("--replay-from", default="baseline",
                        help="replay: the recorded variant whose cassettes to play back")
    parser.add_argument("--cases", nargs="*", help="case ids, id prefixes, or tags")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--reps", type=int)
    parser.add_argument("--model", help="override the app's ANTHROPIC_MODEL")
    parser.add_argument("--effort", help="override the app's ANTHROPIC_EFFORT")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout-s", type=int, default=900, help="per-case wall-clock ceiling")
    parser.add_argument("--budget-usd", type=float, help="hard spend cap (required for claude)")
    parser.add_argument("--yes", action="store_true", help="consent to the paid run as planned")
    parser.add_argument("--approve-harness", action="store_true",
                        help="record the current harness as approved for paid runs")
    parser.add_argument("--fresh", action="store_true", help="discard previous results first")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any attempt ended in errors.jsonl (CI)")
    args = parser.parse_args(argv)

    from backend.config import Settings

    app_defaults = Settings(_env_file=None)
    model = args.model or app_defaults.anthropic_model
    effort = args.effort or app_defaults.anthropic_effort
    # Replay writes to its own folder: it must never overwrite the paid run
    # whose cassettes it is reading.
    variant = args.variant or ("baseline" if args.mode == "claude" else f"diag-{args.mode}")
    if args.mode == "replay" and variant == args.replay_from:
        print("--variant must differ from --replay-from: replay would overwrite the recorded run.")
        return 2
    reps = args.reps or (3 if args.mode == "claude" else 1)
    out = FLOW_DIR / variant
    cases = load_cases(args.cases)
    if args.max_cases:
        cases = cases[: args.max_cases]

    state = _load_state()
    if args.mode == "claude":
        sha = harness_sha()
        if args.approve_harness:
            state["harness_sha"] = sha
            _save_state(state)
            print(f"harness approved: {sha[:12]}")
        elif state.get("harness_sha") != sha:
            print("Harness changed since it was last approved for paid runs (or never was).\n"
                  "Review the diff, then re-run with --approve-harness. Nothing was spent.")
            return 2
        if args.budget_usd is None:
            print("--budget-usd is required for --mode claude. Nothing was spent.")
            return 2

    if args.fresh and out.exists():
        for name in ("results.jsonl", "errors.jsonl", "summary.json"):
            (out / name).unlink(missing_ok=True)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    results_path, errors_path = out / "results.jsonl", out / "errors.jsonl"
    done = {(r["prompt_id"], r["rep"]) for r in _read_jsonl(results_path)}

    tasks = []
    for case in cases:
        for rep in range(reps):
            if (case["id"], rep) in done:
                continue
            task = {"case": case, "rep": rep, "mode": args.mode, "model": model,
                    "effort": effort, "timeout_s": args.timeout_s,
                    "call_timeout_s": min(300, args.timeout_s)}
            if args.mode == "replay":
                cassette = CASSETTE_DIR / args.replay_from / f"{case['id']}_rep{rep}.json"
                if not cassette.exists():
                    continue
                task["cassette"] = str(cassette)
            tasks.append(task)

    if args.mode == "claude":
        estimate = _estimate(_read_jsonl(results_path))
        print(f"Plan: {len(tasks)} calls to run ({len(cases)} cases x {reps} reps, "
              f"{len(done)} already done) on {model}, effort {effort}.")
        print(f"Hard cap: ${args.budget_usd:.2f}. "
              + (estimate or "No measured cost yet: run a small pilot first "
                 "(e.g. --max-cases 3 --reps 1) and the plan will show it."))
        if not args.yes:
            print("Re-run with --yes to proceed. Nothing was spent.")
            return 3

    if not tasks:
        print("nothing to run")
    spent = sum(cost_usd(r.get("model"), r.get("usage")) for r in _read_jsonl(results_path))
    spent += sum(cost_usd(r.get("model"), r.get("usage")) for r in _read_jsonl(errors_path))

    def record(result: dict[str, Any]) -> None:
        nonlocal spent
        calls = result.pop("calls", [])
        trace = result.pop("trace", None)
        kind = result.pop("kind")
        spent += cost_usd(result.get("model"), result.get("usage"))
        if args.mode == "claude" and calls:
            path = CASSETTE_DIR / variant / f"{result['prompt_id']}_rep{result['rep']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"model": model, "effort": effort, "calls": calls},
                                       indent=1) + "\n", encoding="utf-8")
        if kind == "error":
            _append_jsonl(errors_path, result)
            print(f"  ERROR {result['prompt_id']} rep{result['rep']}: {result['failure_class']}")
            return
        (out / "traces" / f"{result['prompt_id']}_rep{result['rep']}.json").write_text(
            json.dumps(trace, indent=1), encoding="utf-8")
        _append_jsonl(results_path, result)
        meta = result["meta"]
        mark = "PASS" if result["grade"]["correct"] else "FAIL"
        print(f"  {mark} {result['prompt_id']:<44} rep{result['rep']} "
              f"expected={'|'.join(meta['expected']):<34} predicted={meta['predicted']}")

    over_budget = False
    if args.workers <= 1 or args.mode in FREE_MODES:
        for task in tasks:
            if args.budget_usd is not None and spent >= args.budget_usd:
                over_budget = True
                break
            record(run_with_ceiling(task))
    else:
        pending = list(tasks)
        with cf.ProcessPoolExecutor(max_workers=args.workers, max_tasks_per_child=1) as pool:
            in_flight: set[cf.Future[dict[str, Any]]] = set()
            while pending or in_flight:
                while pending and len(in_flight) < args.workers:
                    if args.budget_usd is not None and spent >= args.budget_usd:
                        over_budget = True
                        pending.clear()
                        break
                    in_flight.add(pool.submit(run_with_ceiling, pending.pop(0)))
                if not in_flight:
                    break
                finished, in_flight = cf.wait(in_flight, return_when=cf.FIRST_COMPLETED)
                for fut in finished:
                    record(fut.result())

    from evals.root_cause.grading import summarise

    rows = _read_jsonl(results_path)
    errors = _read_jsonl(errors_path)
    summary = summarise(rows)
    summary["errors"] = len(errors)
    summary["error_classes"] = {}
    for err in errors:
        summary["error_classes"][err["failure_class"]] = (
            summary["error_classes"].get(err["failure_class"], 0) + 1)
    summary["cost_usd"] = round(spent, 4) if not math.isnan(spent) else None
    summary["mode"], summary["model"], summary["effort"] = args.mode, model, effort
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"\n{variant}: {summary['scored']} scored rows, {len(errors)} errors, "
          f"cost ${spent:.4f}" + ("  [STOPPED: budget reached]" if over_budget else ""))
    for source, block in summary["by_source"].items():
        print(f"  {source:<10} accuracy {block['accuracy']} (95% CI {block['ci95']}) "
              f"n={block['n']}  grounding={block['grounding_mean']}")
    if "injection" in summary:
        inj = summary["injection"]
        print(f"  injection  avoided planted category {inj['resisted']}/{inj['n']}, "
              f"avoided it and answered correctly {inj['resisted_and_correct']}/{inj['n']}")
    if summary.get("noise_floor_pp") is not None:
        print(f"  noise floor ~±{summary['noise_floor_pp']} pp")
    print(f"  -> {out / 'summary.json'}")
    if over_budget or (args.strict and errors):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
