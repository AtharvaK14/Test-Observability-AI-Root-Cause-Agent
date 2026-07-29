"""Application configuration, loaded from environment / ``.env``.

All tunables live here rather than being read from ``os.environ`` at their point
of use, so that a test can construct a ``Settings`` instance with overrides and
inject it, and so ``.env.example`` can stay an accurate inventory of knobs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Field names map to upper-case env vars (``DATABASE_URL``)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Service ------------------------------------------------------------
    app_name: str = "test-observability-agent"
    environment: Literal["local", "ci", "staging", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = Field(
        default=False,
        description="Emit structured JSON logs. Off locally (unreadable), on in prod.",
    )

    # --- Database -----------------------------------------------------------
    # psycopg 3 driver ("+psycopg"), not psycopg2: it ships wheels for newer
    # CPython releases and is the driver SQLAlchemy 2.x treats as current.
    database_url: str = "postgresql+psycopg://tobs:tobs@localhost:5432/test_observability"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_pre_ping: bool = Field(
        default=True,
        description=(
            "Probe connections before use. CI traffic is bursty, so pooled "
            "connections routinely sit idle long enough for Postgres or an "
            "intervening proxy to drop them; without pre-ping the first request "
            "after a quiet period fails with a stale-connection error."
        ),
    )
    db_echo: bool = False

    # --- Anthropic / agent --------------------------------------------------
    anthropic_api_key: str | None = None
    anthropic_model: str = Field(
        default="claude-opus-5",
        description=(
            "Root-cause triage is a reasoning task, so the default is the "
            "strongest model. 'claude-sonnet-5' is the cost/latency step-down "
            "if you are classifying at very high volume."
        ),
    )
    anthropic_max_tokens: int = Field(
        default=16000,
        description=(
            "Caps thinking AND response text together. Thinking is on by "
            "default on Opus 5, so a budget sized only for the answer truncates "
            "mid-verdict. 16000 also keeps non-streaming requests inside the "
            "SDK's HTTP timeout."
        ),
    )
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    anthropic_timeout_seconds: float = 300.0
    anthropic_max_retries: int = 3
    anthropic_enable_refusal_fallback: bool = Field(
        default=True,
        description=(
            "Let the API re-serve a safety-declined request on a fallback "
            "model. Not paranoia: a security suite legitimately ships tests "
            "named things like test_sql_injection_blocked whose error text "
            "contains attack payloads, and that can trip a classifier. Without "
            "this, those failures silently never get analysed."
        ),
    )
    agent_max_iterations: int = Field(
        default=4,
        description="Upper bound on agentic tool-use turns before forcing a verdict.",
    )
    agent_enabled: bool = Field(
        default=True,
        description="Kill switch. When false, ingestion still works; no analysis runs.",
    )
    analysis_mode: Literal["claude", "heuristic"] = Field(
        default="claude",
        description=(
            "Which analyzer classifies failures. 'heuristic' uses deterministic "
            "rules — no API key, no network, no cost — and doubles as the "
            "baseline the LLM's accuracy is measured against. 'claude' uses the "
            "agent. Verdicts from both are stored with distinct prompt_version "
            "values so their accuracies can be computed separately."
        ),
    )

    # --- Analysis behaviour -------------------------------------------------
    auto_analyze_on_ingest: bool = Field(
        default=True,
        description="Queue root-cause analysis for failures as soon as they are ingested.",
    )
    analysis_confidence_review_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description=(
            "Analyses below this confidence are flagged for human review. Tuned "
            "against the feedback table: if humans keep marking high-confidence "
            "analyses incorrect, this is the dial to turn."
        ),
    )
    history_window_runs: int = Field(
        default=30, description="How many prior runs of a test define its 'recent' behaviour."
    )
    git_repo_path: str | None = Field(
        default=None,
        description=(
            "Optional path to a local checkout of the system under test. When "
            "set, the retriever enriches failures with real commit metadata. "
            "When unset it reports git context as unavailable — it never "
            "fabricates changed-file lists, because invented evidence is worse "
            "than absent evidence once a model reasons over it."
        ),
    )
    context_max_stack_chars: int = 4000
    context_max_log_chars: int = 6000
    max_ingest_bytes: int = Field(
        default=50 * 1024 * 1024,
        description="Reject oversized uploads before parsing. Playwright traces get large.",
    )

    # --- API ----------------------------------------------------------------
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    @field_validator("database_url")
    @classmethod
    def _reject_bare_postgres_scheme(cls, value: str) -> str:
        """Fail fast on ``postgres://`` URLs.

        Heroku-style ``postgres://`` is not a scheme SQLAlchemy recognises, and
        the resulting error surfaces at first query rather than at startup. We
        normalise instead of exploding, but keep the driver explicit.
        """
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        return value

    @property
    def is_sqlite(self) -> bool:
        """True when running against SQLite (the unit-test path)."""
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached because ``Settings()`` re-reads and re-parses ``.env`` on every
    instantiation. Tests override it with ``app.dependency_overrides`` or by
    calling ``get_settings.cache_clear()``.
    """
    return Settings()
