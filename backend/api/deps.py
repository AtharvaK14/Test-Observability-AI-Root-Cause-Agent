"""Shared FastAPI dependencies.

Everything here is overridable via ``app.dependency_overrides`` in tests, which
is the seam that lets the API suite run against an in-memory SQLite database
with the agent stubbed out.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
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


def require_api_key(
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Reject requests that do not carry the configured API key.

    A no-op when no key is configured, which ``Settings`` only allows in local
    and ci. The comparison is constant-time and on bytes: ``compare_digest`` on
    ``str`` raises for non-ASCII input, which would turn a junk header into a
    500 instead of a 401.
    """
    if settings.api_key is None:
        return
    expected = settings.api_key.get_secret_value().encode()
    supplied = (x_api_key or "").encode()
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


ApiKeyDep = Depends(require_api_key)
