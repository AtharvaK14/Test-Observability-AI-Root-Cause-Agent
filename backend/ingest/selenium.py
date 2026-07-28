"""Selenium result parser.

Selenium is a *driver*, not a runner — there is no "Selenium report format".
What a Selenium suite emits depends on the harness wrapped around it: JUnit or
TestNG (XML), pytest (JSON), or a hand-rolled reporter. So this parser sniffs:

* XML  -> delegate to the JUnit parser, attributed to Selenium.
* JSON -> a generic normalised shape, documented below.

The generic shape exists because Selenium Grid users frequently do have custom
reporters, and telling them "convert to JUnit XML first" adds a lossy step for
no reason::

    {"results": [{"name": "...", "status": "failed", "duration_ms": 1234,
                  "error": {"message": "...", "stack": "...", "type": "..."},
                  "logs": "...", "screenshot": "https://...",
                  "browser": "chrome", "node": "grid-node-3"}]}

Every field except ``name`` and ``status`` is optional.
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
from backend.ingest.junit import parse_junit
from backend.models.enums import TestFramework
from backend.models.test_result import SystemMetrics, TestResultCreate


class SeleniumParser(ReportParser):
    framework = TestFramework.SELENIUM

    def parse(self, payload: bytes, metadata: RunMetadata) -> ParsedReport:
        stripped = payload.lstrip()
        if stripped.startswith(b"<"):
            return parse_junit(payload, metadata, framework=self.framework)

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"not valid JSON or XML ({exc.msg} at line {exc.lineno}). Selenium "
                "suites usually report through JUnit/TestNG XML or pytest JSON — "
                "upload those to /ingest/junit or /ingest/pytest. For a custom "
                "reporter, emit {'results': [{'name', 'status', ...}]}.",
                self.framework,
            ) from exc

        entries = self._locate_entries(data)
        if entries is None:
            raise ParseError(
                "unrecognised shape: expected a JSON array of results, or an object "
                f"with a 'results'/'tests' array. Found {type(data).__name__} with "
                f"keys {sorted(data)[:8] if isinstance(data, dict) else 'n/a'}.",
                self.framework,
            )

        report = ParsedReport(framework=self.framework)
        for entry in entries:
            if not isinstance(entry, dict):
                report.warnings.append("skipped a non-object result entry")
                continue
            result = self._build(entry, metadata, report)
            if result is not None:
                report.results.append(result)

        if not report.results:
            report.warnings.append("report parsed successfully but contained no tests")
        return report

    @staticmethod
    def _locate_entries(data: Any) -> list[Any] | None:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "tests", "testResults", "cases"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    return candidate
        return None

    def _build(
        self, entry: dict[str, Any], metadata: RunMetadata, report: ParsedReport
    ) -> TestResultCreate | None:
        name = entry.get("name") or entry.get("test_name") or entry.get("title")
        if not name:
            report.warnings.append("skipped a result entry with no name")
            return None

        error = entry.get("error")
        if isinstance(error, str):
            error = {"message": error}
        elif not isinstance(error, dict):
            error = {}

        message = error.get("message") or entry.get("error_message")
        error_type = error.get("type") or entry.get("error_type")
        if error_type and message and not str(message).startswith(str(error_type)):
            message = f"{error_type}: {message}"

        # Grid node and browser identify *where* the test ran. Concentrated
        # failures on one node are an infrastructure verdict, and without this
        # field that conclusion is unreachable from the data.
        worker = entry.get("node") or entry.get("worker") or entry.get("session_id")
        browser = entry.get("browser") or entry.get("capabilities", {}).get("browserName") \
            if isinstance(entry.get("capabilities"), dict) else entry.get("browser")

        test_name = str(name)
        if browser:
            test_name = f"{test_name} [{browser}]"

        duration = entry.get("duration_ms")
        if duration is None and entry.get("duration") is not None:
            # A bare `duration` is ambiguous. Values under 1000 are almost
            # certainly seconds (no browser test finishes in under a second),
            # which is the least-wrong heuristic available without a unit field.
            raw = entry["duration"]
            duration = (
                self.to_millis(raw, unit="s")
                if isinstance(raw, (int, float)) and raw < 1000
                else self.to_millis(raw)
            )

        return TestResultCreate(
            test_name=test_name,
            test_suite=entry.get("suite") or entry.get("class") or entry.get("classname"),
            test_file=entry.get("file"),
            framework=self.framework,
            status=self.normalize_status(
                entry.get("status") or entry.get("outcome") or entry.get("result"),
                framework=self.framework,
            ),
            duration_ms=self.to_millis(duration),
            attempt=int(entry.get("attempt") or entry.get("retry") or 0),
            error_message=str(message) if message else None,
            stack_trace=error.get("stack") or error.get("stacktrace") or entry.get("stack_trace"),
            logs=self._collect_logs(entry),
            screenshot_url=entry.get("screenshot") or entry.get("screenshot_url"),
            video_url=entry.get("video") or entry.get("video_url"),
            worker_id=str(worker) if worker else None,
            started_at=self.parse_timestamp(entry.get("started_at") or entry.get("start_time")),
            timestamp=self.parse_timestamp(entry.get("timestamp") or entry.get("end_time")),
            environment=metadata.environment,
            git_commit=metadata.git_commit,
            git_branch=metadata.git_branch,
            ci_run_id=metadata.ci_run_id,
            ci_provider=metadata.ci_provider,
            ci_job_url=metadata.ci_job_url,
            metrics=self._collect_metrics(entry),
            raw_payload=entry,
        )

    def _collect_logs(self, entry: dict[str, Any]) -> str | None:
        """Gather log-ish fields, including the browser console.

        The browser console is the highest-value log a Selenium harness can
        capture and the one most often discarded: a JS exception there explains
        a great many "element not found" failures that otherwise look like
        locator problems.
        """
        parts: list[str] = []
        for key, label in (
            ("logs", "logs"),
            ("output", "output"),
            ("console", "browser console"),
            ("browser_logs", "browser console"),
            ("har", "network (HAR)"),
        ):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"--- {label} ---\n{value.strip()}")
            elif isinstance(value, list) and value:
                rendered = self.join_output(value)
                if rendered:
                    parts.append(f"--- {label} ---\n{rendered}")
        return "\n\n".join(parts) if parts else None

    @staticmethod
    def _collect_metrics(entry: dict[str, Any]) -> SystemMetrics:
        """Pick up host metrics when a custom reporter bothered to record them."""
        raw_metrics = entry.get("metrics")
        source: dict[str, Any] = raw_metrics if isinstance(raw_metrics, dict) else entry
        try:
            return SystemMetrics(
                cpu_percent=source.get("cpu_percent"),
                memory_mb=source.get("memory_mb"),
                network_latency_ms=source.get("network_latency_ms"),
            )
        except Exception:
            return SystemMetrics()


register(SeleniumParser())
