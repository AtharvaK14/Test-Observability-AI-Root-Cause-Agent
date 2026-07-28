"""Playwright JSON reporter parser (``--reporter=json``).

Report shape, which is the reason this parser is the longest of the five::

    {
      "suites": [                       # nests recursively (describe blocks)
        {"title": "checkout.spec.ts", "file": "...", "suites": [...],
         "specs": [                     # one per `test(...)` declaration
           {"title": "completes purchase", "ok": false,
            "tests": [                  # one per project (chromium/firefox/...)
              {"projectName": "chromium", "timeout": 30000,
               "results": [              # one per RETRY ATTEMPT
                 {"status": "failed", "duration": 30123, "retry": 0,
                  "error": {...}, "stdout": [...], "attachments": [...]}
               ]}]}]}]
    }

Four levels of nesting before you reach an actual outcome, and the same logical
test appears once per browser project. Flattening it correctly is what makes
"this test has failed 8 of the last 30 runs" a true statement rather than a
number that silently mixes browsers together.
"""

from __future__ import annotations

import json
from typing import Any

from backend.ingest.base import (
    ParsedReport,
    ParseError,
    ReportParser,
    RunMetadata,
    register,
)
from backend.models.enums import TestFramework, TestStatus
from backend.models.test_result import SystemMetrics, TestResultCreate


class PlaywrightParser(ReportParser):
    framework = TestFramework.PLAYWRIGHT

    def parse(self, payload: bytes, metadata: RunMetadata) -> ParsedReport:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"not valid JSON ({exc.msg} at line {exc.lineno}). Playwright's JSON "
                "reporter is enabled with `--reporter=json` or "
                "`reporter: [['json', { outputFile: 'results.json' }]]`.",
                self.framework,
            ) from exc

        if not isinstance(data, dict):
            raise ParseError("expected a JSON object at the top level", self.framework)

        report = ParsedReport(framework=self.framework)

        suites = data.get("suites")
        if not isinstance(suites, list):
            raise ParseError(
                "no 'suites' array found — this does not look like a Playwright JSON "
                "report. If you are uploading a JUnit XML file, use the "
                "/ingest/junit endpoint instead.",
                self.framework,
            )

        for suite in suites:
            self._walk_suite(suite, [], metadata, report)

        # Top-level errors are global failures (config error, worker crash) that
        # belong to no test. Recording them as ERROR rows is what stops "the
        # whole run died" from looking like "nothing ran" on the dashboard.
        for index, error in enumerate(data.get("errors") or []):
            if isinstance(error, dict) and (error.get("message") or error.get("stack")):
                report.results.append(
                    self._global_error(error, index, metadata)
                )

        if not report.results:
            report.warnings.append("report parsed successfully but contained no tests")
        return report

    # ------------------------------------------------------------- walking

    def _walk_suite(
        self,
        suite: Any,
        ancestors: list[str],
        metadata: RunMetadata,
        report: ParsedReport,
    ) -> None:
        """Recurse through nested describe blocks, accumulating the title path."""
        if not isinstance(suite, dict):
            report.warnings.append("skipped a malformed suite entry")
            return

        title = str(suite.get("title") or "").strip()
        # The outermost suite's title is the file path, which is already carried
        # in `file`; including it in the ancestor path would duplicate it in
        # every test name.
        path = [*ancestors, title] if title and ancestors else ancestors or []
        file_path = suite.get("file") or (title if not ancestors else None)

        for spec in suite.get("specs") or []:
            self._parse_spec(spec, path, file_path, metadata, report)

        for child in suite.get("suites") or []:
            child_path = [*ancestors, title] if title else ancestors
            self._walk_suite(child, child_path, metadata, report)

    def _parse_spec(
        self,
        spec: Any,
        ancestors: list[str],
        file_path: str | None,
        metadata: RunMetadata,
        report: ParsedReport,
    ) -> None:
        if not isinstance(spec, dict):
            report.warnings.append("skipped a malformed spec entry")
            return

        spec_title = str(spec.get("title") or "unnamed test").strip()
        suite_path = " > ".join(p for p in ancestors if p) or None

        for test in spec.get("tests") or []:
            if not isinstance(test, dict):
                report.warnings.append(f"skipped a malformed test entry in {spec_title!r}")
                continue

            project = str(test.get("projectName") or "").strip()
            # The project (browser) belongs in the test's identity. A locator
            # that is flaky only in WebKit is a different problem from one that
            # is flaky everywhere, and merging the two histories hides that.
            name_parts = [p for p in (suite_path, spec_title) if p]
            test_name = " > ".join(name_parts)
            if project:
                test_name = f"{test_name} [{project}]"

            attempts: list[TestResultCreate] = []
            for index, result in enumerate(test.get("results") or []):
                parsed = self._parse_result(
                    result,
                    index=index,
                    test_name=test_name,
                    suite=suite_path,
                    file_path=file_path or spec.get("file"),
                    timeout_ms=test.get("timeout"),
                    metadata=metadata,
                    report=report,
                )
                if parsed is not None:
                    attempts.append(parsed)

            if not attempts:
                report.warnings.append(f"test {test_name!r} had no result entries")
                continue

            self.resolve_retry_statuses(attempts)
            report.results.extend(attempts)

    def _parse_result(
        self,
        result: Any,
        *,
        index: int,
        test_name: str,
        suite: str | None,
        file_path: str | None,
        timeout_ms: Any,
        metadata: RunMetadata,
        report: ParsedReport,
    ) -> TestResultCreate | None:
        if not isinstance(result, dict):
            report.warnings.append(f"skipped a malformed result for {test_name!r}")
            return None

        status = self.normalize_status(result.get("status"), framework=self.framework)
        raw_error = result.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}

        # `errors` holds every failure in the attempt; `error` only the first.
        # Soft assertions produce several, and reporting just the first hides
        # the rest of the story.
        extra_errors = [
            e.get("message")
            for e in (result.get("errors") or [])
            if isinstance(e, dict) and e.get("message")
        ]
        message = error.get("message") or (extra_errors[0] if extra_errors else None)
        if len(extra_errors) > 1:
            message = (
                f"{message}\n\n[{len(extra_errors) - 1} additional error(s) in this "
                f"attempt]\n" + "\n---\n".join(str(e) for e in extra_errors[1:])
            )

        logs = self._merge_streams(result)
        duration = self.to_millis(result.get("duration"))

        # Playwright reports timeouts as status=timedOut with no error message.
        # Synthesising one keeps the timeout visible to fingerprinting and to
        # the agent, and preserves the configured limit as evidence.
        if status == TestStatus.ERROR and not message:
            limit = self.to_millis(timeout_ms)
            message = (
                f"Test timed out after {limit}ms" if limit else "Test timed out"
            )

        attachments = self._parse_attachments(result.get("attachments"))

        return TestResultCreate(
            test_name=test_name,
            test_suite=suite,
            test_file=str(file_path) if file_path else None,
            framework=self.framework,
            status=status,
            duration_ms=duration,
            attempt=int(result.get("retry", index) or index),
            error_message=str(message) if message else None,
            stack_trace=error.get("stack") or error.get("snippet"),
            logs=logs,
            worker_id=(
                f"worker-{result['workerIndex']}"
                if result.get("workerIndex") is not None
                else None
            ),
            screenshot_url=attachments["screenshot_url"],
            video_url=attachments["video_url"],
            trace_url=attachments["trace_url"],
            started_at=self.parse_timestamp(result.get("startTime")),
            timestamp=self.parse_timestamp(result.get("startTime")),
            environment=metadata.environment,
            git_commit=metadata.git_commit,
            git_branch=metadata.git_branch,
            ci_run_id=metadata.ci_run_id,
            ci_provider=metadata.ci_provider,
            ci_job_url=metadata.ci_job_url,
            metrics=SystemMetrics(),
            raw_payload={k: v for k, v in result.items() if k != "attachments"},
        )

    # ------------------------------------------------------------- details

    def _merge_streams(self, result: dict[str, Any]) -> str | None:
        """Combine stdout and stderr, labelling which is which.

        Kept together rather than in separate columns because the interleaving
        is diagnostic: a 500 on stdout immediately before a stack trace on
        stderr is the causal chain, and splitting the two destroys the ordering
        that makes it readable.
        """
        parts = []
        if (out := self.join_output(result.get("stdout") or [])) is not None:
            parts.append(f"--- stdout ---\n{out}")
        if (err := self.join_output(result.get("stderr") or [])) is not None:
            parts.append(f"--- stderr ---\n{err}")
        return "\n\n".join(parts) if parts else None

    @staticmethod
    def _parse_attachments(attachments: Any) -> dict[str, str | None]:
        """Extract screenshot / video / trace references.

        The Playwright trace is the single most valuable artefact a human gets —
        a full timeline with DOM snapshots and network activity. Losing the
        pointer to it means the "drill into this failure" path dead-ends.
        """
        found: dict[str, str | None] = {
            "screenshot_url": None,
            "video_url": None,
            "trace_url": None,
        }
        for item in attachments or []:
            if not isinstance(item, dict):
                continue
            location = item.get("path") or item.get("url")
            if not location:
                continue
            name = str(item.get("name") or "").lower()
            content_type = str(item.get("contentType") or "").lower()
            if "trace" in name or "zip" in content_type:
                found["trace_url"] = found["trace_url"] or str(location)
            elif "video" in name or content_type.startswith("video/"):
                found["video_url"] = found["video_url"] or str(location)
            elif "screenshot" in name or content_type.startswith("image/"):
                found["screenshot_url"] = found["screenshot_url"] or str(location)
        return found

    def _global_error(
        self, error: dict[str, Any], index: int, metadata: RunMetadata
    ) -> TestResultCreate:
        """Represent a run-level Playwright error as an ERROR row."""
        return TestResultCreate(
            test_name=f"<playwright run error #{index + 1}>",
            framework=self.framework,
            status=TestStatus.ERROR,
            duration_ms=0,
            error_message=str(error.get("message") or "unspecified run-level error"),
            stack_trace=error.get("stack"),
            environment=metadata.environment,
            git_commit=metadata.git_commit,
            git_branch=metadata.git_branch,
            ci_run_id=metadata.ci_run_id,
            ci_provider=metadata.ci_provider,
            ci_job_url=metadata.ci_job_url,
            raw_payload=error,
        )


register(PlaywrightParser())
