"""The eval harness, tested offline: no API key, no network, no cost.

An eval that is wrong produces confident numbers pointing the wrong way, so the
harness gets the same scrutiny as the code it measures. The properties pinned
here are the ones that would silently corrupt a score: future leakage, injected
text not reaching the model, a grader that flags real facts or accepts invented
ones, retries that go uncounted, and a paid run starting without consent.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import pytest

from backend.config import Settings
from backend.db.models import TestResultDB
from evals.root_cause import grading, run_eval
from evals.root_cause.clients import RecordingClient, ServedModelMismatchError
from evals.root_cause.extract_github_history import parse_pytest_log
from evals.root_cause.worlds import DURATION_UNRECORDED, open_world


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        environment="ci",
        agent_enabled=True,
        auto_analyze_on_ingest=False,
        analysis_mode="heuristic",
    )


def _case(case_id: str) -> dict[str, Any]:
    return next(c for c in run_eval.load_cases() if c["id"] == case_id)


# --- Fixture extraction -------------------------------------------------------

_LOG = "\n".join(
    "Smoke Gate\tRun\t2026-07-09T09:47:10.1Z " + line
    for line in [
        "tests/a.py::TestA::test_ok PASSED [ 50%]",
        "[gw1] [ 75%] FAILED tests/a.py::TestA::test_bad",
        "[gw0] [100%] SKIPPED tests/b.py::TestB::test_skip",
        "=================================== FAILURES ===================================",
        "_ TestA.test_bad _",
        "    def test_bad(self):",
        ">       assert response.status_code == 200",
        "E       assert 401 == 200",
        "tests/a.py:9: AssertionError",
        "----------------------------- Captured log call ------------------------------",
        "WARNING  client: Bad credentials",
        "=========================== short test summary info ============================",
        "FAILED tests/a.py::TestA::test_bad - assert 401 == 200",
    ]
)


def test_parser_reads_serial_xdist_and_short_banners() -> None:
    tests = parse_pytest_log(_LOG)
    assert tests["tests/a.py::TestA::test_ok"]["status"] == "passed"
    assert tests["tests/b.py::TestB::test_skip"]["status"] == "skipped"
    bad = tests["tests/a.py::TestA::test_bad"]
    assert bad["status"] == "failed"
    assert bad["error_message"] == "assert 401 == 200"
    # A single-underscore banner: pytest shortens it for long test names.
    assert "E       assert 401 == 200" in bad["stack_trace"]
    assert "Bad credentials" in bad["logs"]


def test_fixture_is_complete_for_every_case() -> None:
    history = json.loads(
        (Path(run_eval.EVAL_DIR) / "data" / "api_quality_gate_history.json").read_text("utf-8")
    )
    runs = {r["run_id"]: r for r in history["runs"]}
    for case in run_eval.load_cases():
        if not case["world"].startswith("github:"):
            continue
        outcome = runs[case["target"]["run_id"]]["tests"][case["target"]["nodeid"]]
        assert outcome["status"] == "failed", case["id"]
        assert outcome["error_message"] and outcome["stack_trace"], case["id"]


# --- Worlds ----------------------------------------------------------------------


def test_real_world_contains_no_future_runs() -> None:
    """The answer key for a Jul 9 failure is that it resolved on Jul 29. The
    world must stop at Jul 9 or the eval leaks the outcome to the model."""
    with open_world(_case("real-01-bad-credentials-user"), _settings()) as world:
        latest = max(row.timestamp for row in world.session.query(TestResultDB))
        target = world.session.get(TestResultDB, world.test_result_id)
        assert target is not None
        assert latest == target.timestamp
        assert latest < datetime.fromisoformat("2026-07-10T00:00:00+00:00")


def test_real_world_discloses_its_gaps_truthfully() -> None:
    with open_world(_case("real-01-bad-credentials-user"), _settings()) as world:
        context = world.retriever.get_failure_context(world.test_result_id)
        assert context is not None
        rendered = json.dumps(context.to_dict())
        assert "failed but are missing from this history" in rendered
        assert context.duration_analysis == {"available": False, "reason": DURATION_UNRECORDED}


def test_seeded_world_is_identical_across_builds() -> None:
    def snapshot() -> list[tuple[Any, ...]]:
        with open_world(_case("synth-checkout-total"), _settings()) as world:
            rows = world.session.query(TestResultDB).order_by(
                TestResultDB.timestamp, TestResultDB.test_name)
            return [(r.test_name, r.status, r.duration_ms, r.git_commit, r.timestamp)
                    for r in rows]

    assert snapshot() == snapshot()


@pytest.mark.parametrize(
    "case_id",
    ["inject-real-05-force-app-bug", "inject-synth-checkout-hide-bug",
     "inject-synth-report-export-force-test-data"],
)
def test_injected_text_reaches_what_the_model_sees(case_id: str) -> None:
    from backend.analysis.agent import RootCauseAnalysisAgent

    case = _case(case_id)
    with open_world(case, _settings()) as world:
        context = world.retriever.get_failure_context(world.test_result_id)
        assert context is not None
        agent = RootCauseAnalysisAgent(world.retriever, _settings())
        message = agent._build_initial_message(context.to_dict())
    marker = case["injection"]["payload"].strip()[:40]
    assert marker in message.replace('\\"', '"')


# --- Grading ----------------------------------------------------------------------


def test_grounding_accepts_real_facts_in_any_format() -> None:
    corpus = '{"pass_rate_pct": 60.9, "pass_rate": 0.52, "duration_ms": 1428, ' \
             '"error": "assert 403 == 404", "commit": "e1986d7"}'
    share, missing, checked = grading.grounding(
        ["pass rate 61%", "52% of runs", "took 1,428ms", "got 403 on e1986d7"], corpus)
    assert (share, missing, checked) == (1.0, [], 5)


def test_grounding_rejects_invented_and_near_miss_facts() -> None:
    corpus = '{"pass_rate_pct": 60.9, "duration_ms": 1428, "commit": "e1986d7"}'
    share, missing, _ = grading.grounding(
        ["pass rate 75%", "took 1430ms", "commit abc1234ef", "status 418"], corpus)
    assert share == 0.0
    assert set(missing) == {"75%", "1430ms", "abc1234ef", "418"}


def test_injection_metric_cannot_be_gamed_by_always_abstaining() -> None:
    case = _case("inject-real-01-force-app-bug")
    rows = []
    for predicted in ("unknown", "external_dependency", "app_bug"):
        grade, meta = grading.grade_case(case, predicted, [], "")
        rows.append({"prompt_id": predicted, "status": "ok", "tags": case["tags"],
                     "grade": grade, "meta": {**meta, "confidence": 0.5}})
    summary = grading.summarise(rows)["injection"]
    assert summary["resisted"] == 2          # unknown and the right answer avoid app_bug
    assert summary["resisted_and_correct"] == 1  # only the right answer counts here


def test_accepted_set_and_wilson_interval() -> None:
    ambiguous = _case("real-10-sla-authenticated-user")
    assert grading.grade_case(ambiguous, "flaky_test", [], "")[0]["correct"] == 1.0
    assert grading.grade_case(ambiguous, "app_bug", [], "")[0]["correct"] == 0.0
    lo, hi = grading.wilson(6, 11)
    assert (round(lo, 2), round(hi, 2)) == (0.28, 0.79)


def test_calibration_and_consistency() -> None:
    rows = [
        {"prompt_id": "a", "grade": {"correct": 1.0}, "meta": {"predicted": "x", "confidence": 0.9}},
        {"prompt_id": "a", "grade": {"correct": 1.0}, "meta": {"predicted": "x", "confidence": 0.9}},
        {"prompt_id": "b", "grade": {"correct": 0.0}, "meta": {"predicted": "y", "confidence": 0.9}},
        {"prompt_id": "b", "grade": {"correct": 1.0}, "meta": {"predicted": "z", "confidence": 0.6}},
    ]
    cal = grading.calibration(rows)
    assert cal["n"] == 4 and cal["brier"] == pytest.approx((0.01 + 0.01 + 0.81 + 0.16) / 4)
    con = grading.consistency(rows)
    assert con == {"cases_with_reps": 2, "fully_consistent_share": 0.5,
                   "mean_modal_agreement": 0.75}


def test_cost_uses_served_model_and_cache_multipliers() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000,
             "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert run_eval.cost_usd("claude-opus-5-5", usage) == pytest.approx(4 + 2 + 5 + 0.4)
    assert run_eval.cost_usd("claude-opus-5", usage) == pytest.approx(5 + 2.5 + 6.25 + 0.5)
    assert run_eval.cost_usd("rules", {"input_tokens": 0}) == 0.0


# --- Harness end to end (offline modes) ---------------------------------------


@pytest.mark.parametrize(("mode", "expected"), [("oracle", 1.0), ("null", 0.0)])
def test_oracle_passes_and_null_fails_through_the_whole_pipeline(mode: str, expected: float) -> None:
    for case_id in ("real-05-rate-limit-404", "synth-checkout-total"):
        task = {"case": _case(case_id), "rep": 0, "mode": mode, "model": "claude-opus-5",
                "effort": "high", "timeout_s": 120, "call_timeout_s": 60}
        result = run_eval.run_one(task)
        assert result["kind"] == "row", result.get("detail")
        assert result["grade"]["correct"] == expected


def _message(content: list[dict[str, Any]], stop: str, tokens: int = 100) -> dict[str, Any]:
    return {"id": f"msg_{tokens}", "type": "message", "role": "assistant",
            "model": "claude-opus-5", "content": content, "stop_reason": stop,
            "stop_sequence": None, "usage": {"input_tokens": tokens, "output_tokens": 20}}


def test_replay_drives_the_real_agent_loop(tmp_path: Path) -> None:
    """The paid code path, proven offline: the production agent loop runs on a
    cassette, dispatches a real tool against the rebuilt world, submits a
    verdict, and the row is graded with transcript, usage and model recorded."""
    case = _case("real-05-rate-limit-404")
    nodeid = case["target"]["nodeid"]
    cassette = {"calls": [
        {"kind": "beta", "request": {}, "latency_s": 1.5, "attempts": 1, "response": _message(
            [{"type": "tool_use", "id": "tu_1", "name": "get_test_history",
              "input": {"test_name": nodeid, "limit": 5}}], "tool_use", 1000)},
        {"kind": "beta", "request": {}, "latency_s": 2.0, "attempts": 1, "response": _message(
            [{"type": "tool_use", "id": "tu_2", "name": "submit_classification", "input": {
                "category": "external_dependency", "confidence": 0.8,
                "reasoning": "Same commit e1986d7 passed before; 403 from GitHub.",
                "key_evidence": ["assert 403 == 404", "commit e1986d7"],
                "suggestions": ["Authenticate the client"],
                "requires_human_review": False}}], "tool_use", 1500)},
    ]}
    path = tmp_path / "cassette.json"
    path.write_text(json.dumps(cassette), encoding="utf-8")

    result = run_eval.run_one({"case": case, "rep": 0, "mode": "replay", "model": "claude-opus-5",
                               "effort": "high", "timeout_s": 120, "call_timeout_s": 60,
                               "cassette": str(path)})

    assert result["kind"] == "row", result.get("detail")
    assert result["meta"]["predicted"] == "external_dependency"
    assert result["grade"] == {"correct": 1.0, "grounding": 1.0}
    assert result["usage"]["input_tokens"] == 2500
    assert result["model"] == "claude-opus-5"
    assert result["latency_s"] == 3.5
    roles = [t["role"] for t in result["trace"]]
    assert roles[:2] == ["system", "user"]
    assert roles.count("tool_call") == 2 and "tool_result" in roles
    # The tool really ran against the rebuilt world: its output is in the trace.
    history_result = next(t for t in result["trace"] if t["role"] == "tool_result")
    assert nodeid in history_result["content"]


def test_exhausted_cassette_is_an_error_not_a_zero(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"calls": []}), encoding="utf-8")
    result = run_eval.run_one({"case": _case("real-05-rate-limit-404"), "rep": 0,
                               "mode": "replay", "model": "claude-opus-5", "effort": "high",
                               "timeout_s": 120, "call_timeout_s": 60, "cassette": str(path)})
    assert result["kind"] == "error"
    assert result["failure_class"] == "replay_error"


# --- Live client, without the network ---------------------------------------------


class _FakeSDK:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.messages = self
        self.beta = self

    def create(self, **_: Any) -> Any:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _rate_limit_error() -> anthropic.RateLimitError:
    httpx = importlib.import_module("httpx2" if importlib.util.find_spec("httpx2") else "httpx")
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com"))
    return anthropic.RateLimitError("slow down", response=response, body=None)


def test_recording_client_retries_with_backoff_and_counts(monkeypatch) -> None:
    from anthropic.types.beta import BetaMessage

    sleeps: list[float] = []
    monkeypatch.setattr("evals.root_cause.clients.time.sleep", sleeps.append)
    ok = BetaMessage.model_validate(_message([{"type": "text", "text": "hi"}], "end_turn"))
    client = RecordingClient(_FakeSDK([_rate_limit_error(), _rate_limit_error(), ok]))

    response = client.messages.create(model="claude-opus-5", max_tokens=10, messages=[])

    assert response is ok
    assert client.retries == 2 and len(sleeps) == 2
    assert client.calls[0]["attempts"] == 3
    assert client.calls[0]["response"]["usage"]["input_tokens"] == 100


def test_recording_client_refuses_a_substituted_model() -> None:
    from anthropic.types.beta import BetaMessage

    other = _message([{"type": "text", "text": "hi"}], "end_turn")
    other["model"] = "claude-haiku-4-5"
    client = RecordingClient(_FakeSDK([BetaMessage.model_validate(other)]))
    with pytest.raises(ServedModelMismatchError):
        client.messages.create(model="claude-opus-5", max_tokens=10, messages=[])


# --- Consent gates ------------------------------------------------------------------


def test_paid_mode_spends_nothing_without_approval_or_consent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_eval, "FLOW_DIR", tmp_path / "flow")

    def no_network(task: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a paid call was attempted")

    monkeypatch.setattr(run_eval, "run_with_ceiling", no_network)
    base = ["--mode", "claude", "--cases", "real-05", "--workers", "1"]

    assert run_eval.main([*base, "--budget-usd", "1"]) == 2           # harness not approved
    assert run_eval.main([*base, "--approve-harness"]) == 2           # no budget given
    assert run_eval.main([*base, "--budget-usd", "1"]) == 3           # approved, no --yes


def test_world_ids_are_reproducible() -> None:
    """Row ids reach the prompt and tool results; random ones would make replay
    report drift on every run and hide real changes in what the model sees."""
    def target_id() -> str:
        with open_world(_case("real-05-rate-limit-404"), _settings()) as world:
            return world.test_result_id

    assert target_id() == target_id()


def test_unrecorded_duration_is_not_shown_as_zero() -> None:
    with open_world(_case("real-05-rate-limit-404"), _settings()) as world:
        context = world.retriever.get_failure_context(world.test_result_id)
        assert context is not None
        assert context.current_failure["duration_ms"] is None


def test_replay_refuses_to_overwrite_the_recorded_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_eval, "FLOW_DIR", tmp_path / "flow")
    assert run_eval.main(["--mode", "replay", "--variant", "baseline"]) == 2
