"""Ingestion service: normalise → fingerprint → deduplicate → store → cluster.

Kept out of the route handlers so the whole pipeline is testable with a session
and a byte string — no HTTP client, no ``TestClient``, no event loop. The route
layer becomes what it should be: parse the multipart body, call this, serialise
the result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from backend.analysis.clustering import (
    ClusterService,
    compute_dedupe_key,
    compute_failure_signature,
    extract_error_type,
)
from backend.config import Settings, get_settings
from backend.db.models import TestResultDB
from backend.db.repository import TestResultRepository
from backend.ingest.base import ParsedReport
from backend.models.enums import TestStatus
from backend.models.test_result import TestResultCreate
from backend.utils import utcnow

logger = logging.getLogger(__name__)

_FAILING = frozenset({TestStatus.FAILED, TestStatus.FLAKY, TestStatus.ERROR})


@dataclass
class IngestOutcome:
    """What one ingestion actually did, in enough detail to be trusted."""

    stored: list[TestResultDB] = field(default_factory=list)
    skipped_duplicates: int = 0
    failures: list[TestResultDB] = field(default_factory=list)
    analysis_queue: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    distinct_problems: int = 0

    @property
    def stored_count(self) -> int:
        return len(self.stored)


class IngestionService:
    """Persists a parsed report, deduplicating and clustering as it goes."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.test_runs = TestResultRepository(session)
        self.clusters = ClusterService(session)

    def ingest(self, report: ParsedReport) -> IngestOutcome:
        """Store a parsed report. Runs inside the caller's transaction.

        Transaction ownership matters here: results, clusters, and occurrence
        counts must land together. If clustering committed separately and the
        insert then failed, cluster counts would reference rows that do not
        exist — and nothing would ever tell you.
        """
        outcome = IngestOutcome(warnings=list(report.warnings))
        if not report.results:
            return outcome

        prepared = [self._to_row(r) for r in report.results]
        prepared = self._drop_intra_batch_duplicates(prepared, outcome)

        keys = [row.dedupe_key for row in prepared if row.dedupe_key]
        already_stored = self.test_runs.find_existing_dedupe_keys(keys)

        fresh = [
            row
            for row in prepared
            if not (row.dedupe_key and row.dedupe_key in already_stored)
        ]
        outcome.skipped_duplicates += len(prepared) - len(fresh)

        if outcome.skipped_duplicates:
            # Not an error — CI upload steps get retried, and idempotency is the
            # point. Reported so a *systematically* duplicated pipeline is
            # visible rather than quietly halving its own apparent volume.
            logger.info(
                "skipped already-ingested results",
                extra={
                    "count": outcome.skipped_duplicates,
                    "framework": report.framework.value,
                },
            )

        if not fresh:
            return outcome

        self.test_runs.add_all(fresh)
        outcome.stored = fresh

        signatures: set[str] = set()
        for row in fresh:
            if row.status not in _FAILING:
                continue
            outcome.failures.append(row)
            cluster = self.clusters.upsert_for_result(row)
            if row.failure_signature:
                signatures.add(row.failure_signature)

            if self._should_analyze(row, cluster_is_muted=bool(cluster and cluster.is_muted)):
                outcome.analysis_queue.append(row.id)

        outcome.distinct_problems = len(signatures)

        logger.info(
            "ingested test results",
            extra={
                "framework": report.framework.value,
                "stored": len(fresh),
                "failures": len(outcome.failures),
                "distinct_problems": outcome.distinct_problems,
                "queued_for_analysis": len(outcome.analysis_queue),
            },
        )
        return outcome

    # ------------------------------------------------------------- helpers

    def _to_row(self, payload: TestResultCreate) -> TestResultDB:
        """Convert a validated wire object into a database row.

        This is where the two derived columns are computed — at write time, once
        — so that every later query can rely on them without recomputing, and so
        clustering keeps working when the Anthropic API is unavailable.
        """
        error_type = payload.error_type or extract_error_type(
            payload.error_message, payload.stack_trace, payload.framework
        )
        signature = (
            compute_failure_signature(payload.error_message, error_type, payload.stack_trace)
            if payload.status in _FAILING
            else None
        )

        return TestResultDB(
            test_name=payload.test_name,
            test_suite=payload.test_suite,
            test_file=payload.test_file,
            framework=payload.framework,
            status=payload.status,
            duration_ms=payload.duration_ms,
            attempt=payload.attempt,
            retry_count=payload.retry_count,
            error_type=error_type,
            error_message=payload.error_message,
            stack_trace=payload.stack_trace,
            logs=payload.logs,
            failure_signature=signature,
            screenshot_url=payload.screenshot_url,
            video_url=payload.video_url,
            trace_url=payload.trace_url,
            environment=payload.environment,
            git_commit=payload.git_commit,
            git_branch=payload.git_branch,
            ci_run_id=payload.ci_run_id,
            ci_provider=payload.ci_provider,
            ci_job_url=payload.ci_job_url,
            worker_id=payload.worker_id,
            cpu_percent=payload.metrics.cpu_percent,
            memory_mb=payload.metrics.memory_mb,
            disk_io_read_mb=payload.metrics.disk_io_read_mb,
            disk_io_write_mb=payload.metrics.disk_io_write_mb,
            network_latency_ms=payload.metrics.network_latency_ms,
            started_at=payload.started_at,
            # Fall back to ingest time when the reporter gave us nothing. Being
            # explicit beats a NULL that every downstream time filter would have
            # to special-case.
            timestamp=payload.timestamp or utcnow(),
            raw_payload=payload.raw_payload,
            dedupe_key=compute_dedupe_key(
                payload.ci_run_id, payload.framework, payload.test_name, payload.attempt
            ),
        )

    @staticmethod
    def _drop_intra_batch_duplicates(
        rows: list[TestResultDB], outcome: IngestOutcome
    ) -> list[TestResultDB]:
        """Remove duplicate dedupe keys *within* one upload.

        A single file can legitimately contain the same key twice — a merged
        report from sharded runners, or a parameterised test whose parameters
        did not make it into the name. Without this the batch insert dies on a
        unique-constraint violation and the entire upload is lost, which is a
        far worse outcome than dropping one row.
        """
        seen: set[str] = set()
        unique: list[TestResultDB] = []
        collisions = 0
        for row in rows:
            key = row.dedupe_key
            if key and key in seen:
                collisions += 1
                continue
            if key:
                seen.add(key)
            unique.append(row)

        if collisions:
            outcome.warnings.append(
                f"{collisions} result(s) in this file collided on "
                "(ci_run_id, framework, test_name, attempt) and were dropped. This "
                "usually means a merged multi-shard report or parameterised tests "
                "whose parameters are missing from the test name."
            )
            outcome.skipped_duplicates += collisions
        return unique

    def _should_analyze(self, row: TestResultDB, *, cluster_is_muted: bool) -> bool:
        """Decide whether a failure is worth spending an API call on.

        Three filters, each of which is a real cost control:

        * **Agent off** — the kill switch, and a missing API key.
        * **Non-final retry attempts** — analysing all three attempts of one
          flaky test triples the spend for three near-identical verdicts. The
          final attempt carries the same context and the retry history with it.
        * **Muted clusters** — a known issue someone has already acknowledged.
          Re-deriving the same verdict 200 times a day is pure waste.
        """
        if not (self.settings.agent_enabled and self.settings.auto_analyze_on_ingest):
            return False
        if row.attempt != row.retry_count:
            return False
        return not cluster_is_muted
