"""Failure context assembly — deciding what the agent gets to look at.

This module, not the prompt, is where classification accuracy is won or lost.
A model handed only an error message can do no better than a human handed only
an error message. The interesting signals are all *relational*, and none of them
appear in a test report:

* Did this test pass an hour ago on a different commit?  (pass→fail boundary)
* Did 40 other tests fail in the same pipeline run?       (blast radius)
* Did a PyTest API test fail with the same error as this
  Playwright test?                                        (cross-framework proof)
* Did it die in 200ms or burn a 30-second timeout?        (immediate vs. waiting)
* Was the runner at 98% CPU?                              (infrastructure)

Two rules govern everything here.

**Never fabricate.** The project spec's reference implementation returns
hard-coded system metrics and an invented ``changed_files`` list. That is more
dangerous than returning nothing: a model reasoning over fabricated evidence
produces a confident, well-argued, wrong answer, and the confidence score makes
it look trustworthy. Every field below is either measured or explicitly marked
unavailable.

**Budget the context.** Stack traces and CI logs run to megabytes. Everything
here is truncated deliberately, with the truncation *visible* to the model, so
it knows it is looking at an excerpt and can ask for more.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.analysis.clustering import normalize_error_text
from backend.config import Settings, get_settings
from backend.db.models import TestResultDB
from backend.db.repository import TestResultRepository
from backend.models.enums import TestStatus
from backend.utils import truncate, utcnow

logger = logging.getLogger(__name__)

# Log lines worth surfacing. For browser tests the actual cause is almost never
# in the assertion message — it is a 500 on an XHR, a CORS rejection, or an
# unhandled promise rejection sitting in the console output. Naive
# ``logs[:1000]`` truncation reliably discards exactly this.
_LOG_SIGNAL_PATTERN = re.compile(
    r"(error|exception|traceback|fail|timeout|timed out|refused|reset|unavailable"
    r"|denied|unauthor|forbidden|not found|5\d{2}\s|4\d{2}\s|panic|fatal|oom"
    r"|out of memory|econnrefused|etimedout|enotfound|cors|csp|unhandled)",
    re.IGNORECASE,
)


@dataclass
class ContextNote:
    """A retrieval-quality note attached to the bundle.

    Surfaced to the agent so it can calibrate: "no history for this test" is
    itself evidence (a brand-new test failing points at the test, not the app),
    and silently omitting it would let the model assume history was checked and
    found clean.
    """

    field_name: str
    status: str  # "ok" | "unavailable" | "truncated" | "empty"
    detail: str


@dataclass
class FailureContext:
    """Everything known about one failure, ready to hand to the agent."""

    test_result_id: str
    current_failure: dict[str, Any]
    historical_pattern: dict[str, Any]
    ci_run_correlation: dict[str, Any]
    signature_matches: dict[str, Any]
    duration_analysis: dict[str, Any]
    system_metrics: dict[str, Any]
    git_context: dict[str, Any]
    environment_factors: dict[str, Any]
    artifacts: dict[str, Any]
    notes: list[ContextNote] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_result_id": self.test_result_id,
            "current_failure": self.current_failure,
            "historical_pattern": self.historical_pattern,
            "ci_run_correlation": self.ci_run_correlation,
            "signature_matches": self.signature_matches,
            "duration_analysis": self.duration_analysis,
            "system_metrics": self.system_metrics,
            "git_context": self.git_context,
            "environment_factors": self.environment_factors,
            "artifacts": self.artifacts,
            "retrieval_notes": [
                {"field": n.field_name, "status": n.status, "detail": n.detail}
                for n in self.notes
            ],
        }


class ContextRetriever:
    """Assembles failure context from stored observability data.

    Also exposes ``tool_*`` methods. Those are the on-demand half of the design:
    the initial bundle is a *summary* (cheap, always sent), and the agent pulls
    full stack traces or filtered logs only for the failures that need them.
    Dumping every megabyte up front would be simpler, far more expensive, and
    measurably worse — burying the decisive line in 50k tokens of noise is a
    reliable way to make a model miss it.
    """

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.test_runs = TestResultRepository(session)

    # ------------------------------------------------------------------ main

    def get_failure_context(self, test_result_id: str) -> FailureContext | None:
        """Assemble the full context bundle. ``None`` if the id does not exist.

        Returns ``None`` rather than an empty dict (as the spec's version does)
        so the caller must handle the missing case explicitly — an empty dict
        flows onward and produces an analysis of nothing.
        """
        result = self.test_runs.get(test_result_id)
        if result is None:
            logger.warning(
                "context requested for unknown test result",
                extra={"test_result_id": test_result_id},
            )
            return None

        notes: list[ContextNote] = []
        history = self.test_runs.get_history(
            result.test_name, limit=self.settings.history_window_runs
        )

        return FailureContext(
            test_result_id=test_result_id,
            current_failure=self._format_current_failure(result, notes),
            historical_pattern=self._analyze_history(result, history, notes),
            ci_run_correlation=self._analyze_ci_run(result, notes),
            signature_matches=self._analyze_signature(result, notes),
            duration_analysis=self._analyze_duration(result, notes),
            system_metrics=self._collect_system_metrics(result, notes),
            git_context=self._collect_git_context(result, notes),
            environment_factors=self._collect_environment(result),
            artifacts=self._collect_artifacts(result, notes),
            notes=notes,
        )

    # -------------------------------------------------------------- sections

    def _format_current_failure(
        self, result: TestResultDB, notes: list[ContextNote]
    ) -> dict[str, Any]:
        """The failure itself, excerpted to fit a sane context budget."""
        stack = truncate(result.stack_trace, self.settings.context_max_stack_chars)
        if result.stack_trace and stack != result.stack_trace:
            notes.append(
                ContextNote(
                    "stack_trace",
                    "truncated",
                    f"{len(result.stack_trace)} chars available; call "
                    f"get_full_stack_trace for the remainder",
                )
            )

        logs_excerpt, log_note = self._excerpt_logs(result.logs)
        if log_note:
            notes.append(log_note)

        return {
            "test_name": result.test_name,
            "test_suite": result.test_suite,
            "test_file": result.test_file,
            "framework": result.framework.value,
            "status": result.status.value,
            "attempt": result.attempt,
            "retry_count": result.retry_count,
            "retried_and_still_failed": result.attempt > 0,
            "duration_ms": result.duration_ms,
            "timestamp": result.timestamp.isoformat() if result.timestamp else None,
            "error_type": result.error_type,
            "error_message": truncate(result.error_message, 2000),
            "stack_trace_excerpt": stack,
            "logs_excerpt": logs_excerpt,
            "environment": result.environment,
        }

    def _analyze_history(
        self,
        result: TestResultDB,
        history: Sequence[TestResultDB],
        notes: list[ContextNote],
    ) -> dict[str, Any]:
        """Behaviour of this test over its recent runs.

        The decisive question for classification. A test with 200 green runs that
        just went red points hard at a code change; a test that has been
        alternating red/green for three weeks points hard at the test itself.
        Identical error message, opposite verdicts — which is precisely why an
        error-message-only classifier plateaus around coin-flip on this axis.
        """
        if not history:
            notes.append(
                ContextNote(
                    "historical_pattern",
                    "empty",
                    "no prior runs recorded — this is the first execution we have seen",
                )
            )
            return {
                "available": False,
                "interpretation": (
                    "No history. Either a newly added test or a newly ingested "
                    "suite. A brand-new test that fails on its first run more "
                    "often indicates a test-authoring problem than a regression."
                ),
            }

        # ``history`` includes the current run; exclude it so "has this failed
        # before?" is not trivially answered by the failure we are analysing.
        prior = [r for r in history if r.id != result.id]
        total = len(prior)

        statuses = [r.status for r in prior]
        passed = sum(1 for s in statuses if s == TestStatus.PASSED)
        failed = sum(1 for s in statuses if s == TestStatus.FAILED)
        flaky = sum(1 for s in statuses if s == TestStatus.FLAKY)
        errored = sum(1 for s in statuses if s == TestStatus.ERROR)

        week_ago = utcnow() - timedelta(days=7)
        # No `and r.timestamp` guard: the column is non-nullable, so the check
        # was dead code that also confused the sum() overload resolution.
        failures_7d = sum(
            1
            for r in prior
            if r.status in (TestStatus.FAILED, TestStatus.ERROR) and r.timestamp >= week_ago
        )

        last_pass = self.test_runs.get_last_pass_before(
            result.test_name, result.timestamp or utcnow()
        )

        return {
            "available": True,
            "window_runs": total,
            "passed": passed,
            "failed": failed,
            "flaky": flaky,
            "errored": errored,
            "pass_rate_pct": round(passed / total * 100, 1) if total else 0.0,
            "failures_last_7_days": failures_7d,
            "flakiness_score": self._flakiness_score(statuses),
            "consecutive_failures_before_this": self._consecutive_failures(statuses),
            "last_known_pass": {
                "timestamp": last_pass.timestamp.isoformat()
                if last_pass and last_pass.timestamp
                else None,
                "git_commit": last_pass.git_commit if last_pass else None,
                "ci_run_id": last_pass.ci_run_id if last_pass else None,
            }
            if last_pass
            else None,
            "recent_sequence": [
                {
                    "status": r.status.value,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "duration_ms": r.duration_ms,
                    "git_commit": r.git_commit,
                }
                for r in prior[:10]
            ],
            "distinct_error_messages": self._distinct_errors(prior),
        }

    def _analyze_ci_run(self, result: TestResultDB, notes: list[ContextNote]) -> dict[str, Any]:
        """What else failed in the same pipeline execution.

        The blast radius, and the fastest disambiguator the system has:

        - one test red, rest green            -> that test or its feature
        - a whole suite red together          -> shared setup / auth / seed data
        - failures across *different frameworks* -> the app or the environment,
          because independent test code cannot break simultaneously by chance
        """
        siblings = self.test_runs.get_ci_run_siblings(result.ci_run_id, exclude_id=result.id)
        if not siblings:
            return {
                "other_failures_in_run": 0,
                "interpretation": (
                    "This was the only failure in its CI run. Isolated failures "
                    "point at the individual test or the specific feature it "
                    "covers, not at shared infrastructure."
                ),
            }

        frameworks = {s.framework.value for s in siblings} | {result.framework.value}
        suites = {s.test_suite for s in siblings if s.test_suite}
        shared_signature = sum(
            1
            for s in siblings
            if result.failure_signature and s.failure_signature == result.failure_signature
        )

        if len(frameworks) > 1:
            interpretation = (
                f"Failures span {len(frameworks)} frameworks ({', '.join(sorted(frameworks))}) "
                "in the same run. Independently written test code failing together "
                "is strong evidence of an application or environment fault rather "
                "than a test defect."
            )
        elif len(siblings) >= 10:
            interpretation = (
                f"{len(siblings)} other tests failed in this run — a broad outage "
                "pattern. Look for shared setup: authentication, seed data, a "
                "service that did not come up, or a bad deploy."
            )
        elif len(suites) == 1:
            interpretation = (
                f"Failures are confined to the '{next(iter(suites))}' suite, which "
                "points at that suite's shared fixtures or the feature it covers."
            )
        else:
            interpretation = (
                f"{len(siblings)} other tests failed in this run. Check whether "
                "they share a root cause with this one."
            )

        return {
            "ci_run_id": result.ci_run_id,
            "other_failures_in_run": len(siblings),
            "frameworks_affected": sorted(frameworks),
            "cross_framework": len(frameworks) > 1,
            "suites_affected": sorted(s for s in suites),
            "sharing_identical_signature": shared_signature,
            "interpretation": interpretation,
            "sample": [
                {
                    "test_name": s.test_name,
                    "framework": s.framework.value,
                    "error_type": s.error_type,
                    "error_message": truncate(s.error_message, 200),
                    "same_signature": s.failure_signature == result.failure_signature,
                }
                for s in siblings[:10]
            ],
        }

    def _analyze_signature(
        self, result: TestResultDB, notes: list[ContextNote]
    ) -> dict[str, Any]:
        """History of this exact failure fingerprint across all tests."""
        if not result.failure_signature:
            notes.append(
                ContextNote(
                    "signature_matches",
                    "unavailable",
                    "no failure signature — the failure carried no usable error text",
                )
            )
            return {"available": False, "reason": "no usable error text to fingerprint"}

        since = utcnow() - timedelta(days=14)
        matches = self.test_runs.get_by_signature(
            result.failure_signature, since=since, exclude_id=result.id, limit=20
        )
        distinct_tests = self.test_runs.get_distinct_tests_for_signature(
            result.failure_signature
        )
        total = self.test_runs.count_by_signature(result.failure_signature, since=since)

        return {
            "available": True,
            "signature": result.failure_signature[:16],
            "occurrences_14d": total,
            "distinct_tests_affected": len(distinct_tests),
            "affected_test_sample": list(distinct_tests[:10]),
            "normalized_error": normalize_error_text(result.error_message, max_length=300),
            "interpretation": (
                f"This exact error shape has occurred {total} times in 14 days across "
                f"{len(distinct_tests)} distinct test(s). "
                + (
                    "Multiple unrelated tests sharing one error signature indicates a "
                    "shared cause — infrastructure, environment, or the application."
                    if len(distinct_tests) > 1
                    else "Confined to a single test, which is consistent with a defect "
                    "in that test or in the specific feature it exercises."
                )
            ),
            "recent_occurrences": [
                {
                    "test_name": m.test_name,
                    "framework": m.framework.value,
                    "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                    "environment": m.environment,
                }
                for m in matches[:10]
            ],
        }

    def _analyze_duration(
        self, result: TestResultDB, notes: list[ContextNote]
    ) -> dict[str, Any]:
        """Compare this run's duration against the test's passing baseline.

        Separates two failures that share an error message but not a cause:

        - **Died fast** (well under baseline): hit an immediate error. The
          element genuinely was not there; the service refused the connection.
        - **Burned a timeout** (at or above the runner's limit): it was
          *waiting*. Race condition, missing wait, slow dependency, deadlock.

        Same "element not found" message, different owners and different fixes.
        """
        stats = self.test_runs.get_duration_stats(result.test_name)
        baseline = stats.get("avg_duration_ms")

        if not baseline or not stats.get("sample_size"):
            notes.append(
                ContextNote(
                    "duration_analysis", "empty", "no passing runs to establish a baseline"
                )
            )
            return {
                "available": False,
                "current_duration_ms": result.duration_ms,
                "reason": "no successful runs of this test to compare against",
            }

        baseline_f = float(baseline)
        ratio = result.duration_ms / baseline_f if baseline_f else None

        if ratio is None:
            interpretation = "Baseline unavailable."
        elif ratio >= 3.0:
            interpretation = (
                f"Ran {ratio:.1f}x longer than its passing baseline. The test was "
                "waiting rather than failing outright — consistent with a timeout, "
                "a race condition, a missing explicit wait, or a slow dependency."
            )
        elif ratio <= 0.4:
            interpretation = (
                f"Ran {ratio:.1f}x its baseline — it failed almost immediately. "
                "Consistent with an element or endpoint that was genuinely absent, "
                "a connection refused, or a setup/fixture error before the test body."
            )
        else:
            interpretation = (
                f"Duration was normal ({ratio:.1f}x baseline), so the test reached "
                "its assertion and the assertion disagreed. That points at a "
                "behavioural difference rather than a timing problem."
            )

        return {
            "available": True,
            "current_duration_ms": result.duration_ms,
            "baseline_avg_ms": round(baseline_f, 1),
            "baseline_min_ms": stats.get("min_duration_ms"),
            "baseline_max_ms": stats.get("max_duration_ms"),
            "baseline_sample_size": stats.get("sample_size"),
            "ratio_to_baseline": round(ratio, 2) if ratio else None,
            "interpretation": interpretation,
        }

    def _collect_system_metrics(
        self, result: TestResultDB, notes: list[ContextNote]
    ) -> dict[str, Any]:
        """Host metrics recorded at execution time — measured, never invented.

        When absent we say so. The alternative (the spec's hard-coded
        ``cpu_percent: 65.2``) invites the model to rule out resource contention
        on the strength of a number nobody measured.
        """
        metrics = {
            "cpu_percent": result.cpu_percent,
            "memory_mb": result.memory_mb,
            "disk_io_read_mb": result.disk_io_read_mb,
            "disk_io_write_mb": result.disk_io_write_mb,
            "network_latency_ms": result.network_latency_ms,
        }
        present = {k: v for k, v in metrics.items() if v is not None}

        if not present:
            notes.append(
                ContextNote(
                    "system_metrics",
                    "unavailable",
                    "no host metrics submitted with this result",
                )
            )
            return {
                "available": False,
                "note": (
                    "No system metrics were captured for this run. Do not assume "
                    "resource contention was or was not a factor — it is unmeasured. "
                    "Have the CI runner post cpu_percent/memory_mb with results to "
                    "make this dimension available."
                ),
            }

        flags: list[str] = []
        if result.cpu_percent is not None and result.cpu_percent > 90:
            flags.append(f"CPU at {result.cpu_percent:.0f}% — likely resource contention")
        if result.memory_mb is not None and result.memory_mb < 256:
            flags.append(f"only {result.memory_mb:.0f}MB memory free — OOM risk")
        if result.network_latency_ms is not None and result.network_latency_ms > 500:
            flags.append(f"network latency {result.network_latency_ms:.0f}ms — degraded network")

        return {
            "available": True,
            "worker_id": result.worker_id,
            **present,
            "anomaly_flags": flags,
        }

    def _collect_git_context(
        self, result: TestResultDB, notes: list[ContextNote]
    ) -> dict[str, Any]:
        """Code-change context around the failure.

        The valuable part is derived from our own data and always available: the
        last commit on which this test passed, paired with the commit it failed
        on, bounds the suspect range. That alone routinely cuts investigation
        from "read the whole diff" to "read these four commits".

        Enrichment from an actual checkout is best-effort and optional. When no
        repo is configured we report unavailable rather than inventing a
        ``changed_files`` list.
        """
        last_pass = self.test_runs.get_last_pass_before(
            result.test_name, result.timestamp or utcnow()
        )

        context: dict[str, Any] = {
            "failing_commit": result.git_commit,
            "branch": result.git_branch,
            "last_passing_commit": last_pass.git_commit if last_pass else None,
            "suspect_range": None,
            "repo_inspected": False,
        }

        if last_pass and last_pass.git_commit and result.git_commit:
            if last_pass.git_commit != result.git_commit:
                context["suspect_range"] = f"{last_pass.git_commit}..{result.git_commit}"
                context["interpretation"] = (
                    "The test passed on the earlier commit and failed on the later "
                    "one, so any code cause lies within this range."
                )
            else:
                context["interpretation"] = (
                    "The test passed and then failed on the *same commit*. The code "
                    "did not change between those runs, so a code regression is "
                    "effectively ruled out — this is non-determinism, environment, "
                    "or test data."
                )

        if not self.settings.git_repo_path:
            notes.append(
                ContextNote(
                    "git_context",
                    "unavailable",
                    "GIT_REPO_PATH not configured — no changed-file data available",
                )
            )
            context["note"] = (
                "No repository checkout is configured, so changed files are unknown. "
                "Treat file-level attribution as unavailable rather than empty."
            )
            return context

        enriched = self._inspect_local_repo(result.git_commit)
        context.update(enriched)
        return context

    def _inspect_local_repo(self, commit: str | None) -> dict[str, Any]:
        """Best-effort ``git show`` against a configured checkout.

        Every failure mode here is non-fatal: a missing repo, a commit that was
        never fetched, or a slow filesystem must degrade context, never break
        analysis. Hence the broad catch and the explicit timeout.
        """
        if not commit:
            return {"repo_inspected": False, "note": "no commit SHA recorded on this run"}

        repo_path = Path(self.settings.git_repo_path or "")
        if not (repo_path / ".git").exists():
            logger.warning(
                "configured git_repo_path is not a repository",
                extra={"repo_path": str(repo_path)},
            )
            return {
                "repo_inspected": False,
                "note": f"configured git_repo_path '{repo_path}' is not a git repository",
            }

        try:
            proc = subprocess.run(
                ["git", "show", "--stat", "--format=%an%n%ad%n%s", "--no-color", commit],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode != 0:
                return {
                    "repo_inspected": False,
                    "note": f"commit {commit[:8]} not found in local checkout",
                }

            lines = proc.stdout.splitlines()
            changed = [
                line.strip().split("|")[0].strip()
                for line in lines
                if "|" in line and ("+" in line or "-" in line)
            ]
            return {
                "repo_inspected": True,
                "author": lines[0] if lines else None,
                "commit_date": lines[1] if len(lines) > 1 else None,
                "subject": lines[2] if len(lines) > 2 else None,
                "changed_files": changed[:40],
                "changed_file_count": len(changed),
            }
        except subprocess.TimeoutExpired:
            logger.warning("git inspection timed out", extra={"commit": commit})
            return {"repo_inspected": False, "note": "git inspection timed out"}
        except OSError as exc:
            logger.warning("git inspection failed", extra={"commit": commit, "error": str(exc)})
            return {"repo_inspected": False, "note": f"git inspection failed: {exc}"}

    def _collect_environment(self, result: TestResultDB) -> dict[str, Any]:
        return {
            "environment": result.environment,
            "branch": result.git_branch,
            "ci_run_id": result.ci_run_id,
            "ci_provider": result.ci_provider,
            "ci_job_url": result.ci_job_url,
            "worker_id": result.worker_id,
        }

    def _collect_artifacts(
        self, result: TestResultDB, notes: list[ContextNote]
    ) -> dict[str, Any]:
        """Artefact references.

        URLs only — the agent is text-only in this design and cannot open a
        screenshot. They are included because the *human* reading the analysis
        needs them, and because "a screenshot exists" is itself worth telling the
        agent so it can point a reviewer at it.
        """
        available = {
            "screenshot_url": result.screenshot_url,
            "video_url": result.video_url,
            "trace_url": result.trace_url,
        }
        present = {k: v for k, v in available.items() if v}
        if not present:
            notes.append(ContextNote("artifacts", "empty", "no artefacts attached to this run"))
        return {
            "available": bool(present),
            **present,
            "note": (
                "Artefact URLs are provided for the human reviewer; they have not "
                "been inspected as part of this analysis."
                if present
                else "No screenshots, videos, or traces were captured."
            ),
        }

    # ----------------------------------------------------------- agent tools
    # Exposed to Claude as callable tools. Kept as plain methods with plain
    # return types so they are unit-testable without an API key.

    def tool_get_full_stack_trace(self, test_result_id: str) -> dict[str, Any]:
        """Full stack trace for a test result (still bounded, but far larger)."""
        result = self.test_runs.get(test_result_id)
        if result is None:
            return {"error": f"test result {test_result_id} not found"}
        if not result.stack_trace:
            return {"available": False, "reason": "no stack trace was captured"}
        return {
            "available": True,
            "test_name": result.test_name,
            "stack_trace": truncate(result.stack_trace, 20000),
        }

    def tool_search_logs(
        self, test_result_id: str, pattern: str | None = None, max_lines: int = 80
    ) -> dict[str, Any]:
        """Search a run's captured logs.

        A grep, not a dump. Letting the agent ask "show me lines matching
        ``ECONNREFUSED``" is dramatically more token-efficient than shipping a
        10MB log, and it is how a human would actually work the problem.
        """
        result = self.test_runs.get(test_result_id)
        if result is None:
            return {"error": f"test result {test_result_id} not found"}
        if not result.logs:
            return {"available": False, "reason": "no logs were captured for this run"}

        lines = result.logs.splitlines()
        if pattern:
            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                # Return the error to the agent rather than raising: an invalid
                # regex is a recoverable mistake it can correct on the next turn.
                return {"error": f"invalid regex {pattern!r}: {exc}"}
            matched = [line for line in lines if regex.search(line)]
        else:
            matched = [line for line in lines if _LOG_SIGNAL_PATTERN.search(line)]

        return {
            "available": True,
            "total_log_lines": len(lines),
            "matched_lines": len(matched),
            "pattern": pattern or "<default: error/warning signals>",
            "lines": matched[:max_lines],
            "truncated": len(matched) > max_lines,
        }

    def tool_get_test_history(self, test_name: str, limit: int = 30) -> dict[str, Any]:
        """Raw execution history for a test — including tests other than the one
        under analysis, so the agent can check a suspected sibling."""
        history = self.test_runs.get_history(test_name, limit=min(limit, 100))
        if not history:
            return {"available": False, "reason": f"no runs recorded for {test_name!r}"}
        return {
            "available": True,
            "test_name": test_name,
            "runs": [
                {
                    "status": r.status.value,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "duration_ms": r.duration_ms,
                    "git_commit": r.git_commit,
                    "environment": r.environment,
                    "error_message": truncate(r.error_message, 200),
                }
                for r in history
            ],
        }

    def tool_get_ci_run_summary(self, ci_run_id: str) -> dict[str, Any]:
        """Every failure in a pipeline run, grouped by signature."""
        siblings = self.test_runs.get_ci_run_siblings(ci_run_id, limit=200)
        if not siblings:
            return {"available": False, "reason": f"no failures recorded for run {ci_run_id!r}"}

        by_signature: dict[str, list[TestResultDB]] = {}
        for sibling in siblings:
            by_signature.setdefault(sibling.failure_signature or "unfingerprinted", []).append(
                sibling
            )

        return {
            "available": True,
            "ci_run_id": ci_run_id,
            "total_failures": len(siblings),
            "distinct_problems": len(by_signature),
            "groups": [
                {
                    "signature": sig[:16],
                    "count": len(group),
                    "frameworks": sorted({g.framework.value for g in group}),
                    "example_error": truncate(group[0].error_message, 300),
                    "tests": [g.test_name for g in group[:5]],
                }
                for sig, group in sorted(
                    by_signature.items(), key=lambda kv: len(kv[1]), reverse=True
                )[:10]
            ],
        }

    def tool_find_similar_failures(
        self, failure_signature: str, days: int = 14
    ) -> dict[str, Any]:
        """Everywhere else this exact failure shape has appeared."""
        since = utcnow() - timedelta(days=days)
        matches = self.test_runs.get_by_signature(failure_signature, since=since, limit=50)
        if not matches:
            return {"available": False, "reason": "no other occurrences of this signature"}
        return {
            "available": True,
            "occurrences": len(matches),
            "distinct_tests": len({m.test_name for m in matches}),
            "distinct_environments": sorted({m.environment for m in matches}),
            "distinct_frameworks": sorted({m.framework.value for m in matches}),
            "first_seen": min(m.timestamp for m in matches if m.timestamp).isoformat(),
            "last_seen": max(m.timestamp for m in matches if m.timestamp).isoformat(),
            "sample": [
                {
                    "test_name": m.test_name,
                    "framework": m.framework.value,
                    "environment": m.environment,
                    "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                }
                for m in matches[:15]
            ],
        }

    # --------------------------------------------------------------- helpers

    def _excerpt_logs(self, logs: str | None) -> tuple[str | None, ContextNote | None]:
        """Excerpt logs by *relevance*, not by position.

        ``logs[:1000]`` keeps the framework banner and the first few passing
        assertions, and discards the stack trace at the end — the opposite of
        what is wanted. This keeps signal-bearing lines plus a little context,
        and falls back to the tail (where errors usually land) when nothing
        matches.
        """
        if not logs:
            return None, ContextNote("logs", "empty", "no logs captured for this run")

        lines = logs.splitlines()
        limit = self.settings.context_max_log_chars

        if len(logs) <= limit:
            return logs, None

        signal_indices = [i for i, line in enumerate(lines) if _LOG_SIGNAL_PATTERN.search(line)]

        if not signal_indices:
            excerpt = "\n".join(lines[-60:])
            return (
                f"[showing last 60 of {len(lines)} log lines — no error-like lines matched]\n"
                + (truncate(excerpt, limit) or ""),
                ContextNote(
                    "logs",
                    "truncated",
                    f"{len(lines)} lines, none matched error patterns; showing tail",
                ),
            )

        # Keep one line of context on either side of each hit, then merge.
        keep: set[int] = set()
        for index in signal_indices:
            keep.update(range(max(0, index - 1), min(len(lines), index + 2)))

        selected: list[str] = []
        previous = -2
        for index in sorted(keep):
            if index != previous + 1:
                selected.append(f"... [{index - previous - 1} lines omitted] ...")
            selected.append(lines[index])
            previous = index

        excerpt = "\n".join(selected)
        return (
            f"[{len(signal_indices)} error-relevant lines selected from {len(lines)} total]\n"
            + (truncate(excerpt, limit) or ""),
            ContextNote(
                "logs",
                "truncated",
                f"{len(lines)} lines filtered to {len(signal_indices)} relevant; "
                "call search_logs for more",
            ),
        )

    @staticmethod
    def _flakiness_score(statuses: Sequence[TestStatus]) -> float:
        """Flakiness as *instability*, not as failure rate.

        Measured as the share of adjacent run pairs that changed outcome. This
        distinguishes the two things a raw failure rate conflates:

        - alternates pass/fail/pass/fail  -> score ~1.0, genuinely flaky
        - fails every single run          -> score 0.0, consistently **broken**

        Both need attention, but they need different attention, and calling a
        reliably-broken test "50% flaky" sends people looking for a race
        condition that does not exist.
        """
        if len(statuses) < 2:
            return 0.0
        passes = [s == TestStatus.PASSED for s in statuses]
        transitions = sum(1 for a, b in pairwise(passes) if a != b)
        return round(transitions / (len(passes) - 1), 3)

    @staticmethod
    def _consecutive_failures(statuses: Sequence[TestStatus]) -> int:
        """How many runs in a row failed immediately before this one.

        A long streak means "broken and staying broken" — nobody is looking at
        it, and it is not a race.
        """
        streak = 0
        for status in statuses:
            if status in (TestStatus.FAILED, TestStatus.ERROR):
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def _distinct_errors(history: Sequence[TestResultDB], limit: int = 5) -> list[dict[str, Any]]:
        """Distinct normalised errors this test has produced recently.

        One recurring error means one persistent problem. Several different
        errors from the same test usually means the test is fragile in general,
        or that its environment keeps shifting underneath it.
        """
        counts: dict[str, dict[str, Any]] = {}
        for run in history:
            if not run.error_message:
                continue
            key = normalize_error_text(run.error_message, max_length=200)
            if not key:
                continue
            entry = counts.setdefault(
                key, {"normalized": key, "count": 0, "example": truncate(run.error_message, 200)}
            )
            entry["count"] += 1
        return sorted(counts.values(), key=lambda e: e["count"], reverse=True)[:limit]
