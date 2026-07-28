"""Pydantic schemas and shared enums (the API/wire contract).

Deliberately separate from ``backend.db`` (the persistence contract) so the two
can evolve independently: adding a denormalised column for query performance
should not change the JSON that CI runners POST.
"""

from backend.models.enums import (
    AnalysisStatus,
    FeedbackVerdict,
    RootCauseCategory,
    TestFramework,
    TestStatus,
)

__all__ = [
    "AnalysisStatus",
    "FeedbackVerdict",
    "RootCauseCategory",
    "TestFramework",
    "TestStatus",
]
