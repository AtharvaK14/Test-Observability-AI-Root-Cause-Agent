"""Deterministic failure fingerprinting and cluster maintenance.

The core idea: **two failures are the same problem if their error text is the
same once you delete the parts that vary between runs.**

An overnight CI cycle produces 487 red tests. Almost always that is three or
four actual problems, replicated across retries, shards, and parameterised
cases. A triage queue of 487 rows gets ignored; a queue of 4 gets worked. That
collapse is the highest-leverage thing this system does, and it is worth
noticing that it needs no LLM at all.

Which is the point of keeping it here, separate and deterministic:

* **It works when Anthropic does not.** Ingestion, grouping, and the dashboard
  keep functioning during an API outage; only the *explanation* is missing.
* **It is cheap.** Fingerprinting 487 failures costs one hash each. Classifying
  them costs 487 API calls — or 4, if you cluster first and analyse one
  representative per cluster. Same output, two orders of magnitude less spend.
* **It is testable.** Same input, same hash, forever. You can unit-test it,
  which you cannot meaningfully do to a model's judgement.

The rule of thumb behind every pattern below: strip anything that changes when
you run the same failing test twice.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypedDict

from sqlalchemy.orm import Session

from backend.db.models import FailureClusterDB, TestResultDB
from backend.db.repository import FailureClusterRepository, TestResultRepository
from backend.models.enums import RootCauseCategory, TestFramework
from backend.utils import utcnow

logger = logging.getLogger(__name__)

SIGNATURE_VERSION = "v1"
"""Bump when the normalisation rules change.

Mixed into every hash, so old and new signatures cannot collide. Without it, a
rule change silently merges unrelated historical clusters and every occurrence
count in the dashboard becomes a lie — with no error to tell you.
"""

# --- Volatile substitutions -------------------------------------------------
# Ordered: the specific must run before the general, or a broad numeric rule
# will eat the insides of a UUID and defeat the specific rule that follows.
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    # UUIDs — test data ids, session ids, correlation ids.
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
        "<UUID>",
    ),
    # ISO-8601 timestamps.
    (
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<TIMESTAMP>",
    ),
    # Memory addresses / object hashes: "<Foo object at 0x7f3a2b>".
    (re.compile(r"0x[0-9a-f]+", re.I), "<ADDR>"),
    # URLs — host and port vary per environment and per ephemeral container.
    (re.compile(r"https?://[^\s\"'<>)\]]+"), "<URL>"),
    # IPv4 addresses. Needs its own rule: the generic numeric rule below refuses
    # to match a digit preceded by a dot (so identifiers like "v1.2" survive),
    # which leaves "10.0.0.4" partially substituted and makes two container IPs
    # in the same ECONNREFUSED message hash differently.
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<IP>"),
    # Absolute paths. CI checkout dirs embed a run id, so raw paths would make
    # every run of the same failure look unique.
    (re.compile(r"(?:[A-Za-z]:)?[\\/](?:[\w.\-]+[\\/]){2,}[\w.\-]+"), "<PATH>"),
    # Long hex blobs: git SHAs, hashes, tokens.
    (re.compile(r"\b[0-9a-f]{7,40}\b", re.I), "<HEX>"),
    # Ports.
    (re.compile(r":\d{2,5}\b"), ":<PORT>"),
    # Any remaining number, including durations. Deliberately aggressive:
    # "Timeout 30000ms exceeded" and "Timeout 29997ms exceeded" are one problem,
    # and treating them as two is the single most common fingerprinting failure.
    #
    # A trailing ``\b`` would be wrong here and the bug is easy to ship: in
    # "30000ms" there is no word boundary between "0" and "m" (both are word
    # characters), so ``\b\d+\b`` never matches a number with a unit suffix —
    # which is *every duration in every timeout message*. The leading lookbehind
    # keeps us from mangling identifiers like "oauth2" or "h1".
    #
    # The cost of this aggression, stated plainly: "expected 200 to equal 404"
    # and "expected 200 to equal 500" collapse into one cluster. That is a real
    # loss of precision, accepted because unstable duration values fragment
    # clusters far more often than status codes merge them wrongly, and because
    # the error type usually still separates those two cases.
    (re.compile(r"(?<![\w.])\d+(?:\.\d+)?"), "<N>"),
    # Collapse whitespace last.
    (re.compile(r"\s+"), " "),
]

# --- Error type extraction --------------------------------------------------
# The exception class is the strongest cheap signal available. Timeouts and
# assertion failures have almost disjoint root-cause distributions: a timeout is
# usually waiting on something (race, slow dependency, missing element), an
# assertion is usually a genuine behavioural difference.
_ERROR_TYPE_PATTERNS: list[re.Pattern[str]] = [
    # Python: "ValueError: bad input" / "selenium.common.exceptions.TimeoutException"
    re.compile(r"^(?:[\w.]+\.)?(?P<type>[A-Z]\w*(?:Error|Exception|Failure|Warning))\b"),
    # JS/Playwright/Cypress: "TimeoutError: ..." / "AssertionError: ..."
    re.compile(r"\b(?P<type>[A-Z]\w*(?:Error|Exception))\s*:"),
    # Playwright's characteristic message shape.
    re.compile(r"(?P<type>Timeout)\s+\d+m?s\s+exceeded", re.I),
    # PyTest bare assert.
    re.compile(r"^(?P<type>assert)\b"),
]

_FRAMEWORK_FALLBACK_TYPES: dict[TestFramework, str] = {
    TestFramework.PLAYWRIGHT: "PlaywrightError",
    TestFramework.CYPRESS: "CypressError",
    TestFramework.PYTEST: "PytestFailure",
    TestFramework.SELENIUM: "WebDriverError",
}


def normalize_error_text(text: str | None, max_length: int = 600) -> str:
    """Reduce an error message to its run-invariant skeleton.

    ``Timeout 30000ms exceeded waiting for locator('#submit-btn')`` and
    ``Timeout 29997ms exceeded waiting for locator('#submit-btn')`` both become
    ``timeout <N>ms exceeded waiting for locator('#submit-btn')`` — same
    problem, same hash.

    Note what is *kept*: the selector. Two timeouts on different elements are
    different problems, and stripping selectors would over-merge them into one
    useless mega-cluster.
    """
    if not text:
        return ""

    normalized = text.strip()
    for pattern, replacement in _NORMALIZERS:
        normalized = pattern.sub(replacement, normalized)

    # Only the first lines matter: the head of an error is stable, while the
    # tail carries per-run noise (retry counts, captured locals, "8 more...").
    normalized = " ".join(normalized.splitlines()[:5])
    return normalized.strip().lower()[:max_length]


def extract_error_type(
    error_message: str | None,
    stack_trace: str | None = None,
    framework: TestFramework | None = None,
) -> str | None:
    """Pull the exception class out of free-form error text.

    Frameworks bury it in different places, so we try the message, then the
    stack, then fall back to a framework-shaped label. Returning ``None`` would
    push the work onto the LLM for something a regex handles for free.
    """
    for source in (error_message, stack_trace):
        if not source:
            continue
        head = source.strip()
        for line in head.splitlines()[:8]:
            line = line.strip()
            if not line:
                continue
            for pattern in _ERROR_TYPE_PATTERNS:
                match = pattern.search(line)
                if match:
                    return match.group("type")

    if framework is not None and (error_message or stack_trace):
        return _FRAMEWORK_FALLBACK_TYPES.get(framework)
    return None


def compute_failure_signature(
    error_message: str | None,
    error_type: str | None = None,
    stack_trace: str | None = None,
) -> str | None:
    """Stable hash identifying "this specific problem".

    Deliberately **excludes the test name**. Including it would guarantee that
    every test gets its own cluster, which destroys the most valuable inference
    the system can make: an identical normalised error appearing in unrelated
    tests, across different frameworks, is near-proof that the fault is in the
    application or the environment rather than in any test — independent test
    code does not produce identical errors by coincidence.

    Returns ``None`` for a failure with no usable error text, rather than
    hashing the empty string. Otherwise every detail-free failure collapses into
    one enormous meaningless cluster.
    """
    normalized = normalize_error_text(error_message)
    if not normalized and stack_trace:
        # Fall back to the stack's frame shape when there is no message —
        # common for hook/fixture errors.
        normalized = normalize_error_text(stack_trace, max_length=400)
    if not normalized:
        return None

    payload = f"{SIGNATURE_VERSION}|{(error_type or 'unknown').lower()}|{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_dedupe_key(
    ci_run_id: str, framework: TestFramework | str, test_name: str, attempt: int
) -> str:
    """Idempotency key for one execution attempt.

    CI upload steps get retried after network blips, and pipelines get re-run.
    Without this, a duplicated report doubles every failure count on the
    dashboard — a corruption that is invisible until someone questions a number
    and nobody can reproduce it.
    """
    framework_value = framework.value if isinstance(framework, TestFramework) else framework
    payload = f"{ci_run_id}|{framework_value}|{test_name}|{attempt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class ClusterService:
    """Maintains the ``failure_clusters`` table as failures arrive."""

    def __init__(self, session: Session) -> None:
        self.clusters = FailureClusterRepository(session)
        self.test_runs = TestResultRepository(session)

    def upsert_for_result(self, result: TestResultDB) -> FailureClusterDB | None:
        """Attach a failure to its cluster, creating the cluster if new.

        Called during ingestion, inside the caller's transaction — the cluster
        and the test run must land together or not at all, otherwise a crash
        mid-ingest leaves counts that no longer match the rows behind them.
        """
        if not result.failure_signature:
            return None

        cluster = self.clusters.get_by_signature(result.failure_signature)
        now = utcnow()

        if cluster is None:
            cluster = FailureClusterDB(
                pattern_signature=result.failure_signature,
                representative_error=(result.error_message or "")[:2000] or None,
                occurrence_count=1,
                affected_tests=[result.test_name],
                affected_frameworks=[result.framework.value],
                first_seen=result.timestamp or now,
                last_seen=result.timestamp or now,
            )
            self.clusters.add(cluster)
            logger.info(
                "new failure cluster created",
                extra={
                    "signature": result.failure_signature[:12],
                    "test_name": result.test_name,
                },
            )
            return cluster

        cluster.occurrence_count += 1
        cluster.last_seen = max(cluster.last_seen, result.timestamp or now)

        # Lists are reassigned rather than mutated in place: SQLAlchemy does not
        # track in-place mutation of a plain JSON column, so `.append(...)` would
        # be silently discarded at flush. This is a genuinely easy bug to ship.
        if result.test_name not in cluster.affected_tests:
            cluster.affected_tests = [*cluster.affected_tests, result.test_name]
        if result.framework.value not in cluster.affected_frameworks:
            cluster.affected_frameworks = [
                *cluster.affected_frameworks,
                result.framework.value,
            ]

        return cluster

    def apply_analysis_to_cluster(
        self,
        cluster: FailureClusterDB,
        root_cause: RootCauseCategory,
        confidence: float,
        suggested_fix: str | None = None,
    ) -> None:
        """Promote an individual verdict to the whole cluster.

        Only overwrites when the new verdict is *more* confident, so one
        low-confidence guess cannot relabel a pattern that a strong analysis
        already explained.
        """
        if cluster.confidence is None or confidence > cluster.confidence:
            cluster.root_cause = root_cause
            cluster.confidence = confidence
            if suggested_fix:
                cluster.suggested_fix = suggested_fix

    def cluster_stats(self, signature: str, since: datetime | None = None) -> dict[str, object]:
        """Summarise a cluster's spread — fed to the agent as context.

        ``distinct_tests`` is the number that changes a verdict: one test with 40
        occurrences is a flaky test, whereas 40 different tests sharing one
        signature is an outage.
        """
        occurrences = self.test_runs.count_by_signature(signature, since=since)
        tests = self.test_runs.get_distinct_tests_for_signature(signature)
        return {
            "occurrences": occurrences,
            "distinct_tests": len(tests),
            "sample_tests": list(tests[:10]),
        }


class SignatureGroup(TypedDict):
    """One distinct problem within a batch of failures."""

    signature: str
    count: int
    distinct_tests: int
    frameworks: list[str]
    example_error: str | None


@dataclass
class _GroupAccumulator:
    """Mutable tally for one signature while scanning a batch.

    A dataclass rather than a dict-of-mixed-types: the dict version needed a
    ``type: ignore`` on every line, and each of those suppressions could equally
    have been hiding a genuine ``str``-where-``set``-expected mistake.
    """

    signature: str
    example_error: str | None
    count: int = 0
    tests: set[str] = field(default_factory=set)
    frameworks: set[str] = field(default_factory=set)


def summarize_signatures(results: Sequence[TestResultDB]) -> list[SignatureGroup]:
    """Group a batch of failures by signature, largest group first.

    Used by ingestion to report "487 failures, 4 distinct problems" back to CI —
    a genuinely useful line in a pipeline log.
    """
    groups: dict[str, _GroupAccumulator] = {}
    for result in results:
        signature = result.failure_signature or "unfingerprinted"
        group = groups.setdefault(
            signature,
            _GroupAccumulator(signature=signature, example_error=result.error_message),
        )
        group.count += 1
        group.tests.add(result.test_name)
        group.frameworks.add(result.framework.value)

    return sorted(
        (
            SignatureGroup(
                signature=g.signature,
                count=g.count,
                distinct_tests=len(g.tests),
                frameworks=sorted(g.frameworks),
                example_error=g.example_error,
            )
            for g in groups.values()
        ),
        key=lambda g: g["count"],
        reverse=True,
    )
