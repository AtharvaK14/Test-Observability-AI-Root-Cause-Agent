"""The agent loop and context retrieval.

Every test here runs against a stubbed client — no API key, no network, no cost,
no non-determinism. The loop's control flow is exactly the part that must be
verified deterministically, because in production it only runs when something is
already broken.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from sqlalchemy.orm import Session

from backend.analysis.agent import (
    PROMPT_VERSION,
    AgentUnavailableError,
    RootCauseAnalysisAgent,
)
from backend.analysis.context_retriever import ContextRetriever
from backend.config import Settings
from backend.models.enums import AnalysisStatus, RootCauseCategory, TestFramework, TestStatus
from backend.tests.conftest import (
    VALID_VERDICT,
    StubAnthropicClient,
    api_response,
    content_block,
)


@pytest.fixture
def agent_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"agent_enabled": True})


def build_agent(session: Session, settings: Settings, script: list) -> tuple:
    stub = StubAnthropicClient(script)
    agent = RootCauseAnalysisAgent(ContextRetriever(session, settings), settings, client=stub)
    return agent, stub


def tool_call(name: str, payload: dict, call_id: str = "t1"):
    return api_response(
        [content_block(type="tool_use", id=call_id, name=name, input=payload)]
    )


class TestContextRetriever:
    def test_missing_id_returns_none_not_an_empty_bundle(
        self, session: Session, settings: Settings
    ) -> None:
        """An empty dict flows onward and produces an analysis of nothing."""
        assert ContextRetriever(session, settings).get_failure_context("nope") is None

    def test_history_drives_the_regression_signal(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        failure = seed_history(green_runs=20)
        context = ContextRetriever(session, settings).get_failure_context(failure.id)
        assert context is not None

        history = context.historical_pattern
        assert history["available"] is True
        assert history["pass_rate_pct"] == 100.0, "the failure itself is excluded"
        assert history["last_known_pass"]["git_commit"] == "aaa1111"

    def test_same_commit_pass_then_fail_rules_out_a_regression(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """If the code did not change between a pass and a fail, it is
        non-determinism, environment, or data — never a code regression."""
        failure = seed_history(green_runs=3)
        failure.git_commit = "aaa1111"
        session.flush()

        context = ContextRetriever(session, settings).get_failure_context(failure.id)
        assert context is not None
        assert "same commit" in context.git_context["interpretation"]

    def test_cross_framework_failures_are_flagged(
        self, session: Session, settings: Settings, seed_history, make_result
    ) -> None:
        """Independently written test code failing together is the strongest
        available evidence of an application or environment fault."""
        from backend.db.repository import TestResultRepository

        failure = seed_history(green_runs=2)
        TestResultRepository(session).add(
            make_result(
                test_name="api test",
                framework=TestFramework.PYTEST,
                status=TestStatus.FAILED,
                ci_run_id=failure.ci_run_id,
                failure_signature=failure.failure_signature,
            )
        )
        session.flush()

        context = ContextRetriever(session, settings).get_failure_context(failure.id)
        assert context is not None
        assert context.ci_run_correlation["cross_framework"] is True
        assert "frameworks" in context.ci_run_correlation["interpretation"]

    def test_duration_ratio_separates_waiting_from_dying(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """Same error message, opposite causes: burning a timeout means waiting;
        dying fast means the thing genuinely was not there."""
        failure = seed_history(green_runs=5, duration_ms=30000)
        context = ContextRetriever(session, settings).get_failure_context(failure.id)
        assert context is not None
        assert context.duration_analysis["ratio_to_baseline"] == pytest.approx(30.0)
        assert "waiting" in context.duration_analysis["interpretation"]

    def test_absent_metrics_are_reported_as_unmeasured(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """The reference implementation returns hard-coded metrics here. Inventing
        cpu_percent=65.2 invites the model to rule out resource contention on the
        strength of a number nobody measured."""
        failure = seed_history(green_runs=1)
        context = ContextRetriever(session, settings).get_failure_context(failure.id)
        assert context is not None
        assert context.system_metrics["available"] is False
        assert "unmeasured" in context.system_metrics["note"]

    def test_git_context_is_never_fabricated(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        failure = seed_history(green_runs=1)
        context = ContextRetriever(session, settings).get_failure_context(failure.id)
        assert context is not None
        assert context.git_context["repo_inspected"] is False
        assert "changed_files" not in context.git_context

    def test_logs_are_excerpted_by_relevance_not_position(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """`logs[:1000]` keeps the framework banner and drops the stack trace at
        the end — the exact opposite of what is wanted."""
        noise = "\n".join(f"[info] step {i}" for i in range(500))
        failure = seed_history(
            green_runs=1,
            logs=f"{noise}\n[error] POST /api/orders 500\nECONNREFUSED 10.0.0.4:5432",
        )
        context = ContextRetriever(session, settings).get_failure_context(failure.id)
        assert context is not None
        excerpt = context.current_failure["logs_excerpt"]
        assert "ECONNREFUSED" in excerpt
        assert "step 250" not in excerpt

    def test_log_search_tool_returns_regex_errors_as_data(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """An invalid regex is a recoverable mistake the model can fix next turn;
        raising would abort an otherwise fine analysis."""
        failure = seed_history(green_runs=1, logs="some logs")
        result = ContextRetriever(session, settings).tool_search_logs(failure.id, "[unclosed")
        assert "invalid regex" in result["error"]


class TestAgentLoop:
    def test_submits_a_verdict_and_persists_it(
        self, session: Session, agent_settings: Settings, seed_history, verdict_response
    ) -> None:
        failure = seed_history()
        agent, _ = build_agent(session, agent_settings, [verdict_response()])
        analysis = agent.analyze(failure.id, session)

        assert analysis.status == AnalysisStatus.COMPLETED
        assert analysis.root_cause == RootCauseCategory.APP_BUG
        assert analysis.confidence_score == pytest.approx(0.88)
        assert analysis.key_evidence == VALID_VERDICT["key_evidence"]
        assert analysis.prompt_version == PROMPT_VERSION
        assert analysis.input_tokens > 0 and analysis.latency_ms >= 0

    def test_uses_a_retrieval_tool_before_deciding(
        self, session: Session, agent_settings: Settings, seed_history, verdict_response
    ) -> None:
        failure = seed_history(logs="[error] POST /api/orders 500")
        agent, stub = build_agent(
            session,
            agent_settings,
            [
                tool_call("search_logs", {"test_result_id": failure.id, "pattern": "50[0-9]"}),
                verdict_response(),
            ],
        )
        analysis = agent.analyze(failure.id, session)

        assert analysis.status == AnalysisStatus.COMPLETED
        assert analysis.iterations == 2
        # The tool result must be fed back in a single user message; splitting
        # them trains the model out of parallel tool calls.
        second_turn = stub.calls[1]["messages"]
        assert second_turn[-1]["role"] == "user"
        assert second_turn[-1]["content"][0]["type"] == "tool_result"

    def test_invalid_verdict_is_returned_for_correction(
        self, session: Session, agent_settings: Settings, seed_history, verdict_response
    ) -> None:
        """Failing outright would discard a run that is one corrected field from
        done."""
        failure = seed_history()
        bad = {**VALID_VERDICT, "confidence": 1.7, "category": "NOT_A_CATEGORY"}
        agent, stub = build_agent(
            session,
            agent_settings,
            [tool_call("submit_classification", bad), verdict_response()],
        )
        analysis = agent.analyze(failure.id, session)

        assert analysis.status == AnalysisStatus.COMPLETED
        correction = stub.calls[1]["messages"][-1]["content"][0]
        assert correction["is_error"] is True
        assert "Invalid classification" in correction["content"]

    def test_category_casing_is_normalised(
        self, session: Session, agent_settings: Settings, seed_history
    ) -> None:
        """The prompt lists categories in upper case; the enum stores lower.
        Fighting the model over casing produces silent misclassification."""
        failure = seed_history()
        agent, _ = build_agent(
            session,
            agent_settings,
            [tool_call("submit_classification", {**VALID_VERDICT, "category": "FLAKY_TEST"})],
        )
        assert agent.analyze(failure.id, session).root_cause == RootCauseCategory.FLAKY_TEST

    def test_refusal_is_recorded_not_crashed_on(
        self, session: Session, agent_settings: Settings, seed_history
    ) -> None:
        """Safety classifiers return HTTP 200 with empty content. Reading
        content[0] unconditionally raises IndexError — a security suite whose
        error text contains attack payloads is a real trigger for this."""
        failure = seed_history()
        agent, _ = build_agent(
            session,
            agent_settings,
            [
                api_response(
                    [], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")
                )
            ],
        )
        analysis = agent.analyze(failure.id, session)

        assert analysis.status == AnalysisStatus.FAILED
        assert "declined" in (analysis.error_message or "")
        assert analysis.requires_human_review is True

    def test_max_tokens_is_a_distinct_diagnosis(
        self, session: Session, agent_settings: Settings, seed_history
    ) -> None:
        failure = seed_history()
        agent, _ = build_agent(
            session,
            agent_settings,
            [api_response([content_block(type="text", text="…")], stop_reason="max_tokens")],
        )
        analysis = agent.analyze(failure.id, session)
        assert analysis.status == AnalysisStatus.FAILED
        assert "MAX_TOKENS" in (analysis.error_message or "")

    def test_prose_reply_is_nudged_toward_the_tool(
        self, session: Session, agent_settings: Settings, seed_history, verdict_response
    ) -> None:
        failure = seed_history()
        agent, _ = build_agent(
            session,
            agent_settings,
            [
                api_response(
                    [content_block(type="text", text="Probably an app bug.")],
                    stop_reason="end_turn",
                ),
                verdict_response(),
            ],
        )
        analysis = agent.analyze(failure.id, session)
        assert analysis.status == AnalysisStatus.COMPLETED
        assert analysis.iterations == 2

    def test_loop_exhaustion_fails_visibly(
        self, session: Session, agent_settings: Settings, seed_history
    ) -> None:
        failure = seed_history()
        never_decides = [
            tool_call("get_full_stack_trace", {"test_result_id": failure.id}, f"t{i}")
            for i in range(agent_settings.agent_max_iterations)
        ]
        agent, _ = build_agent(session, agent_settings, never_decides)
        analysis = agent.analyze(failure.id, session)

        assert analysis.status == AnalysisStatus.FAILED
        assert "did not reach a verdict" in (analysis.error_message or "")
        assert analysis.iterations == agent_settings.agent_max_iterations

    def test_rate_limit_is_captured_not_raised(
        self, session: Session, agent_settings: Settings, seed_history
    ) -> None:
        """A background worker that raises loses the record of what it was doing."""
        failure = seed_history()

        # A real httpx.Response: anthropic's exception classes read .request off
        # it during construction, so a bare namespace raises AttributeError and
        # the test would pass for the wrong reason.
        import httpx

        rate_limited = anthropic.RateLimitError(
            "slow down",
            response=httpx.Response(
                429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            body=None,
        )

        def raise_rate_limit(**_: object) -> None:
            raise rate_limited

        class RateLimited(StubAnthropicClient):
            def __init__(self) -> None:
                super().__init__([])
                boom: Any = SimpleNamespace(create=raise_rate_limit)
                self.messages = boom
                self.beta = SimpleNamespace(messages=boom)

        agent = RootCauseAnalysisAgent(
            ContextRetriever(session, agent_settings), agent_settings, client=RateLimited()
        )
        analysis = agent.analyze(failure.id, session)
        assert analysis.status == AnalysisStatus.FAILED
        assert "rate limited" in (analysis.error_message or "")

    def test_bad_tool_arguments_do_not_abort_the_run(
        self, session: Session, agent_settings: Settings, seed_history, verdict_response
    ) -> None:
        failure = seed_history()
        agent, _ = build_agent(
            session,
            agent_settings,
            [tool_call("search_logs", {"pattern": "x"}), verdict_response()],
        )
        assert agent.analyze(failure.id, session).status == AnalysisStatus.COMPLETED

    def test_low_confidence_forces_human_review(
        self, session: Session, agent_settings: Settings, seed_history, verdict_response
    ) -> None:
        """A model can be confidently wrong but rarely claims uncertainty it does
        not have, so the threshold catches what self-assessment misses."""
        failure = seed_history()
        agent, _ = build_agent(session, agent_settings, [verdict_response(confidence=0.4)])
        assert agent.analyze(failure.id, session).requires_human_review is True

    def test_unknown_verdict_forces_human_review(
        self, session: Session, agent_settings: Settings, seed_history, verdict_response
    ) -> None:
        failure = seed_history()
        agent, _ = build_agent(
            session, agent_settings, [verdict_response(category="unknown", confidence=0.95)]
        )
        assert agent.analyze(failure.id, session).requires_human_review is True

    def test_kill_switch_raises_rather_than_recording_a_failure(
        self, session: Session, settings: Settings, seed_history
    ) -> None:
        """The agent being switched off is an operator decision, not a defect,
        and must not pollute the accuracy metrics with FAILED rows."""
        failure = seed_history()
        agent = RootCauseAnalysisAgent(
            ContextRetriever(session, settings), settings, client=StubAnthropicClient([])
        )
        with pytest.raises(AgentUnavailableError):
            agent.analyze(failure.id, session)

    def test_unknown_test_result_raises_lookup_error(
        self, session: Session, agent_settings: Settings
    ) -> None:
        agent, _ = build_agent(session, agent_settings, [])
        with pytest.raises(LookupError):
            agent.analyze("does-not-exist", session)


class TestRequestShape:
    """The request must match what current models accept."""

    @pytest.fixture
    def call(self, session: Session, agent_settings: Settings, seed_history, verdict_response):
        failure = seed_history()
        agent, stub = build_agent(session, agent_settings, [verdict_response()])
        agent.analyze(failure.id, session)
        return stub.calls[0]

    def test_omits_parameters_removed_on_current_models(self, call: dict) -> None:
        """temperature / top_p / top_k and budget_tokens all return 400 on
        Opus 5 — steering happens through the prompt and `effort` instead."""
        for removed in ("temperature", "top_p", "top_k"):
            assert removed not in call
        assert "budget_tokens" not in str(call.get("thinking", ""))

    def test_sets_effort_and_a_max_tokens_that_leaves_room_for_thinking(
        self, call: dict, agent_settings: Settings
    ) -> None:
        assert call["output_config"]["effort"] == agent_settings.anthropic_effort
        # max_tokens caps thinking AND response text together; thinking is on by
        # default on Opus 5, so a budget sized only for the answer truncates.
        assert call["max_tokens"] >= 16000

    def test_terminal_tool_uses_strict_schema(self, call: dict) -> None:
        """strict guarantees the verdict validates, which is what makes it safe
        to write straight to the database instead of parsing prose."""
        terminal = next(t for t in call["tools"] if t["name"] == "submit_classification")
        assert terminal["strict"] is True
        assert terminal["input_schema"]["additionalProperties"] is False

    def test_offers_retrieval_tools(self, call: dict) -> None:
        names = {t["name"] for t in call["tools"]}
        assert {"search_logs", "get_ci_run_summary", "find_similar_failures"} <= names

    def test_system_prompt_carries_the_discriminating_heuristics(self, call: dict) -> None:
        prompt = call["system"]
        for category in RootCauseCategory:
            assert category.value in prompt
        assert "blast radius" in prompt.lower()
        assert "same commit" in prompt.lower()
