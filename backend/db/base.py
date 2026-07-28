"""Declarative base and cross-dialect column types.

Isolated from ``backend.db.models`` so Alembic (and tests) can import the
metadata without importing every model module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, MetaData, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase

# Explicit naming convention for constraints and indexes.
#
# Without this, Postgres auto-names constraints and Alembic cannot generate a
# reversible ``DROP CONSTRAINT`` for them — you discover this the first time a
# migration needs to alter a unique constraint in production and the downgrade
# path does not exist. Set it on day one; it is nearly free now and expensive later.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --- Portable column types --------------------------------------------------

# JSONB on Postgres (indexable, binary, supports containment operators); plain
# JSON everywhere else. The variant is what lets the entire repository test
# suite run against in-memory SQLite with zero production-code branching.
JSONColumn = JSONB().with_variant(JSON(), "sqlite")

class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC in Python.

    Postgres stores TIMESTAMPTZ and hands back aware datetimes. **SQLite has no
    timezone type**, so it silently returns naive ones — and the moment such a
    value meets a fresh ``utcnow()``, Python raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes``.

    That error surfaces far from its cause (a cluster's ``last_seen`` compared
    against an incoming result's timestamp, months after the row was written)
    and, worse, it appears *only* on the SQLite path — so it would slip through
    a Postgres-only staging environment and land in whatever runs on SQLite.

    Normalising in the type rather than at each call site means no application
    code has to care, and the SQLite test path behaves like production instead
    of being a subtly different dialect that hides bugs.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Going in: assume naive means UTC, then normalise to UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Coming out: attach UTC when the dialect dropped it (SQLite)."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def json_default_list() -> list[Any]:
    """Default factory for JSON list columns (never share a mutable default)."""
    return []
