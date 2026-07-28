"""Engine, session factory, and the FastAPI request-scoped session dependency.

Concurrency model, stated once so the rest of the codebase can stop worrying
about it: sessions are **synchronous**. FastAPI route handlers that touch the
database are declared ``def`` rather than ``async def``, which makes Starlette
run them in a worker threadpool. A blocking ``psycopg`` call in an ``async def``
handler would stall the entire event loop — the single most common performance
bug in FastAPI services, and the one the project spec's sample code contains.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import Settings, get_settings
from backend.db.base import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    """Dialect-appropriate engine options."""
    if settings.is_sqlite:
        # Unit-test path. StaticPool + a shared connection keeps an in-memory
        # database alive across sessions; without it every new connection gets a
        # fresh, empty database and tests fail with "no such table".
        return {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": settings.db_pool_pre_ping,
        "pool_recycle": 1800,
    }


def init_engine(settings: Settings | None = None, *, force: bool = False) -> Engine:
    """Create (once) and return the process-wide engine."""
    global _engine, _session_factory

    if _engine is not None and not force:
        return _engine

    settings = settings or get_settings()
    engine = create_engine(
        settings.database_url,
        echo=settings.db_echo,
        future=True,
        **_engine_kwargs(settings),
    )

    if settings.is_sqlite:
        # SQLite ignores foreign keys unless asked. Tests that rely on the DB to
        # reject an orphaned analysis would otherwise pass locally and the same
        # bug would surface as a constraint violation only in Postgres.
        @event.listens_for(engine, "connect")
        def _enable_sqlite_fks(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _engine = engine
    _session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        # expire_on_commit=False so ORM objects stay readable after the request's
        # commit. Otherwise serialising a response touches expired attributes,
        # triggers a lazy reload on a closed session, and raises DetachedInstanceError.
    )
    logger.info("database engine initialised", extra={"dialect": engine.dialect.name})
    return engine


def get_engine() -> Engine:
    """Return the engine, initialising it on first use."""
    return _engine if _engine is not None else init_engine()


def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory, initialising the engine on first use."""
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None  # narrowed by init_engine
    return _session_factory


def create_all(engine: Engine | None = None) -> None:
    """Create every table.

    Fine for local development and tests. Production schema changes should go
    through Alembic (``alembic upgrade head``) — ``create_all`` cannot alter an
    existing table, so it silently does nothing when a column is added and you
    find out via a runtime ``UndefinedColumn`` error.
    """
    Base.metadata.create_all(bind=engine or get_engine())


def drop_all(engine: Engine | None = None) -> None:
    """Drop every table. Test teardown only."""
    Base.metadata.drop_all(bind=engine or get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for non-HTTP callers (CLI, background tasks, tests).

    Commits on clean exit, rolls back on exception, always closes. Background
    analysis tasks run outside the request lifecycle and must not reuse the
    request's session — it is already closed by the time they execute.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session (unit of work).

    The commit lives here, not in the repositories. Repositories ``add`` and
    ``flush``; the request boundary decides whether the whole unit of work
    succeeded. That is what makes "ingest 200 results" atomic instead of leaving
    137 rows behind when row 138 violates a constraint.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
