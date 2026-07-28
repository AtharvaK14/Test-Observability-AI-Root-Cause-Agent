"""Framework report parsers, against realistic fixtures.

Each fixture is shaped like the real thing a CI run produces, including the
awkward parts: Playwright's four-level nesting and retry arrays, Cypress's three
mutually incompatible JSON shapes, PyTest's setup/call/teardown split.
"""

from __future__ import annotations

import pytest

from backend.ingest.base import ParseError, RunMetadata, parse_report
from backend.ingest.junit import parse_junit
from backend.models.enums import TestFramework, TestStatus


def by_name(report, fragment: str):
    matches = [r for r in report.results if fragment in r.test_name]
    assert matches, f"no result matching {fragment!r} in {[r.test_name for r in report.results]}"
    return matches


class TestPlaywright:
    @pytest.fixture
    def report(self, fixture_bytes, metadata):
        return parse_report(
            TestFramework.PLAYWRIGHT, fixture_bytes("playwright-report.json"), metadata
        )

    def test_flattens_nested_suites_into_full_names(self, report) -> None:
        names = {r.test_name for r in report.results}
        assert "checkout.spec.ts > guest checkout > completes purchase [chromium]" in names

    def test_browser_project_is_part_of_test_identity(self, report) -> None:
        """A locator flaky only in WebKit is a different problem from one flaky
        everywhere; merging their histories hides that."""
        assert all(
            "[chromium]" in r.test_name or "[webkit]" in r.test_name or "<" in r.test_name
            for r in report.results
        )

    def test_retry_that_passes_is_recorded_as_flaky(self, report) -> None:
        """The core flakiness signal: fail then pass on retry.

        Playwright reports this as a pass and the suite goes green. Recording
        the final attempt as PASSED would hide exactly the failure worth
        investigating."""
        attempts = sorted(
            by_name(report, "completes purchase"), key=lambda r: r.attempt
        )
        assert [a.status for a in attempts] == [TestStatus.FAILED, TestStatus.FLAKY]
        assert [a.attempt for a in attempts] == [0, 1]
        assert all(a.retry_count == 1 for a in attempts)

    def test_extracts_artifact_references(self, report) -> None:
        first = sorted(by_name(report, "completes purchase"), key=lambda r: r.attempt)[0]
        assert first.screenshot_url == "/tmp/shot.png"
        assert first.trace_url == "/tmp/trace.zip", "the trace is the most useful artefact"

    def test_merges_stdout_and_stderr_preserving_order(self, report) -> None:
        first = sorted(by_name(report, "completes purchase"), key=lambda r: r.attempt)[0]
        assert first.logs is not None
        assert "POST /api/orders 503" in first.logs
        assert first.logs.index("stdout") < first.logs.index("stderr")

    def test_synthesises_a_message_for_bare_timeouts(self, report) -> None:
        """status=timedOut carries no error object; without this the failure has
        no text to fingerprint and no evidence for the agent."""
        timed_out = by_name(report, "loads cart page")[0]
        assert timed_out.status == TestStatus.ERROR
        assert "30000" in (timed_out.error_message or "")

    def test_run_level_errors_become_visible_rows(self, report) -> None:
        """A global-setup failure belongs to no test. Dropping it makes a broken
        run look like an empty one."""
        errors = by_name(report, "run error")
        assert errors[0].status == TestStatus.ERROR
        assert "seed users" in (errors[0].error_message or "")

    def test_stamps_ci_metadata_on_every_row(self, report, metadata) -> None:
        assert all(r.ci_run_id == metadata.ci_run_id for r in report.results)
        assert all(r.git_commit == metadata.git_commit for r in report.results)

    def test_rejects_a_non_playwright_json_file(self, fixture_bytes, metadata) -> None:
        with pytest.raises(ParseError, match="suites"):
            parse_report(TestFramework.PLAYWRIGHT, fixture_bytes("malformed.json"), metadata)

    def test_error_names_the_reporter_flag(self, metadata) -> None:
        """"Invalid JSON" alone leaves a pipeline owner with nothing to act on."""
        with pytest.raises(ParseError, match="reporter=json"):
            parse_report(TestFramework.PLAYWRIGHT, b"{not json", metadata)


class TestCypress:
    def test_module_api_shape_with_attempts(self, fixture_bytes, metadata) -> None:
        report = parse_report(
            TestFramework.CYPRESS, fixture_bytes("cypress-report.json"), metadata
        )
        failures = by_name(report, "rejects a bad password")
        assert len(failures) == 2, "both retry attempts are stored"
        assert all(f.status == TestStatus.FAILED for f in failures), "never passed → not flaky"
        assert failures[0].video_url is not None
        assert failures[0].screenshot_url is not None

    def test_title_array_becomes_a_qualified_name(self, fixture_bytes, metadata) -> None:
        report = parse_report(
            TestFramework.CYPRESS, fixture_bytes("cypress-report.json"), metadata
        )
        assert by_name(report, "Login > rejects a bad password")

    def test_mochawesome_shape_is_auto_detected(self, fixture_bytes, metadata) -> None:
        """Users should not have to know which of three shapes their CI emits."""
        report = parse_report(
            TestFramework.CYPRESS, fixture_bytes("cypress-mochawesome.json"), metadata
        )
        assert len(report.results) == 2
        failed = by_name(report, "applies a coupon")[0]
        assert failed.status == TestStatus.FAILED
        assert "detached" in (failed.error_message or "")

    def test_unknown_shape_names_the_shapes_it_expected(self, fixture_bytes, metadata) -> None:
        with pytest.raises(ParseError, match="mochawesome"):
            parse_report(TestFramework.CYPRESS, fixture_bytes("malformed.json"), metadata)


class TestPytest:
    @pytest.fixture
    def report(self, fixture_bytes, metadata):
        return parse_report(TestFramework.PYTEST, fixture_bytes("pytest-report.json"), metadata)

    def test_keeps_parameters_in_the_test_name(self, report) -> None:
        """test_total[admin] and test_total[guest] fail for different reasons and
        must not share a history."""
        assert by_name(report, "test_total[admin]")

    def test_labels_setup_phase_failures(self, report) -> None:
        """A fixture failure and an assertion failure read identically; only the
        owner differs, and this is the one framework that tells us which."""
        fixture_failure = by_name(report, "test_fixture_dependent")[0]
        assert fixture_failure.status == TestStatus.ERROR
        assert "[setup phase]" in (fixture_failure.error_message or "")
        assert "seed-db" in (fixture_failure.error_message or "")

    def test_captures_full_longrepr_not_just_the_crash_line(self, report) -> None:
        failure = by_name(report, "test_total[admin]")[0]
        assert failure.stack_trace is not None
        assert "assert 0 == 120" in failure.stack_trace

    def test_collects_output_from_every_phase(self, report) -> None:
        failure = by_name(report, "test_total[admin]")[0]
        assert failure.logs is not None
        assert "500 from /api/orders" in failure.logs

    def test_durations_are_seconds_converted_to_millis(self, report) -> None:
        """pytest reports fractional seconds. Reading them as milliseconds makes
        every duration 1000x too small and destroys the baseline comparison."""
        failure = by_name(report, "test_total[admin]")[0]
        assert failure.duration_ms == pytest.approx(460, abs=5)

    def test_captures_xdist_worker(self, report) -> None:
        assert by_name(report, "test_total[admin]")[0].worker_id == "gw2"

    def test_collection_errors_are_surfaced(self, report) -> None:
        """An ImportError means a whole module never ran. Treating that as
        "no tests" is how a broken suite looks healthy."""
        collection = by_name(report, "collection error")[0]
        assert collection.status == TestStatus.ERROR
        assert "missing_helper" in (collection.error_message or "")


class TestSelenium:
    @pytest.fixture
    def report(self, fixture_bytes, metadata):
        return parse_report(
            TestFramework.SELENIUM, fixture_bytes("selenium-report.json"), metadata
        )

    def test_captures_grid_node(self, report) -> None:
        """Failures concentrated on one node are an infrastructure verdict that
        is unreachable without this field."""
        assert by_name(report, "search returns")[0].worker_id == "grid-node-3"

    def test_captures_browser_console(self, report) -> None:
        """A JS exception here explains many "element not found" failures that
        otherwise look like locator problems."""
        logs = by_name(report, "search returns")[0].logs or ""
        assert "t.map is not a function" in logs

    def test_captures_system_metrics(self, report) -> None:
        failure = by_name(report, "search returns")[0]
        assert failure.metrics.cpu_percent == pytest.approx(96.4)
        assert failure.metrics.memory_mb == pytest.approx(210)

    def test_prefixes_error_type(self, report) -> None:
        assert (by_name(report, "search returns")[0].error_message or "").startswith(
            "TimeoutException:"
        )

    def test_xml_payload_is_delegated_to_junit(self, fixture_bytes, metadata) -> None:
        report = parse_report(
            TestFramework.SELENIUM, fixture_bytes("junit-report.xml"), metadata
        )
        assert report.framework == TestFramework.SELENIUM
        assert len(report.results) == 4


class TestJUnit:
    @pytest.fixture
    def report(self, fixture_bytes, metadata):
        return parse_junit(
            fixture_bytes("junit-report.xml"), metadata, framework=TestFramework.PLAYWRIGHT
        )

    def test_attributes_to_the_caller_supplied_framework(self, report) -> None:
        """JUnit XML is a format, not a framework. Mislabelling it breaks
        cross-framework correlation, the strongest signal in the system."""
        assert all(r.framework == TestFramework.PLAYWRIGHT for r in report.results)

    def test_distinguishes_failure_from_error(self, report) -> None:
        """<failure> is an assertion the test lost; <error> is the test blowing
        up before it could assert. Different causes, different owners."""
        assert by_name(report, "testInvalidLogin")[0].status == TestStatus.FAILED
        assert by_name(report, "testSsoLogin")[0].status == TestStatus.ERROR

    def test_time_attribute_is_seconds(self, report) -> None:
        assert by_name(report, "testInvalidLogin")[0].duration_ms == 3400

    def test_skipped_is_not_a_failure(self, report) -> None:
        assert by_name(report, "testLegacyLogin")[0].status == TestStatus.SKIPPED

    def test_merges_error_type_into_the_message(self, report) -> None:
        assert "AssertionError" in (by_name(report, "testInvalidLogin")[0].error_message or "")

    def test_blocks_xxe_entity_expansion(self, metadata) -> None:
        """A public upload endpoint parsing untrusted XML is an XXE vector.
        stdlib ElementTree would happily read /etc/passwd here."""
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b"<testsuites><testsuite name=\"s\">"
            b'<testcase name="t">&xxe;</testcase></testsuite></testsuites>'
        )
        with pytest.raises(ParseError):
            parse_junit(payload, metadata, framework=TestFramework.SELENIUM)

    def test_accepts_a_bare_testsuite_root(self, metadata) -> None:
        """pytest emits <testsuite> at the root, not <testsuites>. Handling only
        the latter silently accepts the file and yields zero results."""
        payload = (
            b'<testsuite name="s" tests="1"><testcase classname="c" name="t" time="1.0"/>'
            b"</testsuite>"
        )
        report = parse_junit(payload, metadata, framework=TestFramework.PYTEST)
        assert len(report.results) == 1


class TestEmptyInput:
    @pytest.mark.parametrize("framework", list(TestFramework))
    def test_empty_payload_is_rejected_for_every_framework(
        self, framework: TestFramework, metadata: RunMetadata
    ) -> None:
        with pytest.raises(ParseError):
            parse_report(framework, b"", metadata)
