"""PyTest JSON report parser (``pytest --json-report``, from pytest-json-report).

Report shape::

    {"created": 1700000000.0, "duration": 12.3,
     "tests": [
       {"nodeid": "tests/test_auth.py::TestLogin::test_admin[chrome]",
        "outcome": "failed",
        "setup":    {"duration": 0.01, "outcome": "passed"},
        "call":     {"duration": 0.42, "outcome": "failed",
                     "crash": {"path": "...", "lineno": 42, "message": "..."},
                     "longrepr": "...", "stdout": "...", "stderr": "...",
                     "log": [...]},
        "teardown": {"duration": 0.01, "outcome": "passed"}}]}

Named ``pytest_report`` rather than ``pytest`` deliberately: a module named
``pytest.py`` on the path shadows the pytest package itself, and the resulting
import error appears far away from its cause.

The interesting part is the setup/call/teardown split, which the naive parser
collapses. It should not be collapsed — a failure in ``setup`` is a *fixture*
failure, which is a completely different problem from an assertion failing in
the test body, and this is the only framework that hands us that distinction for
free.
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
from backend.models.test_result import TestResultCreate

_PHASES = ("setup", "call", "teardown")


class PytestParser(ReportParser):
    framework = TestFramework.PYTEST

    def parse(self, payload: bytes, metadata: RunMetadata) -> ParsedReport:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"not valid JSON ({exc.msg} at line {exc.lineno}). Generate the report "
                "with `pytest --json-report --json-report-file=report.json` "
                "(pip install pytest-json-report).",
                self.framework,
            ) from exc

        if not isinstance(data, dict) or not isinstance(data.get("tests"), list):
            raise ParseError(
                "no 'tests' array found — this does not look like a pytest-json-report "
                "file. For a JUnit XML file (`pytest --junitxml=...`), use the "
                "/ingest/junit endpoint instead.",
                self.framework,
            )

        report = ParsedReport(framework=self.framework)
        run_started = self.parse_timestamp(data.get("created"))

        for entry in data["tests"]:
            if not isinstance(entry, dict):
                report.warnings.append("skipped a malformed test entry")
                continue
            result = self._parse_test(entry, run_started, metadata, report)
            if result is not None:
                report.results.append(result)

        # A collection error means whole files never ran. Those tests produce no
        # entries at all, so without this the dashboard shows a suspiciously
        # small, green run rather than a broken one.
        for error in data.get("collectors") or []:
            if isinstance(error, dict) and error.get("outcome") == "failed":
                report.results.append(self._collection_error(error, metadata))

        if not report.results:
            report.warnings.append("report parsed successfully but contained no tests")
        return report

    # --------------------------------------------------------------- test

    def _parse_test(
        self,
        entry: dict[str, Any],
        run_started: Any,
        metadata: RunMetadata,
        report: ParsedReport,
    ) -> TestResultCreate | None:
        nodeid = str(entry.get("nodeid") or "").strip()
        if not nodeid:
            report.warnings.append("skipped a test entry with no nodeid")
            return None

        # nodeid is "path/to/file.py::Class::test_name[param]". Parameters stay
        # in the name: test_login[admin] and test_login[guest] fail for
        # different reasons and must not share a history.
        file_path, _, remainder = nodeid.partition("::")
        parts = [p for p in remainder.split("::") if p]
        suite = "::".join(parts[:-1]) if len(parts) > 1 else file_path or None

        failing_phase, phase_data = self._failing_phase(entry)
        status = self.normalize_status(entry.get("outcome"), framework=self.framework)

        # xfail/xpass arrive as outcome values that map to SKIPPED above; an
        # unexpectedly-passing xfail is a real signal (the bug got fixed, or the
        # marker is stale) so it is kept visible rather than dropped.
        message, stack = self._extract_error(phase_data)
        if failing_phase in ("setup", "teardown") and message:
            # Label it, because "assert 1 == 2" reads identically whether it came
            # from a fixture or the test body, and the owner is different.
            message = f"[{failing_phase} phase] {message}"

        duration = sum(
            self.to_millis((entry.get(p) or {}).get("duration"), unit="s") for p in _PHASES
        )

        return TestResultCreate(
            test_name=nodeid,
            test_suite=suite,
            test_file=file_path or None,
            framework=self.framework,
            status=status,
            duration_ms=duration,
            attempt=self._rerun_index(entry),
            error_message=message,
            stack_trace=stack,
            logs=self._collect_output(entry),
            worker_id=self._worker(entry),
            started_at=run_started,
            timestamp=run_started,
            environment=metadata.environment,
            git_commit=metadata.git_commit,
            git_branch=metadata.git_branch,
            ci_run_id=metadata.ci_run_id,
            ci_provider=metadata.ci_provider,
            ci_job_url=metadata.ci_job_url,
            raw_payload=entry,
        )

    @staticmethod
    def _rerun_index(entry: dict[str, Any]) -> int:
        """Retry index, when pytest-rerunfailures recorded one.

        Plain pytest-json-report has no notion of retries, so this is 0 for most
        reports — but when the plugin is present it is what keeps a
        retried-and-passed test from being recorded as a clean green.
        """
        meta = entry.get("metadata")
        if not isinstance(meta, dict):
            return 0
        try:
            return max(0, int(meta.get("rerun", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _failing_phase(entry: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """Find which phase failed, preferring the earliest.

        Setup first: if a fixture blew up, the call phase never ran and its
        (absent) outcome says nothing useful.
        """
        for phase in _PHASES:
            data = entry.get(phase)
            if isinstance(data, dict) and data.get("outcome") in ("failed", "error"):
                return phase, data
        call = entry.get("call")
        return None, call if isinstance(call, dict) else {}

    def _extract_error(self, phase: dict[str, Any]) -> tuple[str | None, str | None]:
        """Pull a message and a traceback out of a phase record.

        ``crash`` holds the one-line summary; ``longrepr`` holds pytest's full
        rendered traceback with source context and captured locals — the most
        useful artefact pytest produces, and the thing a parser that only reads
        ``crash.message`` throws away.
        """
        if not phase:
            return None, None

        raw_crash = phase.get("crash")
        crash: dict[str, Any] = raw_crash if isinstance(raw_crash, dict) else {}
        message = crash.get("message")
        longrepr = phase.get("longrepr")

        if isinstance(longrepr, dict):
            longrepr = longrepr.get("reprcrash", {}).get("message") or json.dumps(longrepr)

        if not message and isinstance(longrepr, str):
            message = longrepr.strip().splitlines()[-1] if longrepr.strip() else None

        location = ""
        if crash.get("path"):
            location = f" ({crash['path']}:{crash.get('lineno', '?')})"

        stack = str(longrepr) if longrepr else None
        if not stack and phase.get("traceback"):
            stack = "\n".join(
                f"{f.get('path')}:{f.get('lineno')} in {f.get('message', '')}"
                for f in phase["traceback"]
                if isinstance(f, dict)
            )

        return (f"{message}{location}" if message else None), stack

    def _collect_output(self, entry: dict[str, Any]) -> str | None:
        """Gather captured stdout/stderr/logging across all three phases.

        Test code logs from all three, and for an integration test the decisive
        line (a connection error, a 500 from a service) is frequently in the
        setup phase's output rather than the call phase's.
        """
        chunks: list[str] = []
        for phase in _PHASES:
            data = entry.get(phase)
            if not isinstance(data, dict):
                continue
            for stream in ("stdout", "stderr"):
                text = data.get(stream)
                if isinstance(text, str) and text.strip():
                    chunks.append(f"--- {phase}/{stream} ---\n{text.strip()}")
            records = data.get("log")
            if isinstance(records, list) and records:
                rendered = "\n".join(
                    f"{r.get('levelname', 'INFO')} {r.get('name', '')}: {r.get('msg', '')}"
                    for r in records
                    if isinstance(r, dict)
                )
                if rendered.strip():
                    chunks.append(f"--- {phase}/logging ---\n{rendered}")
        return "\n\n".join(chunks) if chunks else None

    @staticmethod
    def _worker(entry: dict[str, Any]) -> str | None:
        """Extract the pytest-xdist worker id.

        Worth capturing: when failures concentrate on one gw* worker, the cause
        is that process or the machine it runs on, which no amount of reading
        the test code will reveal.
        """
        meta = entry.get("metadata")
        if isinstance(meta, dict):
            for key in ("worker", "workerid", "gw"):
                if meta.get(key):
                    return str(meta[key])
        return None

    def _collection_error(
        self, error: dict[str, Any], metadata: RunMetadata
    ) -> TestResultCreate:
        """Represent a collection failure as an ERROR row.

        Usually an ImportError or a syntax error — the tests in that module
        never executed, and treating that as "no tests" is how a broken suite
        looks healthy.
        """
        nodeid = str(error.get("nodeid") or "<unknown module>")
        return TestResultCreate(
            test_name=f"<collection error: {nodeid}>",
            test_file=nodeid,
            framework=self.framework,
            status=TestStatus.ERROR,
            duration_ms=0,
            error_message=str(error.get("longrepr") or "collection failed")[:4000],
            stack_trace=str(error.get("longrepr")) if error.get("longrepr") else None,
            environment=metadata.environment,
            git_commit=metadata.git_commit,
            git_branch=metadata.git_branch,
            ci_run_id=metadata.ci_run_id,
            ci_provider=metadata.ci_provider,
            ci_job_url=metadata.ci_job_url,
            raw_payload=error,
        )


register(PytestParser())
