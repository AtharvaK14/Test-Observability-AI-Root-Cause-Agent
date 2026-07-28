"""The root-cause analysis agent — Claude API orchestration.

What makes this an *agent* rather than a prompt
-----------------------------------------------
The naive version of this feature is one API call: paste the error message in,
read a category out. That plateaus quickly, because the error message is the
least discriminating evidence available — "Timeout 30000ms exceeded" is emitted
by a genuine app hang, a missing wait, a slow CI runner, and a dead dependency
alike.

This module instead runs a loop with three properties:

1. **Grounded.** The first message carries measured context (history, blast
   radius, duration-vs-baseline, signature spread) assembled by
   ``ContextRetriever``, not just the failure text.
2. **Able to investigate.** The model has retrieval tools and can pull the full
   stack trace, grep the logs, or inspect a sibling test's history *only when it
   needs to*. Sending everything up front would cost far more and reliably bury
   the decisive line in noise.
3. **Structurally constrained.** The verdict arrives as a ``strict`` tool call
   validated against a schema, then re-validated by Pydantic. The spec's
   approach — search the response text for a marker, slice between the first
   ``{`` and last ``}``, ``json.loads`` it — fails the moment the model writes a
   brace in its prose, and fails silently.

Failure handling, stated once
-----------------------------
Every exit path writes a row. A rate limit, a malformed verdict, a refusal, an
exhausted loop — each produces a ``FailureAnalysisDB`` with ``status=FAILED``
and the reason. "The agent never ran" and "the agent ran and gave up" are
different problems with different fixes, and a system that silently drops the
second one cannot be debugged.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import anthropic
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.analysis.context_retriever import ContextRetriever
from backend.config import Settings, get_settings
from backend.db.models import FailureAnalysisDB
from backend.db.repository import FailureAnalysisRepository
from backend.models.analysis import AgentClassification
from backend.models.enums import AnalysisStatus, RootCauseCategory
from backend.utils import utcnow

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
"""Stamped on every analysis row.

Without it, "accuracy improved this week" is uninterpretable — you cannot tell
whether the prompt got better, the model changed, or the incoming failure mix
shifted. Bump it whenever the system prompt or tool schemas change, and the
feedback table becomes a controlled comparison instead of an anecdote.
"""

# Beta flag for server-side refusal fallbacks. Scoped to this module so the
# capability probe below has one place to disable it.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

TERMINAL_TOOL = "submit_classification"


class _MessagesAPI(Protocol):
    """The one method this agent calls on a messages resource."""

    def create(self, **kwargs: Any) -> Any: ...


class _BetaAPI(Protocol):
    @property
    def messages(self) -> _MessagesAPI: ...


class AnthropicLike(Protocol):
    """The slice of the Anthropic client this agent actually uses.

    Structural typing rather than the concrete ``Anthropic`` class, because the
    injection seam is the point: a test stub that replays scripted responses
    satisfies this Protocol, whereas typing the parameter as ``Anthropic`` would
    force every test to either construct a real client or lie to the type
    checker. Naming the *used* surface also documents how small it is — one
    method, on two paths.

    Declared as read-only properties rather than plain attributes: a mutable
    Protocol attribute is invariant, so ``messages: _MessagesAPI`` would reject
    any implementation whose attribute is a *subtype* — which is every real
    implementation, including the SDK's own.
    """

    @property
    def messages(self) -> _MessagesAPI: ...

    @property
    def beta(self) -> _BetaAPI: ...


class AgentError(RuntimeError):
    """Base class for agent failures that callers may want to distinguish."""


class AgentUnavailableError(AgentError):
    """No API key configured, or the agent is disabled by the kill switch.

    Separate from a *failed* analysis: the agent being switched off is an
    operator decision, not a defect, and should not pollute the accuracy metrics
    with FAILED rows.
    """


@dataclass
class AgentRunResult:
    """Everything one agent run produced, including when it produced nothing."""

    classification: AgentClassification | None = None
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    model: str = ""

    @property
    def succeeded(self) -> bool:
        return self.classification is not None


class RootCauseAnalysisAgent:
    """Classifies a failed test execution into a root cause with evidence."""

    def __init__(
        self,
        context_retriever: ContextRetriever,
        settings: Settings | None = None,
        client: AnthropicLike | None = None,
    ) -> None:
        """Construct the agent.

        ``client`` is injectable so unit tests can supply a stub that returns
        canned responses. Without that seam, testing the loop's control flow
        (does it stop on the terminal tool? does it survive a malformed verdict?)
        would require either a live API key or monkeypatching the SDK.
        """
        self.settings = settings or get_settings()
        self.retriever = context_retriever
        self._client: AnthropicLike | None = client
        self._fallbacks_supported = self.settings.anthropic_enable_refusal_fallback

    # ------------------------------------------------------------------ setup

    @property
    def client(self) -> AnthropicLike:
        """Lazily construct the SDK client.

        Lazy so that importing this module — which the API layer does at
        startup — never fails on a missing key. Ingestion must keep working
        with the agent switched off.
        """
        if self._client is not None:
            return self._client
        if not self.settings.anthropic_api_key:
            raise AgentUnavailableError(
                "ANTHROPIC_API_KEY is not set; ingestion works but analysis is disabled"
            )
        # The one cast in this module, and it is unavoidable: the SDK's
        # `messages.create` is a heavily-overloaded signature with named
        # keyword-only parameters, which no `**kwargs` Protocol method can
        # structurally match. Confined to this single construction site — every
        # other reference goes through the Protocol and stays checked.
        created = cast(
            AnthropicLike,
            anthropic.Anthropic(
                api_key=self.settings.anthropic_api_key,
                timeout=self.settings.anthropic_timeout_seconds,
                max_retries=self.settings.anthropic_max_retries,
            ),
        )
        self._client = created
        return created

    # ------------------------------------------------------------- public API

    def analyze(self, test_result_id: str, session: Session) -> FailureAnalysisDB:
        """Analyse one failed test execution and persist the verdict.

        Always returns a row. Raises only ``AgentUnavailableError`` (agent off)
        and ``LookupError`` (no such test result) — everything else is captured
        on the row as a FAILED analysis, because a background worker that raises
        loses the record of what it was doing.
        """
        if not self.settings.agent_enabled:
            raise AgentUnavailableError("agent is disabled (AGENT_ENABLED=false)")

        analyses = FailureAnalysisRepository(session)
        context = self.retriever.get_failure_context(test_result_id)
        if context is None:
            raise LookupError(f"test result {test_result_id!r} not found")

        # Persist PENDING before the call. A worker that dies mid-analysis then
        # leaves a visible stuck row rather than no row at all.
        analysis = analyses.add(
            FailureAnalysisDB(
                test_result_id=test_result_id,
                status=AnalysisStatus.PENDING,
                model=self.settings.anthropic_model,
                prompt_version=PROMPT_VERSION,
            )
        )

        result = self._run(context.to_dict())
        self._apply_result(analysis, result)

        logger.info(
            "analysis complete",
            extra={
                "test_result_id": test_result_id,
                "status": analysis.status.value,
                "root_cause": analysis.root_cause.value if analysis.root_cause else None,
                "confidence": analysis.confidence_score,
                "iterations": result.iterations,
                "latency_ms": result.latency_ms,
            },
        )
        return analysis

    # ------------------------------------------------------------- agent loop

    def _run(self, context: dict[str, Any]) -> AgentRunResult:
        """Drive the tool-use loop until the model submits a classification.

        A hand-written loop rather than the SDK's beta ``tool_runner``: the
        runner is a beta surface, and this loop needs to stop on a *specific*
        tool (``submit_classification``) while accumulating per-iteration token
        usage for the cost metrics. Both are clearer written out than configured.
        """
        result = AgentRunResult(model=self.settings.anthropic_model)
        started = time.perf_counter()

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._build_initial_message(context)}
        ]

        try:
            for iteration in range(self.settings.agent_max_iterations):
                result.iterations = iteration + 1
                response = self._create_message(messages)

                result.input_tokens += response.usage.input_tokens or 0
                result.output_tokens += response.usage.output_tokens or 0

                # Safety classifiers can decline a request and still return HTTP
                # 200. Reading response.content[0] without this check raises
                # IndexError on an empty content list — the classic way this
                # surfaces as a mystery crash rather than a handled outcome.
                if response.stop_reason == "refusal":
                    detail = getattr(response, "stop_details", None)
                    category = getattr(detail, "category", None) if detail else None
                    result.error = (
                        f"request declined by safety classifiers (category={category}). "
                        "This can happen for security test suites whose error text "
                        "contains attack payloads."
                    )
                    break

                if response.stop_reason == "max_tokens":
                    result.error = (
                        "response hit max_tokens before a verdict was submitted; "
                        "raise ANTHROPIC_MAX_TOKENS"
                    )
                    break

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    # The model answered in prose instead of calling a tool.
                    # Nudge rather than fail — this is recoverable and costs one turn.
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Call the {TERMINAL_TOOL} tool with your verdict. "
                                "If the evidence is insufficient, submit 'unknown' "
                                "with low confidence rather than describing it in prose."
                            ),
                        }
                    )
                    continue

                # Echo the assistant turn back verbatim. This preserves thinking
                # blocks exactly as received, which the API requires — rebuilding
                # or filtering them breaks the next turn.
                messages.append({"role": "assistant", "content": response.content})

                tool_results: list[dict[str, Any]] = []
                classification: AgentClassification | None = None

                for block in tool_uses:
                    result.tool_calls.append(block.name)

                    if block.name == TERMINAL_TOOL:
                        parsed, error = self._parse_classification(block.input)
                        if parsed is not None:
                            classification = parsed
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": "Classification recorded.",
                                }
                            )
                        else:
                            # Hand the validation error back so the model can fix
                            # it on the next turn. Failing outright here would
                            # discard a run that is one corrected field from done.
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": f"Invalid classification: {error}",
                                    "is_error": True,
                                }
                            )
                        continue

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(
                                self._dispatch_tool(block.name, dict(block.input)),
                                default=str,
                            ),
                        }
                    )

                if classification is not None:
                    result.classification = classification
                    break

                # All results for a turn go back in ONE user message. Splitting
                # them trains the model to stop making parallel tool calls.
                messages.append({"role": "user", "content": tool_results})
            else:
                result.error = (
                    f"agent did not reach a verdict within "
                    f"{self.settings.agent_max_iterations} iterations"
                )

        except anthropic.RateLimitError as exc:
            result.error = f"rate limited by Anthropic API: {exc}"
            logger.warning("agent rate limited", extra={"error": str(exc)})
        except anthropic.APIConnectionError as exc:
            result.error = f"could not reach Anthropic API: {exc}"
            logger.warning("agent connection failure", extra={"error": str(exc)})
        except anthropic.APIStatusError as exc:
            result.error = f"Anthropic API error {exc.status_code}: {exc.message}"
            logger.error("agent api error", extra={"status": exc.status_code})
        except AgentUnavailableError:
            raise
        except Exception as exc:
            # A background worker must not die on an unforeseen error; the row
            # records what happened so the failure is visible in the dashboard.
            result.error = f"unexpected agent error: {exc!r}"
            logger.exception("unexpected agent failure")

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    def _create_message(self, messages: list[dict[str, Any]]) -> Any:
        """One API call, with a one-time probe for refusal-fallback support.

        Fallbacks are model-gated. Rather than hard-coding which models allow
        them (a list that goes stale), we try once and permanently downgrade for
        this process if the parameter is rejected — the analysis still runs,
        just without the safety net.
        """
        params: dict[str, Any] = {
            "model": self.settings.anthropic_model,
            "max_tokens": self.settings.anthropic_max_tokens,
            "system": self._build_system_prompt(),
            "messages": messages,
            "tools": self._tool_definitions(),
            "output_config": {"effort": self.settings.anthropic_effort},
        }

        if not self._fallbacks_supported:
            return self.client.messages.create(**params)

        try:
            return self.client.beta.messages.create(
                **params, betas=[_FALLBACK_BETA], fallbacks="default"
            )
        except anthropic.BadRequestError as exc:
            if "fallback" not in str(exc).lower():
                raise
            logger.warning(
                "refusal fallbacks unsupported for this model; continuing without",
                extra={"model": self.settings.anthropic_model},
            )
            self._fallbacks_supported = False
            return self.client.messages.create(**params)

    @staticmethod
    def _parse_classification(
        payload: Any,
    ) -> tuple[AgentClassification | None, str | None]:
        """Validate the model's tool input into a typed classification."""
        try:
            return AgentClassification.model_validate(payload), None
        except ValidationError as exc:
            return None, "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )

    def _dispatch_tool(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Execute a retrieval tool.

        Never raises. A tool error is returned to the model as data so it can
        adapt — a crashed dispatcher would abort an analysis that was one
        recoverable mistake (a bad regex, a typo'd test name) from succeeding.
        """
        try:
            match name:
                case "get_full_stack_trace":
                    return self.retriever.tool_get_full_stack_trace(
                        tool_input["test_result_id"]
                    )
                case "search_logs":
                    return self.retriever.tool_search_logs(
                        tool_input["test_result_id"],
                        pattern=tool_input.get("pattern"),
                        max_lines=int(tool_input.get("max_lines", 80)),
                    )
                case "get_test_history":
                    return self.retriever.tool_get_test_history(
                        tool_input["test_name"], limit=int(tool_input.get("limit", 30))
                    )
                case "get_ci_run_summary":
                    return self.retriever.tool_get_ci_run_summary(tool_input["ci_run_id"])
                case "find_similar_failures":
                    return self.retriever.tool_find_similar_failures(
                        tool_input["failure_signature"],
                        days=int(tool_input.get("days", 14)),
                    )
                case _:
                    return {"error": f"unknown tool {name!r}"}
        except KeyError as exc:
            return {"error": f"missing required argument {exc}"}
        except Exception as exc:
            logger.warning("tool dispatch failed", extra={"tool": name, "error": str(exc)})
            return {"error": f"tool {name} failed: {exc}"}

    # --------------------------------------------------------- persistence

    def _apply_result(self, analysis: FailureAnalysisDB, result: AgentRunResult) -> None:
        """Write the run's outcome onto the pending analysis row."""
        analysis.iterations = result.iterations
        analysis.input_tokens = result.input_tokens
        analysis.output_tokens = result.output_tokens
        analysis.latency_ms = result.latency_ms
        analysis.model = result.model
        analysis.updated_at = utcnow()

        if result.classification is None:
            analysis.status = AnalysisStatus.FAILED
            analysis.error_message = result.error or "agent produced no classification"
            analysis.requires_human_review = True
            return

        verdict = result.classification
        analysis.status = AnalysisStatus.COMPLETED
        analysis.root_cause = verdict.category
        analysis.confidence_score = verdict.confidence
        analysis.reasoning = verdict.reasoning
        analysis.key_evidence = verdict.key_evidence
        analysis.suggestions = verdict.suggestions

        # Flag for review on the model's own request OR on low confidence. Both,
        # because a model can be confidently wrong but rarely claims uncertainty
        # it does not have — the threshold catches what self-assessment misses.
        analysis.requires_human_review = (
            verdict.requires_human_review
            or verdict.confidence < self.settings.analysis_confidence_review_threshold
            or verdict.category == RootCauseCategory.UNKNOWN
        )

    # ------------------------------------------------------------- prompting

    def _build_system_prompt(self) -> str:
        """The agent's operating instructions.

        The valuable part is not the category list — it is the *discriminating
        heuristics*. Any model can recite that a flaky test has timing issues;
        what makes classification accurate is knowing that an identical error in
        two different frameworks in the same CI run rules out test defects, and
        that a test which passed and then failed on the same commit rules out
        code regressions.
        """
        return f"""\
You are a senior SDET triaging test failures for a multi-framework CI pipeline
(Playwright, Cypress, PyTest, Selenium). Your job is to determine WHY a test
failed and hand the right team something they can act on.

## Categories

Choose exactly one. They are separated by *who fixes it*, which is what makes
the answer useful:

- `app_bug` — a real defect in the application. The test worked correctly and
  caught something. Goes to the dev team.
- `flaky_test` — a defect in the test: missing wait, race condition, brittle
  locator, order dependency, hard-coded sleep. The application is fine. Goes to
  the test author.
- `environment` — the environment the app runs in: a service that was down, a
  bad deploy, network latency, resource contention on the app side. Goes to the
  platform team.
- `test_data` — invalid, missing, stale, or already-consumed fixture data; a
  seeded account in the wrong state. Goes to whoever owns the fixtures.
- `infrastructure` — the CI platform itself: runner OOM, disk full, permissions,
  container image drift, agent lost. Goes to the CI team.
- `external_dependency` — a third party: API timeout, expired credential, rate
  limit, sandbox outage. Usually not fixable directly; needs a stub or retry.
- `unknown` — insufficient evidence. Use it. A confident wrong answer destroys
  trust in this tool faster than an honest "I cannot tell from this".

## How to read the evidence

These signals discriminate far better than the error message does. Weigh them:

1. **Historical pattern is the strongest single signal.** A test with a long
   green streak that just went red points hard at a code or environment change.
   A test that has been alternating pass/fail for weeks points hard at the test
   itself. The error message can be *identical* in both cases.

2. **Same commit, different outcome ⇒ not a code regression.** If the test
   passed and then failed with no commit in between, the code did not change.
   That leaves non-determinism, environment, or data.

3. **Blast radius.** One test red and everything else green implicates that test
   or its feature. A whole suite red together implicates shared setup — auth,
   fixtures, seed data. Failures across *different frameworks* in one run are
   near-proof of an application or environment fault, because independently
   written test code does not break simultaneously by coincidence.

4. **Duration versus baseline.** A run that burned its full timeout was
   *waiting* — race, missing wait, slow dependency. A run that died far faster
   than baseline hit an immediate error — element genuinely absent, connection
   refused, setup error before the test body. Same message, different cause.

5. **Signature spread.** One normalised error appearing across many unrelated
   tests is a shared cause, not many independent test defects.

6. **Absent evidence is not negative evidence.** If system metrics were not
   captured, you cannot rule out resource contention — say so rather than
   assuming the environment was healthy. Check `retrieval_notes` for what was
   unavailable or truncated.

## Investigating

You have tools to fetch the full stack trace, grep the logs, pull another
test's history, summarise a CI run, or find other occurrences of this failure
signature. Use them when a specific question would change your verdict — for
browser tests especially, the real cause is often a 5xx or a connection error in
the console logs rather than anything in the assertion text. Do not fetch data
you will not use.

## Confidence

Calibrate honestly. Roughly:
- 0.9+ : direct evidence, a specific mechanism, and no competing explanation.
- 0.7–0.9 : strong evidence, one plausible alternative remains.
- 0.5–0.7 : leaning, but the evidence is genuinely ambiguous.
- <0.5 : guessing — prefer `unknown` and say what would settle it.

Set `requires_human_review` when a wrong call would be expensive: an `app_bug`
verdict that would open a defect against a team, or anything under 0.6.

## Suggestions

Be specific and actionable. "Add an explicit wait for the '#submit' button
before clicking, replacing the fixed 2s sleep at line 42" is useful.
"Investigate the timeout" is not. Where the evidence points at a specific file,
commit range, or service, name it.

## Finishing

Call `{TERMINAL_TOOL}` exactly once when you have a verdict. Put your argument
in `reasoning` and the specific observations it rests on in `key_evidence`, so a
reviewer can check the facts without re-reading the prose.

Prompt version: {PROMPT_VERSION}
"""

    def _build_initial_message(self, context: dict[str, Any]) -> str:
        """Render the assembled context as the opening user turn."""
        sections = [
            ("CURRENT FAILURE", context.get("current_failure")),
            ("HISTORICAL PATTERN FOR THIS TEST", context.get("historical_pattern")),
            ("OTHER FAILURES IN THE SAME CI RUN", context.get("ci_run_correlation")),
            ("THIS FAILURE SIGNATURE ELSEWHERE", context.get("signature_matches")),
            ("DURATION VS PASSING BASELINE", context.get("duration_analysis")),
            ("SYSTEM METRICS DURING EXECUTION", context.get("system_metrics")),
            ("CODE CHANGE CONTEXT", context.get("git_context")),
            ("ENVIRONMENT", context.get("environment_factors")),
            ("ARTEFACTS", context.get("artifacts")),
            ("RETRIEVAL NOTES (gaps and truncations)", context.get("retrieval_notes")),
        ]
        rendered = "\n\n".join(
            f"## {title}\n{json.dumps(body, indent=2, default=str)}"
            for title, body in sections
            if body
        )
        return (
            f"Analyse this test failure.\n\n"
            f"The test result id is `{context['test_result_id']}` — pass it to any "
            f"tool that needs one.\n\n{rendered}"
        )

    # ---------------------------------------------------------------- tools

    def _tool_definitions(self) -> list[dict[str, Any]]:
        """Tool schemas exposed to the model.

        The retrieval tools are the "request additional data if needed" step of
        the agentic loop: cheap to offer, only paid for when used.
        """
        return [
            {
                "name": "get_full_stack_trace",
                "description": (
                    "Fetch the complete stack trace for a test result. The initial "
                    "context includes only an excerpt; call this when the frames "
                    "beyond it would change your verdict — for example to tell "
                    "whether the failure originated in application code or in the "
                    "test's own helpers."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "test_result_id": {
                            "type": "string",
                            "description": "The test result id from the context.",
                        }
                    },
                    "required": ["test_result_id"],
                },
            },
            {
                "name": "search_logs",
                "description": (
                    "Grep the captured stdout/stderr/console logs for a run. For "
                    "browser tests the decisive evidence is usually here rather "
                    "than in the assertion message: a 5xx on an XHR, ECONNREFUSED, "
                    "a CORS rejection, an unhandled promise rejection. Omit the "
                    "pattern to get all error-like lines."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "test_result_id": {"type": "string"},
                        "pattern": {
                            "type": "string",
                            "description": (
                                "Case-insensitive regular expression, e.g. "
                                "'ECONNREFUSED|50[0-9]|timeout'."
                            ),
                        },
                        "max_lines": {
                            "type": "integer",
                            "description": "Maximum matching lines to return (default 80).",
                        },
                    },
                    "required": ["test_result_id"],
                },
            },
            {
                "name": "get_test_history",
                "description": (
                    "Execution history for any test by name — including tests other "
                    "than the one under analysis. Use it to check whether a "
                    "suspected sibling failure shares this test's pattern."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "test_name": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "description": "How many recent runs (default 30, max 100).",
                        },
                    },
                    "required": ["test_name"],
                },
            },
            {
                "name": "get_ci_run_summary",
                "description": (
                    "Every failure in a CI run, grouped by failure signature. Use it "
                    "to measure blast radius: whether this run had one problem or "
                    "several, and whether the failures span frameworks."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"ci_run_id": {"type": "string"}},
                    "required": ["ci_run_id"],
                },
            },
            {
                "name": "find_similar_failures",
                "description": (
                    "Find every other occurrence of a failure signature across all "
                    "tests, environments, and frameworks. The same signature in "
                    "unrelated tests indicates a shared cause rather than many "
                    "independent test defects."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "failure_signature": {"type": "string"},
                        "days": {
                            "type": "integer",
                            "description": "Lookback window in days (default 14).",
                        },
                    },
                    "required": ["failure_signature"],
                },
            },
            {
                "name": TERMINAL_TOOL,
                "description": (
                    "Submit your final verdict. Call this exactly once, when you "
                    "have reached a conclusion."
                ),
                # strict=True guarantees the input validates against this schema,
                # which is what makes the verdict safe to write straight to the
                # database instead of parsing prose and hoping.
                "strict": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": [c.value for c in RootCauseCategory],
                            "description": "The root cause category.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "Calibrated confidence from 0.0 to 1.0.",
                        },
                        "reasoning": {
                            "type": "string",
                            "description": (
                                "Why this category and not the closest alternative. "
                                "A reviewer must be able to check your argument."
                            ),
                        },
                        "key_evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The specific observations the verdict rests on, one "
                                "per item, each citing a concrete fact from the "
                                "context or a tool result."
                            ),
                        },
                        "suggestions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Concrete remediation steps, most impactful first."
                            ),
                        },
                        "requires_human_review": {
                            "type": "boolean",
                            "description": (
                                "True when a wrong verdict here would be costly or "
                                "the evidence is thin."
                            ),
                        },
                    },
                    "required": [
                        "category",
                        "confidence",
                        "reasoning",
                        "key_evidence",
                        "suggestions",
                        "requires_human_review",
                    ],
                    "additionalProperties": False,
                },
            },
        ]


def analyze_test_result(test_result_id: str, settings: Settings | None = None) -> None:
    """Background-task entry point: analyse one failure in its own session.

    Opens a fresh session because FastAPI's request-scoped session is already
    closed by the time a background task runs — reusing it raises
    ``DetachedInstanceError`` at a confusing distance from the cause.
    """
    from backend.db.session import session_scope  # local import: avoids a cycle

    settings = settings or get_settings()
    try:
        with session_scope() as session:
            agent = RootCauseAnalysisAgent(
                context_retriever=ContextRetriever(session, settings), settings=settings
            )
            agent.analyze(test_result_id, session)
    except AgentUnavailableError as exc:
        logger.info("skipping analysis", extra={"reason": str(exc)})
    except LookupError as exc:
        logger.warning("analysis target missing", extra={"error": str(exc)})
    except Exception:
        # Background tasks have no caller to propagate to; an unlogged exception
        # here is a silently dropped analysis.
        logger.exception(
            "background analysis crashed", extra={"test_result_id": test_result_id}
        )


__all__ = [
    "PROMPT_VERSION",
    "AgentError",
    "AgentRunResult",
    "AgentUnavailableError",
    "RootCauseAnalysisAgent",
    "analyze_test_result",
]
