"""Console-link and Token Plan gateway tests — no real network (MockTransport,
in-process loopback listener on a free port)."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import httpx
import pytest

from app import console_link
from app.providers.alibaba_token_plan import AlibabaTokenPlanProvider
from app.providers.console_gateway import build_result, gateway_for, unwrap_envelope, used_fraction

KEY = "test-api-key-not-a-secret-1234"
TOKEN = "fake-console-access-token-xyz"


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Live app over TestClient with no upstream network access at all."""
    import httpx
    from fastapi.testclient import TestClient
    from app import main

    def refused(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    monkeypatch.setattr(main, "make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(refused)))
    # TestClient sends requests from "testclient" host; bind checks still fine
    monkeypatch.setattr(console_link, "STORE_PATH", str(tmp_path / "link.json"))
    with TestClient(main.app) as test_client:
        yield test_client


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(autouse=True)
def clean_link(monkeypatch, tmp_path):
    port = free_port()
    monkeypatch.setattr(console_link, "LISTEN_PORT", port)
    monkeypatch.setattr(console_link, "ADVERTISE_PORT", port)
    monkeypatch.setattr(console_link, "STORE_PATH", str(tmp_path / "link.json"))
    for name in ("ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN", "ALIBABA_TOKEN_PLAN_REGION"):
        monkeypatch.delenv(name, raising=False)
    console_link.clear_token()
    console_link.link_flow.cancel()
    console_link.link_flow._relay = None
    yield
    console_link.clear_token()
    console_link.link_flow.cancel()
    console_link.link_flow._relay = None


# --- loopback link listener ---------------------------------------------------

def test_link_flow_rejects_wrong_state_and_accepts_matching_one():
    url = console_link.link_flow.start("international")
    assert url and "state=" in url
    state = url.split("state=")[1]
    port = console_link.ADVERTISE_PORT

    def post(payload: dict) -> int:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    assert post({"access_token": "no", "state": "wrong"}) == 403
    assert console_link.console_token() is None
    assert post({"access_token": TOKEN, "state": state}) == 200

    stored = console_link.console_token()
    assert stored and stored["access_token"] == TOKEN

    # one-shot: the state is retired immediately, so a late delivery is rejected
    # even if it races the socket shutdown.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if post({"access_token": "late", "state": state}) == 403:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            break  # listener already gone
    stored = console_link.console_token()
    assert stored and stored["access_token"] == TOKEN  # unchanged


def test_link_url_points_at_loopback_and_official_console():
    url = console_link.link_flow.start("international")
    assert url is not None
    assert url.startswith("https://modelstudio.console.alibabacloud.com/console-login?notice=127.0.0.1:")
    console_link.link_flow.cancel()
    url = console_link.link_flow.start("domestic")
    assert url is not None
    assert url.startswith("https://bailian.console.aliyun.com/console-login?notice=127.0.0.1:")
    console_link.link_flow.cancel()


def test_env_console_token_is_a_source(monkeypatch):
    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_REGION", "china")
    stored = console_link.console_token()
    assert stored is not None
    assert stored["access_token"] == TOKEN
    assert stored["console_region"] == "cn-beijing"
    assert stored["console_site"] == "domestic"


def test_linked_token_never_leaks_to_store_outside_0600_file(tmp_path):
    console_link.link_flow.start("international")
    # deliver directly through the store helper (same code path as the handler)
    console_link._save_store({"access_token": TOKEN, "linked_at": "now"})
    path = tmp_path / "link.json"
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600


# --- gateway routing & parsing --------------------------------------------------

def test_gateway_for_matches_the_cli_routing_table():
    assert gateway_for({"console_region": "ap-southeast-1", "console_site": "international"}) == (
        "bailian-singapore-cs.alibabacloud.com", "IntlBroadScopeAspnGateway")
    assert gateway_for({"console_region": "cn-beijing", "console_site": "domestic"}) == (
        "bailian-cs.console.aliyun.com", "BroadScopeAspnGateway")
    host, action = gateway_for({"console_region": "eu-central-1", "console_site": "international"})
    assert host == "bailian-singapore-cs.alibabacloud.com" and action == "IntlBroadScopeAspnGateway"


def test_used_fraction_accepts_ratios_and_percents():
    assert used_fraction(0.7) == 0.7
    assert used_fraction(70) == 0.7
    assert used_fraction(1.5) == 1.0  # capped
    assert used_fraction(-1) is None
    assert used_fraction(None) is None


def test_unwrap_envelope_peels_the_gateway_layers():
    payload = {"DataV2": {"data": {"data": {"per1WeekPercentage": 0.2}}}}
    assert unwrap_envelope(payload) == {"per1WeekPercentage": 0.2}


def test_build_result_turns_percentages_into_credits_via_quota_config():
    usage = {"per1WeekPercentage": 0.25, "per1WeekResetTime": 1789999999000}
    quota = {"standard": {"weekly": 10000.0}}
    subscription = {"specCode": "standard", "remainingDays": 12, "status": "VALID"}
    result = build_result(usage, quota, subscription, {})

    assert result.ok
    lines = {b.label: b for b in result.balances}
    # minimal card: remaining per window + days left; used amounts only in meta
    assert lines["Credits left (7-day)"].amount == 7500.0
    assert lines["Credits left (7-day)"].primary is True
    assert not any("used" in label.lower() for label in lines)
    assert lines["Plan days left"].amount == 12.0
    assert result.meta["spec"] == "standard"
    assert result.meta["subscription_days_left"] == 12


def test_build_result_without_ceilings_shows_percentages_only():
    result = build_result({"per5HourPercentage": 0.5}, {}, {}, {})
    assert result.ok
    assert result.balances[0].label == "Used (5-hour)"
    assert result.balances[0].amount == 50.0
    assert "percentages only" in (result.note or "")


def test_build_result_without_data_is_a_failure():
    result = build_result({}, {}, {}, {})
    assert not result.ok
    assert "returned nothing" in (result.error or "")


# --- provider integration (linked via the env token path) ------------------------

def _gateway_handler(paths_seen):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.8-flash"}, {"id": "glm-5.2"}]})
        if request.url.path == "/cli/api.json":
            api = str(request.url.params.get("api", ""))
            paths_seen.append(api.rsplit("/", 1)[-1])
            if api.endswith("/usage"):
                return httpx.Response(200, json={"successResponse": True, "data": {"success": True, "data": {"per1WeekPercentage": 0.1}}})
            if api.endswith("/quota-config"):
                return httpx.Response(200, json={"successResponse": True, "data": {"success": True, "data": {"standard": {"weekly": 1000.0}}}})
            if api.endswith("/subscription"):
                return httpx.Response(200, json={"successResponse": True, "data": {"success": True, "data": {"specCode": "standard", "remainingDays": 5, "status": "VALID"}}})
            return httpx.Response(200, json={"successResponse": True, "data": {"success": True, "data": {}}})
        raise AssertionError(f"unexpected request {request.url}")

    return handler


async def test_provider_merges_gateway_quota_with_key_models(monkeypatch):
    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN", TOKEN)
    seen: list[str] = []
    provider = AlibabaTokenPlanProvider(api_key=KEY)
    async with mock_client(_gateway_handler(seen)) as client:
        result = await provider.fetch(client)

    assert result.ok
    labels = [b.label for b in result.balances]
    assert "Credits left (7-day)" in labels
    assert any(b.label.startswith("Models available") for b in result.balances)
    assert result.balances[0].primary is True  # quota leads the card
    assert result.meta["spec"] == "standard"
    assert set(seen) >= {"usage", "quota-config", "subscription"}


async def test_provider_expired_console_session_still_shows_key_data(monkeypatch):
    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN", TOKEN)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"successResponse": True, "data": {"success": False, "errorCode": "NotLogined"}})

    provider = AlibabaTokenPlanProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok  # the key path carries the card
    assert result.meta["console_error"]
    assert "console" in (result.note or "").lower() or "Console" in (result.note or "")


import pathlib
from urllib.parse import parse_qs, urlsplit


# --- relay mode: browser on another machine -------------------------------------

def test_relay_mode_keeps_status_waiting_until_linked_or_expired():
    armed = console_link.link_flow.relay_start("international")
    assert console_link.link_flow.status() == "waiting"
    console_link.link_flow._relay["armed_at"] -= console_link.RELAY_TTL_SECONDS + 1
    assert console_link.link_flow.status() == "idle"
    console_link.link_flow.relay_start("international")
    console_link.link_flow.cancel()


def test_relay_flow_requires_code_and_state():
    armed = console_link.link_flow.relay_start("international")
    code, state = armed["code"], armed["state"]
    assert len(code) == 6 and int(state, 16) >= 0

    # claim with wrong code leaks nothing
    assert console_link.link_flow.relay_claim("ffffff", 8199) is None
    url = console_link.link_flow.relay_claim(code, 8199)
    assert url is not None
    assert url.startswith("https://modelstudio.console.alibabacloud.com/console-login?notice=127.0.0.1:8199?state=" + state)

    # deliver with wrong state rejected; right pair accepted once
    bad = {"access_token": "x", "state": "deadbeef"}
    assert console_link.link_flow.relay_deliver(code, bad["state"], bad) is False
    ok = console_link.link_flow.relay_deliver(code, state, {
        "access_token": TOKEN, "console_site": "international", "console_region": "ap-southeast-1"})
    assert ok is True
    stored = console_link.console_token()
    assert stored and stored["access_token"] == TOKEN and stored["via"] == "relay"
    # attempt spent: a second delivery is refused
    assert console_link.link_flow.relay_deliver(code, state, {"access_token": "late"}) is False


def test_relay_guessed_codes_burn_the_attempt():
    armed = console_link.link_flow.relay_start("international")
    for _ in range(console_link.RELAY_MAX_FAILURES):
        assert console_link.link_flow.relay_claim("bad123", 8199) is None
    # attempt is now dead: the correct code no longer claims
    assert console_link.link_flow.relay_claim(armed["code"], 8199) is None


def test_relay_expiry_ignores_delivery():
    armed = console_link.link_flow.relay_start("international")
    console_link.link_flow._relay["armed_at"] -= console_link.RELAY_TTL_SECONDS + 1
    assert console_link.link_flow.relay_deliver(
        armed["code"], armed["state"], {"access_token": TOKEN}) is False


def test_relay_endpoints_reject_anonymous_traffic(client):
    started = client.post("/api/link/start?relay=true").json()
    assert started["mode"] == "relay" and len(started["code"]) == 6
    assert client.get("/api/link/relay/claim", params={"code": "wrong!", "port": 8199}).json() == {"ok": False}
    assert client.post("/api/link/relay/deliver", json={
        "code": started["code"], "state": "x" * 32, "access_token": TOKEN,
    }).status_code == 403
    # garbage body
    assert client.post("/api/link/relay/deliver", content=b"not json").status_code == 400
    client.post("/api/link/cancel")


def test_relay_script_end_to_end(client):
    """The shipped link-relay.py against the live app: claim, console-URL shape,
    delivery forwarded with state — everything but the browser itself."""
    import importlib.util
    import threading as _t
    import urllib.request as _ur

    spec = importlib.util.spec_from_file_location(
        "link_relay", pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "link-relay.py")
    relay_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay_mod)

    started = client.post("/api/link/start?relay=true").json()

    # stand up a tiny WSGI shim? No — the app is ASGI; call its internals via
    # the same test client by driving the Runner with a fake _fetch.
    runner = relay_mod.Runner("http://dashboard.test", started["code"], 8199)
    calls = []

    def fake_fetch(url, body=None):
        calls.append((url, body))
        parsed = urlsplit(url)
        if parsed.path == "/api/link/relay/claim":
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            url_out = console_link.link_flow.relay_claim(params["code"], int(params["port"]))
            return {"ok": url_out is not None, "url": url_out}
        if parsed.path == "/api/link/relay/deliver":
            payload = json.loads(body)
            ok = console_link.link_flow.relay_deliver(
                payload["code"], payload["state"],
                {k: payload.get(k, "") for k in ("access_token", "console_site", "console_region")})
            return {"ok": ok}
        raise AssertionError(url)

    runner._fetch = fake_fetch
    url = runner.claim()
    assert url and "state=" in url
    state = url.split("state=")[1]
    runner.forward({"access_token": TOKEN, "state": state, "console_site": "international"})
    stored = console_link.console_token()
    assert stored and stored["access_token"] == TOKEN
