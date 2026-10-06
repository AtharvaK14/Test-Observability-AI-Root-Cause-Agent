"""Ingestion endpoints — where CI pipelines POST their test results.

Design decisions worth stating:

**Handlers are ``def``, not ``async def``.** They do blocking work (file reads,
psycopg queries), so Starlette runs them in a worker threadpool. Declaring them
``async`` would block the event loop for the duration of every database call and
stall every other in-flight request — the most common FastAPI performance bug,
and one the project spec's sample code contains.

**One route per framework, one shared implementation.** The routes differ only
in which parser they select. Separate paths (rather than a ``?framework=``
parameter) keep the CI-side curl self-documenting and make the OpenAPI page
useful, while the work stays in one function.

**Responses report what was skipped.** A pipeline that uploads 400 results and
is told ``{"status": "success"}`` while 380 were dropped is worse than no
observability, because it manufactures confidence in a wrong number.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile
from fastapi import status as http_status

from backend.analysis.dispatch import dispatch_analyses
from backend.api.deps import SessionDep, SettingsDep
from backend.ingest.base import ParsedReport, ParseError, RunMetadata, parse_report
from backend.ingest.junit import parse_junit
from backend.ingest.service import IngestionService
from backend.models.enums import TestFramework
from backend.models.test_result import IngestResponse, TestResultCreate
from backend.utils import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])


# --- Shared form parameters -------------------------------------------------
# These are not in the report file — a test reporter has no idea what commit it
# is running against. They come from the CI environment, and without them
# history is unqueryable and the pass→fail commit boundary cannot be computed.

CiRunId = Annotated[
    str,
    Form(description="Groups everything from one pipeline execution (e.g. $GITHUB_RUN_ID)."),
]
Environment = Annotated[str, Form(description="staging | prod | local | ...")]
GitCommit = Annotated[str | None, Form(description="Commit SHA under test.")]
GitBranch = Annotated[str | None, Form(description="Branch under test.")]
CiProvider = Annotated[str | None, Form(description="github-actions | gitlab-ci | jenkins")]
CiJobUrl = Annotated[str | None, Form(description="Deep link back to the CI job.")]


def _metadata(
    ci_run_id: str,
    environment: str,
    git_commit: str | None,
    git_branch: str | None,
    ci_provider: str | None,
    ci_job_url: str | None,
) -> RunMetadata:
    return RunMetadata(
        ci_run_id=ci_run_id or "local",
        environment=environment or "staging",
        git_commit=git_commit or None,
        git_branch=git_branch or None,
        ci_provider=ci_provider or None,
        ci_job_url=ci_job_url or None,
    )


def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read an uploaded file, refusing oversized payloads.

    Checked before parsing rather than after: a 500MB Playwright report with
    embedded traces would otherwise be fully buffered and JSON-parsed before
    anyone noticed, and that is a trivial way to take the service down.
    """
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(
            status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"file is {file.size} bytes, limit is {max_bytes}. Upload the JSON "
                "report without embedded attachments, or raise MAX_INGEST_BYTES."
            ),
        )
    payload = file.file.read()
    if len(payload) > max_bytes:
        raise HTTPException(
            status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file is {len(payload)} bytes, limit is {max_bytes}.",
        )
    if not payload.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="uploaded file is empty",
        )
    return payload


def _store(
    report: ParsedReport,
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    metadata: RunMetadata,
) -> IngestResponse:
    """Persist a parsed report and queue analysis for its failures."""
    outcome = IngestionService(session, settings).ingest(report)

    # Commit before handing IDs to anything that opens its own connection.
    # get_db's commit runs in dependency teardown, which FastAPI executes AFTER
    # background tasks — so without this, analysis looks up rows that are not
    # yet visible to it and logs "analysis target missing" for every failure.
    # (In-memory SQLite masks this: StaticPool shares one connection.)
    session.commit()

    # Redis queue when REDIS_URL is set (durable, consumed by backend.worker),
    # in-process background tasks otherwise. Either way the CI step gets its
    # response in milliseconds instead of waiting on N sequential analyses.
    job_ids = dispatch_analyses(outcome.analysis_queue, background, settings)

    if outcome.distinct_problems and outcome.failures:
        logger.info(
            "failure summary",
            extra={
                "failures": len(outcome.failures),
                "distinct_problems": outcome.distinct_problems,
            },
        )

    return IngestResponse(
        status="success" if not outcome.warnings else "partial",
        framework=report.framework,
        ingested=outcome.stored_count,
        skipped_duplicates=outcome.skipped_duplicates,
        failures_detected=len(outcome.failures),
        analyses_queued=len(outcome.analysis_queue),
        analysis_job_ids=job_ids,
        errors=outcome.warnings,
        test_result_ids=[row.id for row in outcome.stored],
        ci_run_id=metadata.ci_run_id,
        received_at=utcnow(),
    )


def _ingest_file(
    framework: TestFramework,
    file: UploadFile,
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    metadata: RunMetadata,
) -> IngestResponse:
    payload = _read_upload(file, settings.max_ingest_bytes)
    try:
        report = parse_report(framework, payload, metadata)
    except ParseError as exc:
        # 400 with the parser's own message, which names the reporter flag that
        # produces the expected format. "Invalid JSON" alone leaves the pipeline
        # owner with nothing to act on.
        # Keys in `extra` must not collide with LogRecord's own attributes —
        # `filename`, `module`, `name`, `process`, etc. raise KeyError at log
        # time, turning a handled 400 into an unhandled 500. Hence `upload_name`.
        logger.warning(
            "report parse failed",
            extra={"framework": framework.value, "upload_name": file.filename},
        )
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return _store(report, session, settings, background, metadata)


# --- Endpoints --------------------------------------------------------------


@router.post(
    "/playwright",
    response_model=IngestResponse,
    summary="Ingest a Playwright JSON report",
)
def ingest_playwright(
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="Output of `playwright test --reporter=json`")],
    ci_run_id: CiRunId = "local",
    environment: Environment = "staging",
    git_commit: GitCommit = None,
    git_branch: GitBranch = None,
    ci_provider: CiProvider = None,
    ci_job_url: CiJobUrl = None,
) -> IngestResponse:
    """Ingest Playwright results.

    Handles the nested ``suites → specs → tests → results`` structure, keeps one
    row per retry attempt, splits results per browser project, and extracts
    screenshot/video/trace attachment paths.
    """
    return _ingest_file(
        TestFramework.PLAYWRIGHT,
        file,
        session,
        settings,
        background,
        _metadata(ci_run_id, environment, git_commit, git_branch, ci_provider, ci_job_url),
    )


@router.post("/cypress", response_model=IngestResponse, summary="Ingest a Cypress JSON report")
def ingest_cypress(
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="Module API, mochawesome, or mocha JSON")],
    ci_run_id: CiRunId = "local",
    environment: Environment = "staging",
    git_commit: GitCommit = None,
    git_branch: GitBranch = None,
    ci_provider: CiProvider = None,
    ci_job_url: CiJobUrl = None,
) -> IngestResponse:
    """Ingest Cypress results.

    Auto-detects which of the three Cypress JSON shapes was uploaded (module
    API, mochawesome, or plain mocha) rather than requiring the caller to know.
    """
    return _ingest_file(
        TestFramework.CYPRESS,
        file,
        session,
        settings,
        background,
        _metadata(ci_run_id, environment, git_commit, git_branch, ci_provider, ci_job_url),
    )


@router.post("/pytest", response_model=IngestResponse, summary="Ingest a pytest-json-report file")
def ingest_pytest(
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="Output of `pytest --json-report`")],
    ci_run_id: CiRunId = "local",
    environment: Environment = "staging",
    git_commit: GitCommit = None,
    git_branch: GitBranch = None,
    ci_provider: CiProvider = None,
    ci_job_url: CiJobUrl = None,
) -> IngestResponse:
    """Ingest PyTest results.

    Preserves the setup/call/teardown distinction — a fixture failure is a
    different problem from an assertion failure — and captures pytest's full
    ``longrepr`` traceback rather than only the one-line crash message.
    """
    return _ingest_file(
        TestFramework.PYTEST,
        file,
        session,
        settings,
        background,
        _metadata(ci_run_id, environment, git_commit, git_branch, ci_provider, ci_job_url),
    )


@router.post("/selenium", response_model=IngestResponse, summary="Ingest Selenium results")
def ingest_selenium(
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="JUnit/TestNG XML, or normalised JSON")],
    ci_run_id: CiRunId = "local",
    environment: Environment = "staging",
    git_commit: GitCommit = None,
    git_branch: GitBranch = None,
    ci_provider: CiProvider = None,
    ci_job_url: CiJobUrl = None,
) -> IngestResponse:
    """Ingest Selenium results (XML or JSON — the format is sniffed)."""
    return _ingest_file(
        TestFramework.SELENIUM,
        file,
        session,
        settings,
        background,
        _metadata(ci_run_id, environment, git_commit, git_branch, ci_provider, ci_job_url),
    )


@router.post("/junit", response_model=IngestResponse, summary="Ingest any JUnit XML report")
def ingest_junit(
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="Any JUnit-format XML report")],
    framework: Annotated[
        TestFramework,
        Query(
            description=(
                "Which framework produced this file — drives cross-framework "
                "correlation."
            )
        ),
    ] = TestFramework.SELENIUM,
    ci_run_id: CiRunId = "local",
    environment: Environment = "staging",
    git_commit: GitCommit = None,
    git_branch: GitBranch = None,
    ci_provider: CiProvider = None,
    ci_job_url: CiJobUrl = None,
) -> IngestResponse:
    """Ingest a JUnit XML report from any framework.

    The universal fallback when native JSON is unavailable. ``framework`` is a
    required piece of judgement rather than a guess: mislabelling it breaks the
    cross-framework correlation that makes "these failed together in unrelated
    suites" a usable signal.

    Lossier than the native parsers — JUnit XML carries no retry attempts and no
    artefact links — so prefer the framework-specific endpoint when you can.
    """
    metadata = _metadata(
        ci_run_id, environment, git_commit, git_branch, ci_provider, ci_job_url
    )
    payload = _read_upload(file, settings.max_ingest_bytes)
    try:
        report = parse_junit(payload, metadata, framework=framework)
    except ParseError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _store(report, session, settings, background, metadata)


@router.post("/results", response_model=IngestResponse, summary="Ingest pre-normalised results")
def ingest_normalized(
    session: SessionDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    results: list[TestResultCreate],
) -> IngestResponse:
    """Ingest results already in this system's schema, as a JSON body.

    The escape hatch for a framework with no parser here, or for a custom
    reporter. Every field is validated by Pydantic, so the same guarantees hold
    as for a parsed upload — this bypasses parsing, not validation.
    """
    if not results:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail="no results supplied"
        )

    frameworks = {r.framework for r in results}
    report = ParsedReport(framework=next(iter(frameworks)), results=list(results))
    if len(frameworks) > 1:
        # Allowed — a single CI run legitimately spans frameworks — but the
        # response can only name one, so say which and why.
        report.warnings.append(
            f"batch spans {len(frameworks)} frameworks "
            f"({', '.join(sorted(f.value for f in frameworks))}); each result keeps "
            "its own framework, the response reports the first."
        )

    metadata = RunMetadata(ci_run_id=results[0].ci_run_id, environment=results[0].environment)
    return _store(report, session, settings, background, metadata)
