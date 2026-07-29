"""Rule-based classifier — the offline baseline.

Why this exists, in order of importance:

1. **It is the control group.** "The agent classifies 82% correctly" means
   nothing on its own. "The agent gets 82% where deterministic rules get 54%"
   is a result. Without a baseline there is no way to tell whether the LLM is
   earning its cost, and that comparison is the single most defensible number
   this project can produce.
2. **It runs with no API key and no network.** The whole pipeline — ingest,
   cluster, classify, dashboard, feedback — works offline.
3. **It is the fallback.** When the Anthropic API is down or rate-limited,
   something still classifies.

What it is not
--------------
This is deliberately *not* an attempt to beat the LLM. It encodes the rules an
experienced SDET applies in the first thirty seconds, and it inherits their
limits: it reads signals, not meaning. It cannot tell a `NullPointerException`
in payment code from one in a logging helper, cannot judge whether a selector is
brittle, and cannot read a stack trace. Those gaps are exactly where the LLM
should win, and the point of running both is to measure by how much.

Its confidence is therefore capped below the LLM's ceiling (see
``MAX_HEURISTIC_CONFIDENCE``). A rule that fires is evidence, not proof.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

from sqlalchemy.orm import Session

from backend.analysis.agent import AgentRunResult, apply_run_result
from backend.analysis.context_retriever import ContextRetriever, FailureContext
from backend.config import Settings, get_settings
from backend.db.models import FailureAnalysisDB
from backend.db.repository import FailureAnalysisRepository
from backend.models.analysis import AgentClassification
from backend.models.enums import AnalysisStatus, RootCauseCategory

logger = logging.getLogger(__name__)

RULES_VERSION = "heuristic-v1"
"""Stored in ``prompt_version`` so heuristic and LLM analyses stay separable.

Filtering metrics by this value is what lets you compute the two accuracies
independently and compare them.
"""

MAX_HEURISTIC_CONFIDENCE = 0.80
"""Ceiling on any rule's confidence.

Rules match on surface patterns. A regex that spots ``ECONNREFUSED`` is good
evidence of an environment problem and *cannot* be proof, because the same
string appears in a test that deliberately asserts connection handling. Letting
a pattern match claim 0.95 would make the baseline look better than it is and
corrupt the comparison it exists to support.
"""


# --- Signal vocabularies ----------------------------------------------------
# Ordered most-specific-first within each group. These are matched against the
# error message, error type, and captured logs together, because which of the
# three carries the decisive string differs by framework.

_INFRASTRUCTURE = re.compile(
    r"(out of memory|oom[- ]?kill|cannot allocate memory|no space left|disk (?:is )?full"
    r"|quota exceeded on|read-only file system|permission denied|eacces|eperm"
    r"|too many open files|emfile|runner (?:lost|crashed|terminated|shut down)"
    r"|container (?:killed|exited|oom)|the job (?:running|was) cancell?ed"
    r"|no space|segmentation fault|killed signal|exit code 137)",
    re.IGNORECASE,
)

_EXTERNAL_DEPENDENCY = re.compile(
    r"(rate limit|429 too many|quota exceeded|api key|unauthorized|401 |403 forbidden"
    r"|invalid (?:token|credential)|token (?:expired|invalid)|oauth"
    r"|third[- ]party|upstream (?:error|timeout)|sandbox (?:is )?(?:down|unavailable)"
    r"|stripe|twilio|sendgrid|auth0|okta|paypal|braintree)",
    re.IGNORECASE,
)

_ENVIRONMENT = re.compile(
    r"(econnrefused|econnreset|enotfound|ehostunreach|etimedout|connection refused"
    r"|connection reset|could not connect|unable to connect|service unavailable"
    r"|\b50[234]\b|bad gateway|gateway timeout|dns|getaddrinfo|network (?:is )?unreachable"
    r"|no healthy upstream|socket hang up|ssl|certificate)",
    re.IGNORECASE,
)

_TEST_DATA = re.compile(
    r"(duplicate key|already exists|unique constraint|integrity ?error"
    r"|no (?:such )?(?:user|record|row|order|account|customer|product) (?:found|exists)"
    r"|fixture|seed(?:ed|ing)? data|test data|factory|expected .{0,40} to (?:exist|be found)"
    r"|does not exist in the database|empty (?:result|dataset)|stale (?:data|record))",
    re.IGNORECASE,
)

_TIMING = re.compile(
    r"(timeout|timed out|not visible|not attached|detached from the dom|element is not"
    r"|stale ?element|not clickable|intercepted|waiting for|race|deadlock"
    r"|is covered by another element|animation)",
    re.IGNORECASE,
)

_ASSERTION = re.compile(
    r"(assertion|assert\b|expected .{0,60}(?:to (?:equal|be|contain)|but (?:got|was|received))"
    r"|toequal|tobe\(|tohavetext|deepequal|received:)",
    re.IGNORECASE,
)


class Tier(IntEnum):
    """How a rule knows what it claims to know.

    Confidence alone cannot rank these against each other. A history-based rule
    can legitimately reach 0.78 ("this test alternates pass/fail every other
    run") while a signature match sits at 0.65 ("the error says `429 rate limit
    exceeded, stripe`") — and ranking on confidence alone would let the
    circumstantial reading beat the one that names the actual cause.

    So evidence class is the primary key and confidence only breaks ties within
    a class. Direct wins because it identifies *what* went wrong; inference only
    describes the shape of the failure.
    """

    INFERRED = 1
    """Derived from history: pass rate, flakiness, streaks, commit deltas.
    Describes a pattern, does not identify a cause."""

    DIRECT = 2
    """The failure output names the cause: ECONNREFUSED, no space left on
    device, 429 rate limit. Not proof — the same strings appear in tests that
    deliberately assert on them — but it beats a pattern."""


@dataclass
class RuleMatch:
    """One rule's verdict, before precedence is applied."""

    category: RootCauseCategory
    confidence: float
    reason: str
    evidence: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    tier: Tier = Tier.INFERRED


@dataclass
class _Signals:
    """The facts a rule can consult, flattened out of the context bundle.

    Extracted once and passed to every rule so that rules stay readable and,
    more importantly, so a rule cannot accidentally reach into a part of the
    context nobody expected it to depend on.
    """

    text: str
    error_type: str
    framework: str
    status: str
    attempt: int
    duration_ms: int
    duration_ratio: float | None
    has_baseline: bool
    history_available: bool
    prior_runs: int
    pass_rate: float
    flakiness: float
    consecutive_failures: int
    failures_7d: int
    same_commit_as_last_pass: bool
    has_commit_change: bool
    cross_framework: bool
    siblings: int
    shared_signature_siblings: int
    distinct_tests_with_signature: int
    cpu_percent: float | None
    memory_mb: float | None
    metrics_available: bool

    def matches(self, pattern: re.Pattern[str]) -> str | None:
        """Return the matched substring, so evidence can quote it."""
        found = pattern.search(self.text)
        return found.group(0) if found else None


class HeuristicClassifier:
    """Deterministic root-cause classification from measured signals."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---------------------------------------------------------------- public

    def classify(self, context: FailureContext) -> AgentClassification:
        """Apply every rule and return the strongest verdict.

        Rules are evaluated in full rather than short-circuiting on the first
        match, then ranked. Short-circuiting would make the outcome depend on
        list order, which is a bad property for something whose whole job is to
        be a stable point of comparison.
        """
        signals = self._extract(context)
        matches = [m for rule in self._rules() if (m := rule(signals)) is not None]

        # Veto, not a downweight. If the test passed and then failed on the
        # *same commit*, the application binary was identical in both runs, so a
        # code regression is ruled out by construction — no amount of
        # circumstantial confidence should be able to reinstate it.
        if signals.same_commit_as_last_pass:
            vetoed = [m for m in matches if m.category == RootCauseCategory.APP_BUG]
            matches = [m for m in matches if m.category != RootCauseCategory.APP_BUG]
            if vetoed:
                logger.debug(
                    "app_bug vetoed: pass and fail on the same commit",
                    extra={"vetoed_confidence": vetoed[0].confidence},
                )

        if not matches:
            return self._unknown(signals)

        # Evidence class first, confidence only as a tie-break within it.
        best = max(matches, key=lambda m: (m.tier, m.confidence))
        runners_up = [m for m in matches if m is not best]

        reasoning = best.reason
        if runners_up:
            alternatives = ", ".join(
                f"{m.category.value} ({m.confidence:.2f})" for m in runners_up[:3]
            )
            reasoning += f" Competing signals considered: {alternatives}."

        return AgentClassification(
            category=best.category,
            confidence=min(best.confidence, MAX_HEURISTIC_CONFIDENCE),
            reasoning=(
                f"[{RULES_VERSION}] {reasoning} "
                "This verdict comes from deterministic rules over measured signals, "
                "not from reading the stack trace or the test code."
            ),
            key_evidence=best.evidence,
            suggestions=best.suggestions,
            # Anything the rules are not confident about goes to a human. The
            # baseline should over-refer rather than over-claim: a wrong
            # confident answer is worse than an honest "look at this".
            requires_human_review=best.confidence < 0.7,
        )

    # ----------------------------------------------------------------- rules

    def _rules(self) -> list[Callable[[_Signals], RuleMatch | None]]:
        return [
            self._rule_infrastructure,
            self._rule_external_dependency,
            self._rule_environment,
            self._rule_test_data,
            self._rule_same_commit_nondeterminism,
            self._rule_unstable_history,
            self._rule_retry_recovered,
            self._rule_new_regression,
            self._rule_broken_streak,
        ]

    def _rule_infrastructure(self, s: _Signals) -> RuleMatch | None:
        """CI platform problems. Checked first — the signals are unambiguous.

        Nothing else produces "no space left on device", so when it appears
        there is no competing explanation worth weighing.
        """
        hit = s.matches(_INFRASTRUCTURE)
        if not hit:
            # Resource exhaustion sometimes shows up only in the host metrics,
            # with a timeout as the visible symptom.
            if s.metrics_available and s.cpu_percent is not None and s.cpu_percent > 95:
                return RuleMatch(
                    RootCauseCategory.INFRASTRUCTURE,
                    0.62,
                    f"The runner was at {s.cpu_percent:.0f}% CPU during execution, which "
                    "is enough to cause timeouts in tests that are otherwise correct.",
                    [f"CPU at {s.cpu_percent:.0f}% at execution time"],
                    [
                        "Check whether other jobs shared this runner",
                        "Reduce test parallelism, or move to a larger runner",
                    ],
                    tier=Tier.DIRECT,
                )
            return None

        return RuleMatch(
            RootCauseCategory.INFRASTRUCTURE,
            0.8,
            f"The failure output contains {hit!r}, which is a CI runner or host "
            "condition rather than anything the test or the application did.",
            [f"matched infrastructure signal: {hit!r}"],
            [
                "Check runner disk, memory, and permissions for this job",
                "Look at whether other jobs on the same runner failed together",
            ],
            tier=Tier.DIRECT,
        )

    def _rule_external_dependency(self, s: _Signals) -> RuleMatch | None:
        """Third-party service problems."""
        hit = s.matches(_EXTERNAL_DEPENDENCY)
        if not hit:
            return None
        return RuleMatch(
            RootCauseCategory.EXTERNAL_DEPENDENCY,
            0.65,
            f"The failure references {hit!r}, which points at a third-party service "
            "rather than your application or your test.",
            [f"matched external-dependency signal: {hit!r}"],
            [
                "Check the third party's status page for this time window",
                "Stub this dependency in CI so its availability stops gating your suite",
                "If it is a credential, check expiry and rotation",
            ],
            tier=Tier.DIRECT,
        )

    def _rule_environment(self, s: _Signals) -> RuleMatch | None:
        """Service or network problems in the environment under test.

        Cross-framework corroboration is the strongest form of this: when a
        Playwright test and a PyTest test fail with the same signature in one
        run, independently written test code broke simultaneously, which test
        defects do not do.
        """
        hit = s.matches(_ENVIRONMENT)
        if not hit and not (s.cross_framework and s.shared_signature_siblings):
            return None

        confidence = 0.68 if hit else 0.6
        evidence: list[str] = []
        reason_parts: list[str] = []

        if hit:
            evidence.append(f"matched connectivity signal: {hit!r}")
            reason_parts.append(
                f"The failure output contains {hit!r}, a connectivity or service-health "
                "signal"
            )

        if s.cross_framework and s.shared_signature_siblings:
            confidence = min(0.78, confidence + 0.12)
            evidence.append(
                f"{s.shared_signature_siblings} test(s) in other frameworks failed with "
                "the identical error signature in this CI run"
            )
            reason_parts.append(
                "and the same error appeared in a different framework in the same run, "
                "which independently written test code does not do by coincidence"
            )
        elif s.distinct_tests_with_signature > 3:
            confidence = min(0.75, confidence + 0.07)
            evidence.append(
                f"this error signature affects {s.distinct_tests_with_signature} distinct tests"
            )
            reason_parts.append(
                f"and the same signature spans {s.distinct_tests_with_signature} unrelated "
                "tests, indicating a shared cause"
            )

        return RuleMatch(
            RootCauseCategory.ENVIRONMENT,
            confidence,
            " ".join(reason_parts) + ".",
            evidence,
            [
                "Check service health and deploy events for this environment at this time",
                "Confirm the dependency was reachable from the CI network",
                "Consider a readiness gate before the suite starts",
            ],
            tier=Tier.DIRECT,
        )

    def _rule_test_data(self, s: _Signals) -> RuleMatch | None:
        """Fixture and seed-data problems."""
        hit = s.matches(_TEST_DATA)
        if not hit:
            return None
        confidence = 0.6
        evidence = [f"matched test-data signal: {hit!r}"]

        # Data problems typically survive retries — the record is still wrong.
        if s.consecutive_failures >= 2:
            confidence += 0.08
            evidence.append(
                f"failed {s.consecutive_failures} consecutive runs, consistent with a "
                "persistent data state rather than a transient race"
            )

        return RuleMatch(
            RootCauseCategory.TEST_DATA,
            confidence,
            f"The failure references {hit!r}, which points at fixture or seed data "
            "rather than application behaviour.",
            evidence,
            [
                "Check whether the fixture was seeded and not consumed by an earlier test",
                "Make this test create the data it needs rather than relying on shared state",
                "Verify test isolation — a prior test may be mutating this record",
            ],
            tier=Tier.DIRECT,
        )

    def _rule_same_commit_nondeterminism(self, s: _Signals) -> RuleMatch | None:
        """Passed and failed on the *same* commit ⇒ not a code regression.

        The single most decisive inference available, and one an error-message
        classifier can never make: if the code did not change between a pass and
        a fail, a code regression is ruled out by construction. What remains is
        non-determinism in the test, the environment, or the data.
        """
        if not s.history_available or not s.same_commit_as_last_pass:
            return None
        if s.prior_runs < 2:
            return None

        timing = s.matches(_TIMING)
        confidence = 0.72 if timing else 0.62
        evidence = [
            "this test passed and then failed on the same commit — the code did not change"
        ]
        if timing:
            evidence.append(f"matched timing signal: {timing!r}")
        if s.duration_ratio and s.duration_ratio >= 3:
            confidence = min(0.78, confidence + 0.06)
            evidence.append(
                f"ran {s.duration_ratio:.1f}x its passing baseline, so it was waiting "
                "rather than failing immediately"
            )

        return RuleMatch(
            RootCauseCategory.FLAKY_TEST,
            confidence,
            "The test passed and failed on the same commit, which rules out a code "
            "regression: the application binary was identical in both runs. That leaves "
            "non-determinism in the test itself as the most likely cause.",
            evidence,
            [
                "Replace any fixed sleep with an explicit wait on the condition itself",
                "Check for shared state or ordering dependencies with neighbouring tests",
                "Run this test 50x in isolation to confirm it is non-deterministic",
            ],
        )

    def _rule_unstable_history(self, s: _Signals) -> RuleMatch | None:
        """A history that alternates pass/fail is a flaky test by definition."""
        if not s.history_available or s.prior_runs < 5:
            return None
        if s.flakiness < 0.25:
            return None

        confidence = 0.6 + min(0.15, s.flakiness * 0.2)
        evidence = [
            f"flakiness score {s.flakiness:.2f} — the outcome changed on "
            f"{s.flakiness:.0%} of consecutive run pairs",
            f"pass rate {s.pass_rate:.0f}% over {s.prior_runs} prior runs",
        ]
        if timing := s.matches(_TIMING):
            confidence = min(0.78, confidence + 0.08)
            evidence.append(f"matched timing signal: {timing!r}")

        return RuleMatch(
            RootCauseCategory.FLAKY_TEST,
            confidence,
            f"This test's recent history alternates between pass and fail "
            f"(flakiness {s.flakiness:.2f} over {s.prior_runs} runs). A test that is "
            "sometimes green and sometimes red on similar code is non-deterministic, "
            "regardless of what today's error message says.",
            evidence,
            [
                "Quarantine this test until it is stabilised — it is currently costing "
                "trust in every other result",
                "Look for timing assumptions, shared state, or ordering dependencies",
            ],
        )

    def _rule_retry_recovered(self, s: _Signals) -> RuleMatch | None:
        """Failed then passed on retry within the same run."""
        if s.status != "flaky" and s.attempt == 0:
            return None
        if s.status != "flaky":
            return None
        return RuleMatch(
            RootCauseCategory.FLAKY_TEST,
            0.75,
            "This test failed and then passed on retry within the same CI run, against "
            "identical code and an identical environment. That is the definition of a "
            "flaky test.",
            [f"passed on attempt {s.attempt + 1} after failing earlier in the same run"],
            [
                "Fix the non-determinism rather than relying on the retry — retries hide "
                "this failure from the suite's pass rate",
                "Check for a race between the assertion and an async update",
            ],
        )

    def _rule_new_regression(self, s: _Signals) -> RuleMatch | None:
        """Long green streak, then red on a new commit ⇒ likely a real defect.

        The complement of the same-commit rule. A reliable test going red after
        a code change is the pattern that most often means the test did its job.
        """
        if not s.history_available or s.prior_runs < 5:
            return None
        if s.pass_rate < 90 or s.flakiness > 0.2:
            return None
        if not s.has_commit_change:
            return None

        confidence = 0.62
        evidence = [
            f"{s.pass_rate:.0f}% pass rate over {s.prior_runs} prior runs — this test "
            "has been reliable",
            "the failing run is on a different commit than the last passing run",
        ]

        if s.matches(_ASSERTION):
            confidence += 0.1
            evidence.append(
                "the failure is an assertion, so the test reached its check and the "
                "application behaved differently than expected"
            )
        if s.has_baseline and s.duration_ratio is not None and 0.5 <= s.duration_ratio <= 2:
            confidence += 0.05
            evidence.append(
                f"duration was normal ({s.duration_ratio:.1f}x baseline), so this is not "
                "a timeout or a hang"
            )
        if s.matches(_TIMING):
            # A timeout on a new commit is ambiguous — could be a real perf
            # regression, could be a race that only now got exposed.
            confidence -= 0.12
            evidence.append(
                "note: the error is timing-related, which weakens the app-bug reading"
            )

        return RuleMatch(
            RootCauseCategory.APP_BUG,
            confidence,
            "A test with a strong passing history failed on a new commit. The test has "
            "demonstrated it is reliable, so the most likely explanation is that the "
            "application changed.",
            evidence,
            [
                "Review the diff between the last passing commit and this one",
                "Reproduce locally against the failing commit before filing",
            ],
        )

    def _rule_broken_streak(self, s: _Signals) -> RuleMatch | None:
        """Failing consistently for many runs — broken, not flaky.

        Low confidence on the *category* by design: a long red streak says
        "somebody stopped looking at this", which is a triage fact, not a cause.
        """
        if not s.history_available or s.consecutive_failures < 5:
            return None
        if s.flakiness > 0.15:
            return None
        return RuleMatch(
            RootCauseCategory.UNKNOWN,
            0.45,
            f"This test has failed {s.consecutive_failures} consecutive runs with no "
            "intervening pass. It is consistently broken rather than flaky, but the "
            "rules cannot tell from surface signals whether the cause is the test, the "
            "application, or the environment.",
            [
                f"{s.consecutive_failures} consecutive failures",
                f"pass rate {s.pass_rate:.0f}% over {s.prior_runs} runs",
            ],
            [
                "Triage this manually — a long-running red test is either an unfixed "
                "defect or a test that should be deleted",
                "Check whether anyone owns this test",
            ],
        )

    # -------------------------------------------------------------- fallback

    def _unknown(self, s: _Signals) -> AgentClassification:
        """No rule fired.

        Returning ``UNKNOWN`` honestly is the point. A baseline that guesses
        when it has nothing would inflate its own accuracy on the easy cases and
        make the comparison against the LLM meaningless.
        """
        gaps = []
        if not s.history_available:
            gaps.append("no execution history for this test")
        if not s.has_baseline:
            gaps.append("no passing runs to establish a duration baseline")
        if not s.metrics_available:
            gaps.append("no host metrics captured")

        return AgentClassification(
            category=RootCauseCategory.UNKNOWN,
            confidence=0.25,
            reasoning=(
                f"[{RULES_VERSION}] No rule matched. The error text contains none of the "
                "recognised infrastructure, connectivity, data, or timing signals, and "
                "the execution history is not distinctive enough to infer a cause."
                + (f" Missing context: {'; '.join(gaps)}." if gaps else "")
            ),
            key_evidence=[f"error type: {s.error_type or 'unknown'}", *gaps],
            suggestions=[
                "Classify this one manually — it is the kind of case the LLM agent "
                "exists to handle",
                "If host metrics are missing, have the CI runner post cpu_percent and "
                "memory_mb with results",
            ],
            requires_human_review=True,
        )

    # --------------------------------------------------------------- signals

    def _extract(self, context: FailureContext) -> _Signals:
        """Flatten the context bundle into the facts rules consult."""
        current = context.current_failure
        history = context.historical_pattern
        ci = context.ci_run_correlation
        signature = context.signature_matches
        duration = context.duration_analysis
        metrics = context.system_metrics
        git = context.git_context

        # Message, type, and logs searched together: which one carries the
        # decisive string differs by framework (Playwright puts it in the error,
        # pytest in the longrepr, Selenium usually in the browser console).
        text = " \n".join(
            str(part)
            for part in (
                current.get("error_type"),
                current.get("error_message"),
                current.get("stack_trace_excerpt"),
                current.get("logs_excerpt"),
            )
            if part
        )

        last_pass_commit = (history.get("last_known_pass") or {}).get("git_commit")
        failing_commit = git.get("failing_commit")

        return _Signals(
            text=text,
            error_type=str(current.get("error_type") or ""),
            framework=str(current.get("framework") or ""),
            status=str(current.get("status") or ""),
            attempt=int(current.get("attempt") or 0),
            duration_ms=int(current.get("duration_ms") or 0),
            duration_ratio=duration.get("ratio_to_baseline"),
            has_baseline=bool(duration.get("available")),
            history_available=bool(history.get("available")),
            prior_runs=int(history.get("window_runs") or 0),
            pass_rate=float(history.get("pass_rate_pct") or 0.0),
            flakiness=float(history.get("flakiness_score") or 0.0),
            consecutive_failures=int(history.get("consecutive_failures_before_this") or 0),
            failures_7d=int(history.get("failures_last_7_days") or 0),
            # Both commits known AND identical. Unknown commits must not be
            # mistaken for "same commit" — that would fire the strongest rule in
            # the set on missing data.
            same_commit_as_last_pass=bool(
                last_pass_commit and failing_commit and last_pass_commit == failing_commit
            ),
            has_commit_change=bool(
                last_pass_commit and failing_commit and last_pass_commit != failing_commit
            ),
            cross_framework=bool(ci.get("cross_framework")),
            siblings=int(ci.get("other_failures_in_run") or 0),
            shared_signature_siblings=int(ci.get("sharing_identical_signature") or 0),
            distinct_tests_with_signature=int(signature.get("distinct_tests_affected") or 0),
            cpu_percent=metrics.get("cpu_percent"),
            memory_mb=metrics.get("memory_mb"),
            metrics_available=bool(metrics.get("available")),
        )


class HeuristicAnalyzer:
    """Runs the rule-based classifier and persists the verdict.

    Deliberately mirrors ``RootCauseAnalysisAgent.analyze`` — same signature,
    same row shape, same failure semantics — so callers can swap between them
    and the resulting rows stay directly comparable.
    """

    def __init__(
        self, context_retriever: ContextRetriever, settings: Settings | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.retriever = context_retriever
        self.classifier = HeuristicClassifier(self.settings)

    def analyze(self, test_result_id: str, session: Session) -> FailureAnalysisDB:
        """Classify one failed test execution and persist the verdict."""
        context = self.retriever.get_failure_context(test_result_id)
        if context is None:
            raise LookupError(f"test result {test_result_id!r} not found")

        analyses = FailureAnalysisRepository(session)
        analysis = analyses.add(
            FailureAnalysisDB(
                test_result_id=test_result_id,
                status=AnalysisStatus.PENDING,
                model=RULES_VERSION,
                prompt_version=RULES_VERSION,
            )
        )

        started = time.perf_counter()
        result = AgentRunResult(model=RULES_VERSION, iterations=1)
        try:
            result.classification = self.classifier.classify(context)
        except Exception as exc:
            # Same contract as the LLM agent: a crash becomes a visible FAILED
            # row rather than an exception escaping a background task.
            result.error = f"heuristic classification failed: {exc!r}"
            logger.exception("heuristic classifier crashed")

        # Zero tokens is the honest value and a useful one: it makes the cost
        # comparison against the LLM path fall out of the existing metrics
        # endpoint with no extra bookkeeping.
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        apply_run_result(analysis, result, self.settings)

        logger.info(
            "heuristic analysis complete",
            extra={
                "test_result_id": test_result_id,
                "root_cause": analysis.root_cause.value if analysis.root_cause else None,
                "confidence": analysis.confidence_score,
            },
        )
        return analysis


def classify_context(
    context: FailureContext, settings: Settings | None = None
) -> AgentClassification:
    """Classify an already-assembled context. Convenience for tests and scripts."""
    return HeuristicClassifier(settings).classify(context)


__all__: list[str] = [
    "MAX_HEURISTIC_CONFIDENCE",
    "RULES_VERSION",
    "HeuristicAnalyzer",
    "HeuristicClassifier",
    "RuleMatch",
    "classify_context",
]
