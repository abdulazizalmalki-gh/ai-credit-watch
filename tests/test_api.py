"""API tests: registry wiring, key secrecy, caching and optional basic auth."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.main import BalanceCache, app
from app.ratelimit import RefreshLimiter

KEY = "test-api-key-not-a-secret-1234"


@pytest.fixture(autouse=True)
def isolated_keys(monkeypatch, tmp_path):
    """No ambient keys: neither the environment nor any keys file on disk.

    DEFAULT_KEYS_FILES is patched too, otherwise a developer's real .env in the repo
    root would leak into the assertions (CI has no such file, so this keeps the two
    environments honest about the same expectations).
    """
    monkeypatch.setenv("KEYS_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(config, "DEFAULT_KEYS_FILES", (str(tmp_path / "no-keys.env"),))
    for name in (
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_ADMIN_KEY",
        "ANTHROPIC_API_KEY",
        "MOONSHOT_API_KEY",
        "KIMI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "OPENAI_API_KEY",
        "BASIC_AUTH_USER",
        "BASIC_AUTH_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    config._file_keys.cache_clear()
    monkeypatch.setattr(main, "BASIC_AUTH_USER", "")
    monkeypatch.setattr(main, "BASIC_AUTH_PASSWORD", "")
    yield
    config._file_keys.cache_clear()


def handler(request: httpx.Request) -> httpx.Response:
    if "deepseek" in request.url.host:
        return httpx.Response(
            200,
            json={
                "is_available": True,
                "balance_infos": [
                    {"currency": "USD", "total_balance": "42.00", "granted_balance": "0.00", "topped_up_balance": "42.00"}
                ],
            },
        )
    if request.url.path.endswith("/key"):
        return httpx.Response(200, json={"data": {"limit": 10, "limit_remaining": 4.5, "usage": 5.5}})
    return httpx.Response(403, json={})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        main, "make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(main, "cache", BalanceCache(0))
    # a fresh limiter per test: the real one is process-wide state
    monkeypatch.setattr(main, "rate_limiter", RefreshLimiter(0, 0))
    with TestClient(app) as test_client:
        yield test_client


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Credit Watch" in response.text


def test_provider_catalog_lists_every_provider_without_keys(client):
    body = client.get("/api/providers").json()
    ids = [p["id"] for p in body["providers"]]
    assert ids == ["anthropic", "deepseek", "moonshot", "openai", "openrouter", "xiaomi"]
    assert all(p["configured"] is False for p in body["providers"])
    assert all(p["key_hint"] is None for p in body["providers"])
    for provider in body["providers"]:
        assert provider["docs_url"].startswith("https://")
        assert provider["key_env"].isupper()


def test_unconfigured_providers_are_reported_not_fatal(client):
    body = client.get("/api/balances").json()
    assert [p["status"] for p in body["providers"]] == ["unconfigured"] * 6
    assert body["providers"][0]["note"].startswith("Not configured")
    assert body["providers"][0]["balances"] == []


def test_unconfigured_providers_are_hidden_by_default(client):
    """The page filters them out; the API still lists them so the UI can name the env var."""
    balances = client.get("/api/balances").json()
    assert balances["show_unconfigured"] is False
    assert len(balances["providers"]) == 6

    providers = client.get("/api/providers").json()
    assert providers["show_unconfigured"] is False
    assert len(providers["providers"]) == 6


def test_show_unconfigured_can_be_switched_on(client, monkeypatch):
    monkeypatch.setattr(main, "SHOW_UNCONFIGURED", True)
    assert client.get("/api/balances").json()["show_unconfigured"] is True
    assert client.get("/api/providers").json()["show_unconfigured"] is True


def test_configured_providers_return_balances_and_only_masked_keys(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    response = client.get("/api/balances")
    body = response.json()
    by_id = {p["id"]: p for p in body["providers"]}

    assert KEY not in response.text  # the secret itself is never serialised
    assert [p["status"] for p in body["providers"]] == [
        "unconfigured",
        "ok",
        "unconfigured",
        "unconfigured",
        "ok",
        "unconfigured",
    ]
    assert [p["key_hint"] for p in body["providers"]] == [None, "…1234", None, None, "…1234", None]

    deepseek = by_id["deepseek"]
    assert deepseek["balances"][0]["amount"] == 42.0
    assert deepseek["balances"][0]["primary"] is True

    openrouter = by_id["openrouter"]
    assert openrouter["balances"][0]["amount"] == 4.5
    # this provider only has a normal key: the credits endpoint 403s
    assert openrouter["meta"]["account_credits_available"] is False


def test_provider_errors_do_not_break_the_dashboard(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)

    def broken(request: httpx.Request) -> httpx.Response:
        if "deepseek" in request.url.host:
            return httpx.Response(500, text="upstream exploded")
        return httpx.Response(401, json={})

    monkeypatch.setattr(main, "make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(broken)))
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    body = client.get("/api/balances").json()
    statuses = {p["id"]: p for p in body["providers"]}
    assert statuses["deepseek"]["ok"] is False
    assert "500" in statuses["deepseek"]["error"]
    assert statuses["openrouter"]["ok"] is False
    assert "Unauthorized" in statuses["openrouter"]["error"]


def test_results_are_cached_within_ttl(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    monkeypatch.setattr(main, "cache", BalanceCache(60))
    first = client.get("/api/balances").json()
    second = client.get("/api/balances").json()
    assert first["cached"] is False
    assert second["cached"] is True
    forced = client.get("/api/balances?refresh=true").json()
    assert forced["cached"] is False


def test_basic_auth_when_enabled(client, monkeypatch):
    monkeypatch.setattr(main, "BASIC_AUTH_USER", "admin")
    monkeypatch.setattr(main, "BASIC_AUTH_PASSWORD", "hunter2")

    assert client.get("/api/balances").status_code == 401
    assert client.get("/healthz").status_code == 200  # health stays open for container checks
    ok = client.get("/api/balances", auth=("admin", "hunter2"))
    assert ok.status_code == 200
    assert client.get("/api/balances", auth=("admin", "wrong")).status_code == 401
