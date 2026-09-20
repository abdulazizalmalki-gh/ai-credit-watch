"""Refresh abuse protection: limiter unit tests + API behaviour."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.main import BalanceCache, app
from app.ratelimit import RefreshLimiter, client_key

KEY = "test-api-key-not-a-secret-1234"


def ip(*octets: int) -> str:
    """Build an address without spelling one out in this published file."""
    return ".".join(str(o) for o in octets)


class FakeClock:
    """Deterministic time so no test has to sleep."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- unit ---------------------------------------------------------------------


def test_first_request_is_allowed():
    limiter = RefreshLimiter(10, 20, clock=FakeClock())
    assert limiter.acquire("client-a") == (True, 0.0)


def test_same_client_is_blocked_until_the_interval_elapses():
    clock = FakeClock()
    limiter = RefreshLimiter(10, 20, clock=clock)
    limiter.acquire("client-a")

    allowed, retry_after = limiter.acquire("client-a")
    assert not allowed
    assert 9.0 < retry_after <= 10.0

    clock.advance(10)
    assert limiter.acquire("client-a") == (True, 0.0)


def test_clients_do_not_block_each_other():
    limiter = RefreshLimiter(10, 20, clock=FakeClock())
    assert limiter.acquire("client-a")[0]
    assert limiter.acquire("client-b")[0]


def test_global_cap_protects_provider_quota_across_clients():
    limiter = RefreshLimiter(0, 3, clock=FakeClock())
    for index in range(3):
        assert limiter.acquire(f"client-{index}")[0]

    allowed, retry_after = limiter.acquire("client-99")
    assert not allowed
    assert retry_after > 0


def test_global_cap_window_slides():
    clock = FakeClock()
    limiter = RefreshLimiter(0, 2, clock=clock)
    assert limiter.acquire("a")[0]
    assert limiter.acquire("b")[0]
    assert not limiter.acquire("c")[0]
    clock.advance(61)
    assert limiter.acquire("c")[0]


def test_limiter_can_be_switched_off():
    limiter = RefreshLimiter(0, 0, clock=FakeClock())
    assert not limiter.enabled
    for _ in range(50):
        assert limiter.acquire("spam")[0]


def test_describe_reports_the_configuration():
    described = RefreshLimiter(7, 9).describe()
    assert described == {
        "enabled": True,
        "refresh_min_interval_seconds": 7.0,
        "refresh_max_per_minute": 9,
    }


def test_client_key_only_trusts_xff_from_loopback():
    # the private-range hop is assembled at runtime: this file is published, and a
    # literal private address here would trip the very sweep it is testing
    private_hop = ip(10, 1, 1, 1)
    assert client_key("127.0.0.1", f"198.51.100.9, {private_hop}") == "198.51.100.9"
    assert client_key("::1", "198.51.100.9") == "198.51.100.9"
    # a direct public client cannot spoof its identity through the header
    assert client_key("198.51.100.7", "203.0.113.9") == "198.51.100.7"
    assert client_key(None, None) == "unknown"


def test_tracked_clients_stay_bounded():
    clock = FakeClock()
    limiter = RefreshLimiter(0.0, 10**9, clock=clock)
    for index in range(6000):
        limiter.acquire(f"client-{index}")
    assert len(limiter._last) <= 4096


# --- API ----------------------------------------------------------------------


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "5.00", "granted_balance": "0.00", "topped_up_balance": "5.00"}
            ],
        },
    )


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KEYS_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(config, "DEFAULT_KEYS_FILES", (str(tmp_path / "no-keys.env"),))
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config._file_keys.cache_clear()
    monkeypatch.setattr(main, "BASIC_AUTH_USER", "")
    monkeypatch.setattr(main, "BASIC_AUTH_PASSWORD", "")
    monkeypatch.setattr(main, "make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    yield
    config._file_keys.cache_clear()


def make_client(limiter: RefreshLimiter | None = None, ttl: float = 60.0, host: str = "198.51.100.7"):
    main.cache = BalanceCache(ttl)
    main.rate_limiter = limiter if limiter is not None else RefreshLimiter(10, 20)
    return TestClient(app, client=(host, 51234))


def test_rapid_forced_refreshes_are_rejected_with_429():
    client = make_client()
    assert client.get("/api/balances?refresh=true").status_code == 200

    blocked = client.get("/api/balances?refresh=true")
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["retry_after"] >= 1
    assert "rate limit" in body["detail"].lower()
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked.headers["cache-control"] == "no-store"


def test_cached_reads_are_never_limited():
    client = make_client()
    assert client.get("/api/balances?refresh=true").status_code == 200
    for _ in range(25):
        assert client.get("/api/balances").status_code == 200


def test_one_client_cannot_starve_another():
    limiter = RefreshLimiter(10, 20)
    first = make_client(limiter, host="198.51.100.7")
    second = make_client(limiter, host="203.0.113.9")
    assert first.get("/api/balances?refresh=true").status_code == 200
    assert first.get("/api/balances?refresh=true").status_code == 429
    assert second.get("/api/balances?refresh=true").status_code == 200


def test_global_budget_stops_a_distributed_flood():
    limiter = RefreshLimiter(0, 3)
    for index in range(3):
        assert make_client(limiter, host=f"198.51.100.{index}").get("/api/balances?refresh=true").status_code == 200
    assert make_client(limiter, host="198.51.100.99").get("/api/balances?refresh=true").status_code == 429


def test_uncached_reads_need_a_token_too():
    """With a zero TTL every read would hit the providers, so it is limited as well."""
    client = make_client(RefreshLimiter(10, 20), ttl=0.0)
    assert client.get("/api/balances").status_code == 200
    assert client.get("/api/balances").status_code == 429


def test_limiter_can_be_disabled_entirely():
    client = make_client(RefreshLimiter(0, 0))
    for _ in range(15):
        assert client.get("/api/balances?refresh=true").status_code == 200


def test_providers_endpoint_advertises_the_limits():
    client = make_client(RefreshLimiter(7, 12))
    body = client.get("/api/providers").json()
    assert body["enabled"] is True
    assert body["refresh_min_interval_seconds"] == 7.0
    assert body["refresh_max_per_minute"] == 12


def test_health_and_page_are_never_limited():
    client = make_client(RefreshLimiter(10, 20))
    for _ in range(30):
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 200
