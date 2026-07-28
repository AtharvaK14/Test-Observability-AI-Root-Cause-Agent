"""Framework-specific report parsers.

Separate from ``backend.api.ingest`` on purpose: parsing is where the bugs live,
and a parser that is a pure ``bytes -> list[TestResultCreate]`` function can be
tested against a real Playwright report checked into the repo, with no HTTP
client, no database, and no fixtures.
"""

from backend.ingest.base import (
    ParsedReport,
    ParseError,
    ReportParser,
    get_parser,
    parse_report,
)

__all__ = [
    "ParseError",
    "ParsedReport",
    "ReportParser",
    "get_parser",
    "parse_report",
]
