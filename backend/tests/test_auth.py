"""API key authentication, and the startup guard that makes it mandatory when deployed."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from backend.config import Settings, get_settings
from backend.db.session import create_all, drop_all, init_engine
from backend.main import create_app

KEY = "s3cret-test-key"
FORM = {"ci_run_id": "run-auth", "environment": "staging"}


@pytest.fixture
def build_client(settings: Settings) -> Iterator[Callable[..., TestClient]]:
    """Build a client whose app runs on ``settings`` with the given overrides.

    ``model_copy`` skips validation, so this can also build a prod-config app
    without going through the API_KEY guard; the guard has its own tests below.
    """
    clients: list[TestClient] = []

    def _build(**overrides: Any) -> TestClient:
        configured = settings.model_copy(update=overrides)
        app = create_app(configured)
        app.dependency_overrides[get_settings] = lambda: configured
        init_engine(configured, force=True)
        create_all()
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _build
    for client in clients:
        client.__exit__(None, None, None)
    drop_all()


def ingest(client: TestClient, fixture_bytes: Any, headers: dict[str, str] | None = None) -> Any:
    return client.post(
        "/ingest/pytest",
        files={"file": ("r.json", fixture_bytes("pytest-report.json"))},
        data=FORM,
        headers=headers or {},
    )


class TestNoKeyConfigured:
    def test_routes_stay_open_locally(self, client: TestClient, fixture_bytes) -> None:
        """Local and ci keep working without a key, as they did before auth existed."""
        assert ingest(client, fixture_bytes).status_code == 200
        assert client.get("/api/analysis/summary").status_code == 200


class TestKeyConfigured:
    @pytest.fixture
    def authed(self, build_client) -> TestClient:
        return build_client(api_key=SecretStr(KEY))

    def test_missing_header_is_401(self, authed: TestClient, fixture_bytes) -> None:
        response = ingest(authed, fixture_bytes)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "ApiKey"
        assert authed.get("/api/analysis/summary").status_code == 401

    def test_wrong_key_is_401(self, authed: TestClient, fixture_bytes) -> None:
        assert ingest(authed, fixture_bytes, {"X-API-Key": "nope"}).status_code == 401
        response = authed.get("/api/analysis/summary", headers={"X-API-Key": KEY + "x"})
        assert response.status_code == 401

    def test_non_ascii_key_is_401_not_500(self, authed: TestClient) -> None:
        """``compare_digest`` on str raises for non-ASCII; comparing bytes avoids it."""
        response = authed.get("/api/analysis/summary", headers={b"X-API-Key": "café".encode()})
        assert response.status_code == 401

    def test_correct_key_reaches_ingest_and_analysis(
        self, authed: TestClient, fixture_bytes
    ) -> None:
        headers = {"X-API-Key": KEY}
        assert ingest(authed, fixture_bytes, headers).status_code == 200
        summary = authed.get("/api/analysis/summary", headers=headers)
        assert summary.status_code == 200

    def test_health_probes_need_no_key(self, authed: TestClient) -> None:
        """The load balancer and the deploy smoke test call these unauthenticated."""
        assert authed.get("/health").status_code == 200
        ready = authed.get("/health/ready")
        assert ready.status_code == 200
        assert KEY not in ready.text

    def test_401_body_does_not_echo_the_key(self, authed: TestClient) -> None:
        response = authed.get("/api/analysis/summary", headers={"X-API-Key": "guess"})
        assert KEY not in response.text
        assert "guess" not in response.text


class TestStartupGuard:
    @pytest.mark.parametrize("environment", ["staging", "prod"])
    def test_deployed_environment_without_key_refuses_to_start(self, environment) -> None:
        with pytest.raises(ValidationError, match="API_KEY must be set"):
            Settings(environment=environment, api_key=None)

    @pytest.mark.parametrize("environment", ["staging", "prod"])
    def test_empty_key_counts_as_missing(self, environment) -> None:
        with pytest.raises(ValidationError, match="API_KEY must be set"):
            Settings(environment=environment, api_key=SecretStr(""))

    def test_deployed_environment_with_key_starts(self) -> None:
        configured = Settings(environment="prod", api_key=SecretStr(KEY))
        assert configured.api_key is not None
        assert configured.api_key.get_secret_value() == KEY

    @pytest.mark.parametrize("environment", ["local", "ci"])
    def test_key_is_optional_locally(self, environment) -> None:
        assert Settings(environment=environment, api_key=None).api_key is None

    def test_key_is_masked_in_repr(self) -> None:
        """A settings object that ends up in a log line must not carry the key."""
        configured = Settings(environment="prod", api_key=SecretStr(KEY))
        assert KEY not in repr(configured)
        assert KEY not in str(configured.model_dump())


class TestDocs:
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_docs_are_served_locally(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize("environment", ["staging", "prod"])
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_docs_are_off_when_deployed(self, build_client, environment: str, path: str) -> None:
        deployed = build_client(environment=environment, api_key=SecretStr(KEY))
        assert deployed.get(path).status_code == 404
        assert deployed.get(path, headers={"X-API-Key": KEY}).status_code == 404


class TestSchemaBootstrap:
    """Which startups run create_all(). Lives here because it shares the
    deployed-config fixtures; the decision is the only part worth testing."""

    @pytest.mark.parametrize("environment", ["local", "ci"])
    def test_development_always_creates(self, settings: Settings, environment: str) -> None:
        from backend.main import schema_bootstrap_reason

        configured = settings.model_copy(update={"environment": environment})
        assert schema_bootstrap_reason(configured) == "development mode"

    def test_prod_skips_by_default(self, settings: Settings) -> None:
        from backend.main import schema_bootstrap_reason

        configured = settings.model_copy(update={"environment": "prod"})
        assert schema_bootstrap_reason(configured) is None

    def test_prod_creates_when_opted_in(self, settings: Settings) -> None:
        from backend.main import schema_bootstrap_reason

        configured = settings.model_copy(
            update={"environment": "prod", "create_schema_on_start": True}
        )
        assert schema_bootstrap_reason(configured) == "CREATE_SCHEMA_ON_START"

    def test_flag_parses_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CREATE_SCHEMA_ON_START", "true")
        assert Settings().create_schema_on_start is True
