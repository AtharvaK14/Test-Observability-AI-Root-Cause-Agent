"""Shared FastAPI dependencies.

Everything here is overridable via ``app.dependency_overrides`` in tests, which
is the seam that lets the API suite run against an in-memory SQLite database
with the agent stubbed out.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from backend.analysis.context_retriever import ContextRetriever
from backend.config import Settings, get_settings
from backend.db.session import get_db

SessionDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_context_retriever(session: SessionDep, settings: SettingsDep) -> ContextRetriever:
    """Request-scoped context retriever."""
    return ContextRetriever(session, settings)


RetrieverDep = Annotated[ContextRetriever, Depends(get_context_retriever)]
