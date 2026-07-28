"""FastAPI application entry point.

Run locally:      uvicorn backend.main:app --reload
Run in Docker:    docker compose up
API docs:         http://localhost:8000/docs
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from backend import __version__
from backend.api import analysis as analysis_routes
from backend.api import ingest as ingest_routes
from backend.config import Settings, get_settings
from backend.db.session import create_all, get_engine, init_engine
from backend.logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down."""
    settings: Settings = get_settings()
    configure_logging(settings)
    init_engine(settings)

    if settings.environment in ("local", "ci"):
        # Convenient locally, wrong in production: create_all cannot ALTER an
        # existing table, so a schema change appears to succeed and then fails
        # at runtime with an undefined-column error. Production goes through
        # db/schema.sql or Alembic.
        create_all()
        logger.info("created tables via create_all (development mode)")

    logger.info(
        "service started",
        extra={
            "version": __version__,
            "environment": settings.environment,
            "model": settings.anthropic_model,
            "agent_enabled": settings.agent_enabled and bool(settings.anthropic_api_key),
        },
    )
    if settings.agent_enabled and not settings.anthropic_api_key:
        # Loud, because the failure is otherwise invisible: ingestion keeps
        # working perfectly and no analysis ever appears.
        logger.warning(
            "ANTHROPIC_API_KEY is not set — ingestion will work but no analysis will run"
        )

    yield
    logger.info("service stopping")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory.

    A factory rather than a module-level ``app = FastAPI()`` so tests can build
    an instance with overridden settings instead of mutating global state and
    hoping import order works out.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title="Test Observability + AI Root-Cause Agent",
        version=__version__,
        lifespan=lifespan,
        description=(
            "Ingests test results from Playwright, Cypress, PyTest, and Selenium, "
            "then uses Claude to classify why each failure happened.\n\n"
            "**Ingest** endpoints are what CI calls. **Analysis** endpoints are what "
            "the dashboard calls."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(ingest_routes.router)
    app.include_router(analysis_routes.router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Log the traceback, return a shaped error.

        Two jobs. The traceback goes to the logs where an operator can find it;
        the response says only that something failed, because echoing an
        exception string to an HTTP client leaks table names, file paths, and
        occasionally connection strings.
        """
        logger.exception(
            "unhandled exception",
            extra={"path": request.url.path, "method": request.method},
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "internal server error",
                "path": request.url.path,
            },
        )

    @app.get("/health", tags=["health"], summary="Liveness probe")
    def health() -> dict[str, Any]:
        """Is the process up? Deliberately touches nothing else.

        A liveness probe that checks the database restarts the app when the
        database blips — which fixes nothing and turns a recoverable outage into
        a crash loop. Dependency checks belong in readiness.
        """
        return {"status": "ok", "version": __version__, "environment": settings.environment}

    @app.get("/health/ready", tags=["health"], summary="Readiness probe")
    def readiness() -> JSONResponse:
        """Can this instance actually serve traffic?

        Checks the database (hard dependency — nothing works without it) and
        reports agent availability (soft dependency — ingestion is fully
        functional without it, so a missing key must not fail readiness and pull
        the whole service out of rotation).
        """
        checks: dict[str, Any] = {}
        healthy = True

        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            logger.error("readiness: database unreachable", extra={"error": str(exc)})
            checks["database"] = f"unreachable: {type(exc).__name__}"
            healthy = False

        if not settings.agent_enabled:
            checks["agent"] = "disabled"
        elif not settings.anthropic_api_key:
            checks["agent"] = "degraded: ANTHROPIC_API_KEY not set"
        else:
            checks["agent"] = f"ok ({settings.anthropic_model})"

        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ready" if healthy else "not_ready", "checks": checks},
        )

    return app


app = create_app()
