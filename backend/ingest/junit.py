"""JUnit XML parser — the universal fallback.

Every framework here can emit JUnit XML (``--reporter=junit``, ``--junitxml``,
Surefire, TestNG), which makes it the one format that always works. It is also
the *lossiest*: no retry attempts, no artefact links, no structured error type
beyond a ``type`` attribute. Prefer a framework's native JSON when you have it;
reach for this when you do not.

Security note: XML parsing is a genuine attack surface. An uploaded document can
declare external entities that read local files (XXE) or expand recursively
until the process dies (billion laughs). ``defusedxml`` disables both. This is
not theoretical for a public ingest endpoint, and stdlib ``ElementTree`` does
not protect against it.
"""

from __future__ import annotations

import logging
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as DefusedET

from backend.ingest.base import ParsedReport, ParseError, ReportParser, RunMetadata
from backend.models.enums import TestFramework, TestStatus
from backend.models.test_result import TestResultCreate

logger = logging.getLogger(__name__)


class JUnitParser(ReportParser):
    """Parses JUnit XML on behalf of whichever framework produced it."""

    framework = TestFramework.SELENIUM
    """Default attribution.

    Selenium suites (JUnit, TestNG, NUnit) are the ones that most often have
    *only* XML available. The ``/ingest/junit`` endpoint overrides this per
    upload so a Playwright JUnit file is still attributed to Playwright — the
    framework field drives cross-framework correlation, so mislabelling it
    would break the single most diagnostic signal the system has.
    """

    def parse(
        self,
        payload: bytes,
        metadata: RunMetadata,
        framework: TestFramework | None = None,
    ) -> ParsedReport:
        attribution = framework or self.framework
        try:
            root = DefusedET.fromstring(payload)
        except Exception as exc:  # defusedxml raises several distinct types
            raise ParseError(
                f"not valid XML ({exc}). Expected a JUnit XML report such as "
                "`pytest --junitxml=results.xml` or "
                "`playwright test --reporter=junit`.",
                attribution,
            ) from exc

        suites = self._find_suites(root)
        if not suites:
            raise ParseError(
                f"no <testsuite> elements found (root element is <{root.tag}>). "
                "This does not look like a JUnit XML report.",
                attribution,
            )

        report = ParsedReport(framework=attribution)
        for suite in suites:
            self._parse_suite(suite, attribution, metadata, report)

        if not report.results:
            report.warnings.append("report parsed successfully but contained no testcases")
        return report

    @staticmethod
    def _find_suites(root: Element) -> list[Element]:
        """Locate the testsuite elements.

        The root is ``<testsuites>`` in most files but a bare ``<testsuite>``
        in some (pytest with a single suite, older Surefire). Handling only the
        first shape silently accepts the file and produces zero results.
        """
        if root.tag == "testsuite":
            return [root]
        suites = root.findall(".//testsuite")
        return suites or ([root] if root.find("testcase") is not None else [])

    def _parse_suite(
        self,
        suite: Element,
        framework: TestFramework,
        metadata: RunMetadata,
        report: ParsedReport,
    ) -> None:
        suite_name = suite.get("name") or None
        suite_logs = self._element_text(suite, "system-out"), self._element_text(
            suite, "system-err"
        )
        # Suite-level timestamps are per-suite, not per-test; the whole suite
        # shares one, which is the best resolution JUnit offers.
        started = self.parse_timestamp(suite.get("timestamp"))

        for case in suite.findall("testcase"):
            classname = case.get("classname") or ""
            name = case.get("name") or "unnamed test"
            # classname is the module/class path; combining them reproduces the
            # fully-qualified identity other formats give us directly.
            test_name = f"{classname}.{name}" if classname else name

            status, message, stack = self._case_outcome(case)
            case_logs = self._merge_logs(
                self._element_text(case, "system-out"),
                self._element_text(case, "system-err"),
                *suite_logs,
            )

            report.results.append(
                TestResultCreate(
                    test_name=test_name,
                    test_suite=classname or suite_name,
                    test_file=case.get("file"),
                    framework=framework,
                    status=status,
                    # JUnit's `time` is fractional SECONDS. Reading it as
                    # milliseconds makes every duration 1000x too small and
                    # quietly destroys the duration-vs-baseline signal.
                    duration_ms=self.to_millis(case.get("time"), unit="s"),
                    error_message=message,
                    stack_trace=stack,
                    logs=case_logs,
                    started_at=started,
                    timestamp=started,
                    environment=metadata.environment,
                    git_commit=metadata.git_commit,
                    git_branch=metadata.git_branch,
                    ci_run_id=metadata.ci_run_id,
                    ci_provider=metadata.ci_provider,
                    ci_job_url=metadata.ci_job_url,
                    raw_payload={
                        "attributes": dict(case.attrib),
                        "suite": suite_name,
                    },
                )
            )

    def _case_outcome(self, case: Element) -> tuple[TestStatus, str | None, str | None]:
        """Derive status and error detail from a testcase's children.

        JUnit distinguishes ``<failure>`` (an assertion the test made and lost)
        from ``<error>`` (the test blew up before it could assert). That is a
        real distinction — the first means the app behaved differently than
        expected, the second usually means something never got as far as being
        tested — so it is preserved rather than flattened to "failed".
        """
        for tag, status in (("failure", TestStatus.FAILED), ("error", TestStatus.ERROR)):
            node = case.find(tag)
            if node is not None:
                error_type = node.get("type")
                message = node.get("message") or (node.text or "").strip().split("\n")[0]
                if error_type and message and not message.startswith(error_type):
                    message = f"{error_type}: {message}"
                body = (node.text or "").strip() or None
                return status, (message or f"<{tag} with no message>"), body

        if case.find("skipped") is not None:
            node = case.find("skipped")
            return TestStatus.SKIPPED, (node.get("message") if node is not None else None), None

        # A rerun element means the runner retried. We cannot reconstruct the
        # individual attempts from JUnit, but flagging it keeps a retried pass
        # out of the "clean green" bucket.
        if case.find("rerunFailure") is not None or case.find("flakyFailure") is not None:
            node = case.find("rerunFailure") or case.find("flakyFailure")
            return (
                TestStatus.FLAKY,
                (node.get("message") if node is not None else "passed on retry"),
                (node.text or "").strip() if node is not None and node.text else None,
            )

        return TestStatus.PASSED, None, None

    @staticmethod
    def _element_text(parent: Element, tag: str) -> str | None:
        node = parent.find(tag)
        if node is None or not node.text:
            return None
        text = node.text.strip()
        return text or None

    @staticmethod
    def _merge_logs(*sections: Any) -> str | None:
        labels = ("case stdout", "case stderr", "suite stdout", "suite stderr")
        parts = [
            f"--- {label} ---\n{text}"
            for label, text in zip(labels, sections, strict=False)
            if isinstance(text, str) and text
        ]
        return "\n\n".join(parts) if parts else None


# Not registered against a framework: JUnit XML is a *format*, not a framework,
# and every framework can emit it. The Selenium parser delegates here for XML,
# and the /ingest/junit endpoint calls parse_junit() with an explicit framework.
_PARSER = JUnitParser()


def parse_junit(
    payload: bytes, metadata: RunMetadata, framework: TestFramework
) -> ParsedReport:
    """Parse a JUnit XML report and attribute it to an explicit framework."""
    return _PARSER.parse(payload, metadata, framework=framework)
