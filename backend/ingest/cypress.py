"""Cypress report parser.

Cypress has no single JSON format — which shape you get depends on how the
suite is run, and teams in the same org routinely produce different ones:

1. **Module API** (``cypress.run()`` result, or ``--reporter json`` in recent
   versions): ``{"runs": [{"spec": {...}, "tests": [{"attempts": [...]}]}]}``.
   The richest shape and the only one with native retry data.
2. **mochawesome**: ``{"results": [{"suites": [{"tests": [...]}]}]}`` — the most
   common CI reporter, nests recursively.
3. **Plain mocha JSON**: ``{"stats": {...}, "tests": [...], "failures": [...]}``.
   Flat, no retry information.

Rather than making the user know which one their pipeline emits, the parser
sniffs the shape. Guessing wrong is loud (``ParseError``), never silent.
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
from backend.models.enums import TestFramework
from backend.models.test_result import TestResultCreate


class CypressParser(ReportParser):
    framework = TestFramework.CYPRESS

    def parse(self, payload: bytes, metadata: RunMetadata) -> ParsedReport:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"not valid JSON ({exc.msg} at line {exc.lineno}). Cypress JSON output "
                "comes from `cypress run --reporter json`, the mochawesome reporter, "
                "or the `cypress.run()` module API.",
                self.framework,
            ) from exc

        if not isinstance(data, dict):
            raise ParseError("expected a JSON object at the top level", self.framework)

        report = ParsedReport(framework=self.framework)

        if isinstance(data.get("runs"), list):
            self._parse_module_api(data["runs"], metadata, report)
        elif isinstance(data.get("results"), list):
            self._parse_mochawesome(data["results"], metadata, report)
        elif isinstance(data.get("tests"), list):
            self._parse_plain_mocha(data, metadata, report)
        else:
            raise ParseError(
                "unrecognised Cypress report shape: expected one of 'runs' (module "
                "API), 'results' (mochawesome), or 'tests' (mocha json) at the top "
                f"level, found keys {sorted(data)[:8]}.",
                self.framework,
            )

        if not report.results:
            report.warnings.append("report parsed successfully but contained no tests")
        return report

    # ------------------------------------------------------ 1. module API

    def _parse_module_api(
        self, runs: list[Any], metadata: RunMetadata, report: ParsedReport
    ) -> None:
        """Parse ``cypress.run()`` output — the only shape with retry attempts."""
        for run in runs:
            if not isinstance(run, dict):
                report.warnings.append("skipped a malformed run entry")
                continue

            raw_spec = run.get("spec")
            spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
            spec_file = spec.get("relative") or spec.get("name")
            # The video is per-spec, not per-test, so every failure in the spec
            # points at the same recording — still worth attaching, since it is
            # usually the fastest way for a human to see what happened.
            video = run.get("video")

            for test in run.get("tests") or []:
                if not isinstance(test, dict):
                    report.warnings.append("skipped a malformed test entry")
                    continue

                title = test.get("title")
                # Cypress carries the title as a path array: ["describe", "it"].
                if isinstance(title, list):
                    parts = [str(t) for t in title if t]
                else:
                    parts = [str(title or "unnamed test")]
                test_name = " > ".join(parts)
                suite = " > ".join(parts[:-1]) or None

                attempts_raw = test.get("attempts")
                if not isinstance(attempts_raw, list) or not attempts_raw:
                    attempts_raw = [test]

                attempts: list[TestResultCreate] = []
                for index, attempt in enumerate(attempts_raw):
                    if not isinstance(attempt, dict):
                        continue
                    attempts.append(
                        self._build(
                            test_name=test_name,
                            suite=suite,
                            file_path=spec_file,
                            state=attempt.get("state") or test.get("state"),
                            duration=attempt.get("duration") or attempt.get("wallClockDuration"),
                            error=(
                                attempt.get("error")
                                or attempt.get("err")
                                or test.get("displayError")
                            ),
                            attempt_index=index,
                            started_at=(
                                attempt.get("startedAt")
                                or attempt.get("wallClockStartedAt")
                            ),
                            video_url=str(video) if video else None,
                            screenshot_url=self._first_screenshot(attempt),
                            metadata=metadata,
                            raw=attempt,
                        )
                    )

                if attempts:
                    self.resolve_retry_statuses(attempts)
                    report.results.extend(attempts)
                else:
                    report.warnings.append(f"test {test_name!r} had no usable attempts")

    # ----------------------------------------------------- 2. mochawesome

    def _parse_mochawesome(
        self, results: list[Any], metadata: RunMetadata, report: ParsedReport
    ) -> None:
        for entry in results:
            if not isinstance(entry, dict):
                report.warnings.append("skipped a malformed results entry")
                continue
            file_path = entry.get("file") or entry.get("fullFile")
            self._walk_mocha_suite(entry, [], file_path, metadata, report)

    def _walk_mocha_suite(
        self,
        node: dict[str, Any],
        ancestors: list[str],
        file_path: Any,
        metadata: RunMetadata,
        report: ParsedReport,
    ) -> None:
        """Recurse through mochawesome's nested suites."""
        title = str(node.get("title") or "").strip()
        path = [*ancestors, title] if title else ancestors

        for test in node.get("tests") or []:
            if not isinstance(test, dict):
                report.warnings.append("skipped a malformed test entry")
                continue
            name = str(test.get("title") or "unnamed test")
            full = " > ".join([*path, name]) if path else name
            report.results.append(
                self._build(
                    test_name=full,
                    suite=" > ".join(path) or None,
                    file_path=file_path,
                    state=self._mocha_state(test),
                    duration=test.get("duration"),
                    error=test.get("err"),
                    attempt_index=0,
                    started_at=None,
                    metadata=metadata,
                    raw=test,
                )
            )

        for child in node.get("suites") or []:
            if isinstance(child, dict):
                self._walk_mocha_suite(child, path, file_path, metadata, report)

    # ---------------------------------------------------- 3. plain mocha

    def _parse_plain_mocha(
        self, data: dict[str, Any], metadata: RunMetadata, report: ParsedReport
    ) -> None:
        """Parse flat mocha JSON.

        The `tests` array contains every test; `failures` and `pending` are
        subsets of it. We iterate `tests` alone and derive state from the entry,
        because iterating all three double-counts every failure — a subtle bug
        that inflates the dashboard by exactly the number of failures.
        """
        for test in data.get("tests") or []:
            if not isinstance(test, dict):
                report.warnings.append("skipped a malformed test entry")
                continue
            full_title = str(test.get("fullTitle") or test.get("title") or "unnamed test")
            leaf = str(test.get("title") or "")
            suite = full_title[: -len(leaf)].strip() if leaf and full_title.endswith(leaf) else None
            report.results.append(
                self._build(
                    test_name=full_title,
                    suite=suite or None,
                    file_path=test.get("file"),
                    state=self._mocha_state(test),
                    duration=test.get("duration"),
                    error=test.get("err"),
                    attempt_index=int(test.get("currentRetry") or 0),
                    started_at=None,
                    metadata=metadata,
                    raw=test,
                )
            )

    # ---------------------------------------------------------- helpers

    @staticmethod
    def _mocha_state(test: dict[str, Any]) -> str:
        """Derive a state from a mocha test entry.

        Mocha does not always set `state`: a pending test has ``pending: true``
        and no state at all, and a passing test in some reporter versions has
        only ``pass: true``. Treating a missing state as failure (or as pass)
        both produce wrong dashboards, so each signal is checked explicitly.
        """
        if test.get("state"):
            return str(test["state"])
        if test.get("pending"):
            return "pending"
        err = test.get("err")
        if isinstance(err, dict) and (err.get("message") or err.get("stack")):
            return "failed"
        if test.get("pass") is True or test.get("fail") is False:
            return "passed"
        return "passed" if test.get("duration") is not None else "skipped"

    @staticmethod
    def _first_screenshot(attempt: dict[str, Any]) -> str | None:
        for shot in attempt.get("screenshots") or []:
            if isinstance(shot, dict) and shot.get("path"):
                return str(shot["path"])
        return None

    def _build(
        self,
        *,
        test_name: str,
        suite: str | None,
        file_path: Any,
        state: Any,
        duration: Any,
        error: Any,
        attempt_index: int,
        started_at: Any,
        metadata: RunMetadata,
        raw: dict[str, Any],
        video_url: str | None = None,
        screenshot_url: str | None = None,
    ) -> TestResultCreate:
        message, stack = self._extract_error(error)
        status = self.normalize_status(state, framework=self.framework)
        return TestResultCreate(
            test_name=test_name,
            test_suite=suite,
            test_file=str(file_path) if file_path else None,
            framework=self.framework,
            status=status,
            duration_ms=self.to_millis(duration),
            attempt=max(0, attempt_index),
            error_message=message,
            stack_trace=stack,
            video_url=video_url,
            screenshot_url=screenshot_url,
            started_at=self.parse_timestamp(started_at),
            timestamp=self.parse_timestamp(started_at),
            environment=metadata.environment,
            git_commit=metadata.git_commit,
            git_branch=metadata.git_branch,
            ci_run_id=metadata.ci_run_id,
            ci_provider=metadata.ci_provider,
            ci_job_url=metadata.ci_job_url,
            raw_payload=raw,
        )

    @staticmethod
    def _extract_error(error: Any) -> tuple[str | None, str | None]:
        """Pull message and stack out of the several error shapes Cypress uses."""
        if not error:
            return None, None
        if isinstance(error, str):
            # `displayError` is a pre-rendered string with the message on the
            # first line and the stack below it.
            first = error.partition("\n")[0]
            return first.strip() or None, error
        if isinstance(error, dict):
            message = error.get("message") or error.get("name")
            stack = error.get("stack") or error.get("estack") or error.get("codeFrame")
            if isinstance(stack, dict):
                stack = stack.get("frame") or json.dumps(stack)
            return (str(message) if message else None), (str(stack) if stack else None)
        return str(error), None


register(CypressParser())
