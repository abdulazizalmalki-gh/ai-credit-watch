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
    yield
    console_link.clear_token()
    console_link.link_flow.cancel()


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
    assert lines["Credits remaining (7-day)"].amount == 7500.0
    assert lines["Credits remaining (7-day)"].primary is True
    assert lines["Credits used (7-day)"].amount == 2500.0
    assert result.meta["spec"] == "standard"
    assert result.meta["subscription_days_left"] == 12
    assert "Plan: standard, 12 day(s) left." in result.note


def test_build_result_without_ceilings_shows_percentages_only():
    result = build_result({"per5HourPercentage": 0.5}, {}, {}, {})
    assert result.ok
    assert result.balances[0].label == "Window used (5-hour)"
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
    assert "Credits remaining (7-day)" in labels
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
