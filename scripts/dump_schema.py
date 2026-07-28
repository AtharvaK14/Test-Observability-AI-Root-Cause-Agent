"""Emit the PostgreSQL DDL for the current ORM metadata.

Generated rather than hand-written so ``db/schema.sql`` cannot silently drift
from ``backend/db/models.py``. Regenerate with:

    python scripts/dump_schema.py > db/schema.sql
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import backend.db.models  # noqa: F401  (registers all tables on the metadata)
from backend.db.base import Base

HEADER = """\
-- ---------------------------------------------------------------------------
-- Test Observability + AI Root-Cause Agent -- PostgreSQL schema
--
-- GENERATED FILE. Do not edit by hand.
--   python scripts/dump_schema.py > db/schema.sql
--
-- Applied automatically on first container start (docker-compose mounts this
-- into /docker-entrypoint-initdb.d). For an existing database, use Alembic.
-- ---------------------------------------------------------------------------
"""


def main() -> int:
    dialect = postgresql.dialect()
    out: list[str] = [HEADER]

    for table in Base.metadata.sorted_tables:
        out.append(f"\n-- {'-' * 74}\n-- {table.name}\n-- {'-' * 74}")
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        out.append(f"{ddl};\n")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            idx_ddl = str(CreateIndex(index).compile(dialect=dialect)).strip()
            out.append(f"{idx_ddl};")

    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
