"""Provider parsing tests — no real network calls (httpx.MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import KIND_USED
from app.providers.deepseek import DeepSeekProvider
from app.providers.openrouter import OpenRouterProvider

KEY = "test-api-key-not-a-secret-1234"


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


DEEPSEEK_MODELS = {
    "object": "list",
    "data": [
        {"id": "deepseek-flash", "object": "model", "name": "DeepSeek-V4.1-Flash",
         "context_window": 1048576, "max_output_tokens": 393216,
         "input_modalities": ["text", "image"]},
        {"id": "deepseek-v4-pro", "object": "model", "name": "DeepSeek-V4-Pro",
         "context_window": 1048576, "max_output_tokens": 393216,
         "input_modalities": ["text"]},
    ],
}


def deepseek_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == f"Bearer {KEY}"
    if request.url.path == "/models":
        return httpx.Response(200, json=DEEPSEEK_MODELS)
    assert request.url.path == "/user/balance"
    return httpx.Response(
        200,
        json={
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "110.00",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.00",
                }
            ],
        },
    )


async def test_deepseek_parses_balances():
    provider = DeepSeekProvider(api_key=KEY)
    async with mock_client(deepseek_handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    primary = [b for b in result.balances if b.primary]
    assert len(primary) == 1
    assert primary[0].amount == 110.0
    assert primary[0].currency == "USD"
    assert primary[0].label == "Total balance"
    labels = {b.label: b.amount for b in result.balances}
    assert labels["Granted (unexpired)"] == 10.0
    assert labels["Topped up"] == 100.0
    assert result.meta["usable"] is True
    # model context lines ride along when /models answers
    assert labels["Models callable by this key"] == 2.0
    flash = next(b for b in result.balances if b.label == "deepseek-flash")
    assert "context 1,048,576" in flash.note
    assert "max output 393,216" in flash.note
    assert "+image" in flash.note  # modalities beyond text-only get shown
    pro = next(b for b in result.balances if b.label == "deepseek-v4-pro")
    assert "image" not in pro.note


async def test_deepseek_survives_models_failure():
    """Balance is the contract; a broken /models must not sink the card."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/models":
            raise httpx.ConnectError("boom", request=request)
        return deepseek_handler(request)

    provider = DeepSeekProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)
    assert result.ok
    assert [b for b in result.balances if b.label == "Total balance"][0].amount == 110.0
    assert not any("Models callable" in b.label for b in result.balances)


async def test_deepseek_unauthorized():
    provider = DeepSeekProvider(api_key=KEY)
    async with mock_client(lambda request: httpx.Response(401, json={"error": "no"})) as client:
        result = await provider.fetch(client)
    assert not result.ok
    assert "Unauthorized" in (result.error or "")


async def test_deepseek_network_error_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider = DeepSeekProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)
    assert not result.ok
    assert "Network error" in (result.error or "")


async def test_openrouter_with_management_key_reports_account_credits():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/key"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "label": "prod",
                        "limit": 20,
                        "limit_remaining": 15,
                        "usage": 5,
                        "usage_daily": 0.4,
                        "usage_weekly": 1.5,
                        "usage_monthly": 3.0,
                        "is_free_tier": False,
                    }
                },
            )
        return httpx.Response(200, json={"data": {"total_credits": 100.0, "total_usage": 25.0}})

    provider = OpenRouterProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    primary = [b for b in result.balances if b.primary]
    assert len(primary) == 1
    assert primary[0].label == "Account credits remaining"
    assert primary[0].amount == 75.0
    assert result.meta["account_credits_available"] is True
    assert result.meta["usage_daily"] == 0.4


async def test_openrouter_without_management_key_falls_back_to_key_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/key"):
            return httpx.Response(
                200,
                json={"data": {"limit": 20, "limit_remaining": 12.5, "usage": 7.5, "is_free_tier": False}},
            )
        return httpx.Response(403, json={"error": {"message": "management key required"}})

    provider = OpenRouterProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    primary = [b for b in result.balances if b.primary]
    assert primary[0].label == "Remaining on this key"
    assert primary[0].amount == 12.5
    assert result.meta["account_credits_available"] is False


async def test_openrouter_unlimited_key_gets_a_note():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"limit": None, "limit_remaining": None, "usage": 3.25}})
        return httpx.Response(403, json={})

    provider = OpenRouterProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note and "No credit cap" in result.note
    used = [b for b in result.balances if b.kind == KIND_USED]
    assert used and used[0].amount == 3.25


async def test_openrouter_rejects_bad_key():
    provider = OpenRouterProvider(api_key=KEY)
    async with mock_client(lambda request: httpx.Response(401, json={})) as client:
        result = await provider.fetch(client)
    assert not result.ok
    assert "Unauthorized" in (result.error or "")


# --- OpenAI -------------------------------------------------------------------
# Admin keys are the only OpenAI keys that can read billing, and they are usually
# denied on /v1/models — the regression this set guards against.
# Fixtures avoid long sk- shaped strings: the privacy sweep flags those.

ADMIN_KEY = "sk-admin-xyz"


def costs_payload(today_value: float, yesterday_value: float) -> dict:
    from datetime import datetime, timezone

    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = int(midnight.timestamp())

    def bucket(start: int, value: float) -> dict:
        return {
            "object": "bucket",
            "start_time": start,
            "end_time": start + 86400,
            "results": [
                {"object": "organization.costs.result", "amount": {"value": value, "currency": "usd"}}
            ],
        }

    return {"object": "page", "data": [bucket(today_ts, today_value), bucket(today_ts - 86400, yesterday_value)]}


def usage_payload(input_tokens: int, output_tokens: int, requests: int) -> dict:
    return {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": 0,
                "end_time": 86400,
                "results": [
                    {
                        "object": "organization.usage.completions.result",
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "num_model_requests": requests,
                    }
                ],
            }
        ],
    }


def models_ok(count: int = 2) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": f"model-{i}"} for i in range(count)]})


MODELS_FORBIDDEN = httpx.Response(
    403,
    json={"error": {"message": "You have insufficient permissions for this operation. Missing scopes: api.model.read."}},
)


def openai_handler(*, costs: httpx.Response, models: httpx.Response, usage: httpx.Response | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/organization/costs"):
            return costs
        if path.endswith("/organization/usage/completions"):
            return usage or httpx.Response(404, json={"error": {"message": "no usage"}})
        if path.endswith("/models"):
            return models
        return httpx.Response(404, json={"error": {"message": "unexpected path"}})

    return handler


async def test_openai_admin_key_reports_spend_and_usage():
    from app.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=ADMIN_KEY)
    handler = openai_handler(
        costs=httpx.Response(200, json=costs_payload(1.25, 0.75)),
        models=MODELS_FORBIDDEN,
        usage=httpx.Response(200, json=usage_payload(1000, 500, 3)),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok, result.error
    assert result.error is None
    primary = [b for b in result.balances if b.primary]
    assert len(primary) == 1
    assert primary[0].label == "Spend (last 30 days)"
    assert primary[0].amount == 2.0
    assert primary[0].currency == "USD"

    labels = {b.label: b.amount for b in result.balances}
    assert labels["Spend today (UTC)"] == 1.25
    assert labels["Average per day"] == round(2.0 / 30, 4)
    assert labels["Tokens (last 30 days)"] == 1500.0
    assert labels["Model requests"] == 3.0
    assert "Models visible" not in labels  # admin keys cannot read /v1/models
    assert result.meta["billing_available"] is True
    assert result.meta["key_type"] == "admin"
    assert result.note is None


async def test_openai_admin_key_is_not_rejected_by_the_models_endpoint():
    """Regression: a 403 from /v1/models must not turn a working admin key into an error."""
    from app.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=ADMIN_KEY)
    handler = openai_handler(costs=httpx.Response(200, json=costs_payload(0.0, 0.0)), models=MODELS_FORBIDDEN)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert not result.error
    assert "Unauthorized" not in (result.note or "")
    assert result.meta["billing_available"] is True


async def test_openai_zero_spend_says_it_is_usage_not_credit():
    """A bare $0.00 reads as "no credit" — the card must explain the difference."""
    from app.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=ADMIN_KEY)
    handler = openai_handler(
        costs=httpx.Response(200, json=costs_payload(0.0, 0.0)),
        models=MODELS_FORBIDDEN,
        usage=httpx.Response(200, json=usage_payload(0, 0, 0)),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note, "a zero-spend admin card must carry a note"
    assert "No spend" in result.note
    assert "credit balance" in result.note
    assert "session key" in result.note


async def test_openai_with_spend_has_no_balance_note():
    from app.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=ADMIN_KEY)
    handler = openai_handler(
        costs=httpx.Response(200, json=costs_payload(0.4, 0.1)),
        models=MODELS_FORBIDDEN,
        usage=httpx.Response(200, json=usage_payload(10, 10, 1)),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note is None


async def test_openai_reports_a_model_count_when_the_key_can_read_models():
    from app.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=ADMIN_KEY)
    handler = openai_handler(costs=httpx.Response(200, json=costs_payload(0.5, 0.0)), models=models_ok(7))
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    labels = {b.label: b.amount for b in result.balances}
    assert labels["Models visible"] == 7.0
    assert result.meta["models_visible"] == 7


async def test_openai_project_key_explains_that_billing_needs_an_admin_key():
    from app.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=KEY)
    handler = openai_handler(costs=httpx.Response(403, json={"error": {"message": "denied"}}), models=models_ok(2))
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok  # the key works, only billing is out of reach
    assert result.note and "OPENAI_ADMIN_KEY" in result.note
    assert result.meta["billing_available"] is False
    assert result.meta["key_type"] == "project"
    assert not [b for b in result.balances if b.primary]
    labels = {b.label: b.amount for b in result.balances}
    assert labels["Models visible to this key"] == 2.0


async def test_openai_rejects_a_key_that_both_endpoints_refuse():
    from app.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=KEY)
    handler = openai_handler(
        costs=httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}}),
        models=httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-***"}}),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert not result.ok
    assert "Unauthorized" in (result.error or "")
    assert result.meta["costs_status"] == 401
    assert result.meta["models_status"] == 401


async def test_openai_rate_limited_costs_endpoint_is_not_a_failure():
    from app.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key=ADMIN_KEY)
    handler = openai_handler(costs=httpx.Response(429, text="slow down"), models=MODELS_FORBIDDEN)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note and "rate limited" in result.note.lower()
    assert result.meta["costs_status"] == 429


async def test_openai_reports_network_errors():
    from app.providers.openai import OpenAIProvider

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider = OpenAIProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)
    assert not result.ok
    assert "Network error" in (result.error or "")


async def test_openai_prefers_the_admin_key_when_both_are_set(monkeypatch, tmp_path):
    from app import config
    from app.providers.openai import OpenAIProvider

    monkeypatch.setenv("KEYS_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(config, "DEFAULT_KEYS_FILES", (str(tmp_path / "none.env"),))
    config._file_keys.cache_clear()
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", ADMIN_KEY)
    assert config.get_key(OpenAIProvider.env_keys) == ADMIN_KEY
    config._file_keys.cache_clear()


# --- Anthropic -----------------------------------------------------------------
# Admin credentials are separate from standard keys, cost arrives as a decimal
# string in CENTS, and the auth header is x-api-key + anthropic-version.

ANTHROPIC_ADMIN_KEY = "sk-ant-admin01-xyz"


def anthropic_costs_payload(today_cents: str, yesterday_cents: str) -> dict:
    from datetime import datetime, timezone

    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = midnight.isoformat().replace("+00:00", "Z")
    yesterday_iso = (midnight.replace(hour=0) - __import__("datetime").timedelta(days=1)).isoformat().replace("+00:00", "Z")

    def bucket(start: str, cents: str) -> dict:
        return {
            "starting_at": start,
            "ending_at": start,
            "results": [
                {
                    "amount": cents,
                    "currency": "USD",
                    "cost_type": "tokens",
                    "model": "claude-sonnet",
                    "service_tier": "standard",
                }
            ],
        }

    return {"data": [bucket(today_iso, today_cents), bucket(yesterday_iso, yesterday_cents)], "has_more": False}


def anthropic_usage_payload(uncached: int, output: int, cache_read: int = 0) -> dict:
    return {
        "data": [
            {
                "starting_at": "2026-01-01T00:00:00Z",
                "ending_at": "2026-01-02T00:00:00Z",
                "results": [
                    {
                        "uncached_input_tokens": uncached,
                        "output_tokens": output,
                        "cache_read_input_tokens": cache_read,
                        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
                        "model": "claude-sonnet",
                        "service_tier": "standard",
                    }
                ],
            }
        ],
        "has_more": False,
    }


def anthropic_handler(*, costs: httpx.Response, models: httpx.Response, usage: httpx.Response | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/organizations/cost_report"):
            return costs
        if path.endswith("/organizations/usage_report/messages"):
            return usage or httpx.Response(404, json={"error": {"message": "no usage"}})
        if path.endswith("/models"):
            return models
        return httpx.Response(404, json={"error": {"message": "unexpected path"}})

    return handler


async def test_anthropic_admin_key_reports_spend_converted_from_cents():
    from app.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key=ANTHROPIC_ADMIN_KEY)
    handler = anthropic_handler(
        costs=httpx.Response(200, json=anthropic_costs_payload("123.78912", "100")),
        models=httpx.Response(403, json={"error": {"message": "admin keys cannot list models"}}),
        usage=httpx.Response(200, json=anthropic_usage_payload(1000, 500, 250)),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok, result.error
    primary = [b for b in result.balances if b.primary]
    assert primary and primary[0].label == "Spend (last 30 days)"
    # "123.78912" cents -> $1.2379 today, "100" cents -> $1.00 yesterday
    assert primary[0].amount == round(1.2378912 + 1.0, 4)
    assert primary[0].currency == "USD"

    labels = {b.label: b.amount for b in result.balances}
    assert labels["Spend today (UTC)"] == round(1.2378912, 4)
    assert labels["Tokens (last 30 days)"] == 1500.0
    assert labels["Cache reads / writes"] == 250.0
    assert result.meta["key_type"] == "admin"
    assert result.meta["billing_available"] is True
    assert result.note is None


async def test_anthropic_cost_strings_are_cents_not_dollars():
    """Regression guard for the unit: "100" USD must be $1.00, not $100."""
    from app.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key=ANTHROPIC_ADMIN_KEY)
    handler = anthropic_handler(
        costs=httpx.Response(200, json=anthropic_costs_payload("100", "0")),
        models=httpx.Response(403, json={}),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    primary = [b for b in result.balances if b.primary][0]
    assert primary.amount == 1.0


async def test_anthropic_sends_its_own_auth_headers():
    from app.providers.anthropic import AnthropicProvider

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=anthropic_costs_payload("0", "0"))

    provider = AnthropicProvider(api_key=ANTHROPIC_ADMIN_KEY)
    async with mock_client(handler) as client:
        await provider.fetch(client)

    assert seen.get("x-api-key") == ANTHROPIC_ADMIN_KEY
    assert seen.get("anthropic-version") == "2023-06-01"


async def test_anthropic_zero_spend_explains_the_missing_balance_endpoint():
    from app.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key=ANTHROPIC_ADMIN_KEY)
    handler = anthropic_handler(
        costs=httpx.Response(200, json=anthropic_costs_payload("0", "0")),
        models=httpx.Response(403, json={}),
        usage=httpx.Response(200, json=anthropic_usage_payload(0, 0)),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note and "No spend" in result.note
    assert "credit-balance endpoint" in result.note


async def test_anthropic_standard_key_is_told_to_use_an_admin_key():
    from app.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key=KEY)
    handler = anthropic_handler(
        costs=httpx.Response(403, json={"error": {"message": "admin credential required"}}),
        models=models_ok(5),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note and "ANTHROPIC_ADMIN_KEY" in result.note
    assert result.meta["key_type"] == "standard"
    assert result.meta["billing_available"] is False
    labels = {b.label: b.amount for b in result.balances}
    assert labels["Models visible to this key"] == 5.0


async def test_anthropic_rejects_a_key_both_endpoints_refuse():
    from app.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key=KEY)
    handler = anthropic_handler(
        costs=httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}),
        models=httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}),
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert not result.ok
    assert "Unauthorized" in (result.error or "")
    assert result.meta["models_status"] == 401


async def test_anthropic_rate_limited_cost_report_is_not_a_failure():
    from app.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key=ANTHROPIC_ADMIN_KEY)
    handler = anthropic_handler(costs=httpx.Response(429, text="slow down"), models=httpx.Response(403, json={}))
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note and "rate limited" in result.note.lower()


async def test_anthropic_reports_network_errors():
    from app.providers.anthropic import AnthropicProvider

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider = AnthropicProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)
    assert not result.ok
    assert "Network error" in (result.error or "")


def test_anthropic_prefers_the_admin_key(monkeypatch, tmp_path):
    from app import config
    from app.providers.anthropic import AnthropicProvider

    monkeypatch.setenv("KEYS_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(config, "DEFAULT_KEYS_FILES", (str(tmp_path / "none.env"),))
    config._file_keys.cache_clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", ANTHROPIC_ADMIN_KEY)
    assert config.get_key(AnthropicProvider.env_keys) == ANTHROPIC_ADMIN_KEY
    config._file_keys.cache_clear()


# --- Moonshot / Kimi -----------------------------------------------------------
# The API reports bare numbers (currency inferred from the host) and a 401 usually
# means "wrong region", not "bad key" — both are covered here.

MOONSHOT_KEY = "test-moonshot-key-not-a-secret"


def moonshot_balance(available: float, cash: float, voucher: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "available_balance": available,
                "cash_balance": cash,
                "voucher_balance": voucher,
            }
        },
    )


def moonshot_handler(by_host: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        response = by_host.get(request.url.host)
        if response is None:
            return httpx.Response(404, json={"error": {"message": "no route"}})
        return response

    return handler


async def test_moonshot_reports_all_three_balances(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.delenv("MOONSHOT_REGION", raising=False)
    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)
    assert provider.base_url == "https://api.moonshot.ai/v1"
    assert provider.currency == "USD"

    handler = moonshot_handler({"api.moonshot.ai": moonshot_balance(49.58894, 3.0, 46.58893)})
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok, result.error
    primary = [b for b in result.balances if b.primary]
    assert primary and primary[0].label == "Available balance"
    assert primary[0].amount == 49.58894
    assert primary[0].currency == "USD"

    labels = {b.label: b.amount for b in result.balances}
    assert labels["Cash balance"] == 3.0
    assert labels["Voucher balance"] == 46.58893
    assert result.note is None
    assert result.meta["host"] == "api.moonshot.ai"
    assert result.meta["region_auto_detected"] is False


async def test_moonshot_china_region_uses_cny(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.setenv("MOONSHOT_REGION", "china")
    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)
    assert provider.base_url == "https://api.moonshot.cn/v1"
    assert provider.currency == "CNY"

    handler = moonshot_handler({"api.moonshot.cn": moonshot_balance(120.5, 20.5, 100.0)})
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert [b for b in result.balances if b.primary][0].currency == "CNY"


async def test_moonshot_base_url_override_wins(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
    monkeypatch.setenv("MOONSHOT_REGION", "international")
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)
    assert provider.base_url == "https://api.moonshot.cn/v1"
    assert provider.currency == "CNY"


async def test_moonshot_flags_a_key_that_belongs_to_the_other_region(monkeypatch):
    """A 401 from the configured host may just be a region mismatch."""
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.delenv("MOONSHOT_REGION", raising=False)
    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)

    handler = moonshot_handler(
        {
            "api.moonshot.ai": httpx.Response(401, json={"error": {"message": "invalid api key"}}),
            "api.moonshot.cn": moonshot_balance(88.0, 8.0, 80.0),
        }
    )
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok, result.error
    assert result.note and "belongs to the China platform" in result.note
    assert "MOONSHOT_REGION=china" in result.note
    assert result.meta["region_auto_detected"] is True
    assert result.meta["host"] == "api.moonshot.cn"
    assert [b for b in result.balances if b.primary][0].currency == "CNY"


async def test_moonshot_rejects_a_key_both_regions_deny(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.delenv("MOONSHOT_REGION", raising=False)
    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)
    denied = httpx.Response(401, json={"error": {"message": "invalid api key"}})
    handler = moonshot_handler({"api.moonshot.ai": denied, "api.moonshot.cn": denied})

    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert not result.ok
    assert "Unauthorized" in (result.error or "")
    assert result.meta["tried"] == ["https://api.moonshot.ai/v1", "https://api.moonshot.cn/v1"]


async def test_moonshot_warns_that_inference_is_blocked_at_zero(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)
    handler = moonshot_handler({"api.moonshot.ai": moonshot_balance(0.0, 0.0, 0.0)})
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note and "blocks inference" in result.note


async def test_moonshot_notes_arrears_when_cash_is_negative(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)
    handler = moonshot_handler({"api.moonshot.ai": moonshot_balance(10.0, -5.0, 15.0)})
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note and "in arrears" in result.note


async def test_moonshot_rate_limited_is_not_a_failure(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)
    handler = moonshot_handler({"api.moonshot.ai": httpx.Response(429, text="slow down")})
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.note and "rate limited" in result.note.lower()


async def test_moonshot_handles_unexpected_shapes(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)
    handler = moonshot_handler({"api.moonshot.ai": httpx.Response(200, json={"unexpected": True})})
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert not result.ok
    assert "Unexpected response shape" in (result.error or "")


async def test_moonshot_reports_network_errors(monkeypatch):
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    provider = MoonshotProvider(api_key=MOONSHOT_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with mock_client(handler) as client:
        result = await provider.fetch(client)
    assert not result.ok
    assert "Network error" in (result.error or "")


def test_moonshot_env_alias_prefers_moonshot_key(monkeypatch, tmp_path):
    from app import config
    from app.providers.moonshot import MoonshotProvider

    monkeypatch.setenv("KEYS_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(config, "DEFAULT_KEYS_FILES", (str(tmp_path / "none.env"),))
    config._file_keys.cache_clear()
    monkeypatch.setenv("KIMI_API_KEY", "kimi-alias-key")
    monkeypatch.setenv("MOONSHOT_API_KEY", MOONSHOT_KEY)
    assert config.get_key(MoonshotProvider.env_keys) == MOONSHOT_KEY
    monkeypatch.delenv("MOONSHOT_API_KEY")
    assert config.get_key(MoonshotProvider.env_keys) == "kimi-alias-key"
    config._file_keys.cache_clear()


INTERNATIONAL = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"


# --- Alibaba Token Plan (Personal Edition) -------------------------------------

async def test_alibaba_token_plan_reports_models_and_no_fake_balance():
    from app.providers.alibaba_token_plan import AlibabaTokenPlanProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "qwen3.8-flash"}, {"id": "glm-5.2"}]},
        )

    provider = AlibabaTokenPlanProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)

    assert result.ok
    assert result.meta["key_valid"] is True
    assert result.meta["models_visible"] == 2
    primary = [b for b in result.balances if b.primary]
    assert len(primary) == 1
    assert primary[0].amount == 2.0
    # the whole point: it never invents a Credits number
    assert "Connect console session" in result.note
    assert not any(b.amount is not None and b.currency == "Credits" for b in result.balances)


async def test_alibaba_token_plan_rejects_a_dead_key_with_region_advice():
    from app.providers.alibaba_token_plan import AlibabaTokenPlanProvider

    provider = AlibabaTokenPlanProvider(api_key=KEY)
    async with mock_client(lambda request: httpx.Response(401, json={})) as client:
        result = await provider.fetch(client)

    assert not result.ok
    assert "region-bound" in result.error
    assert "cn-beijing" in result.error  # international key was tried: the other host is named


async def test_alibaba_token_plan_region_switches_host(monkeypatch):
    from app.providers.alibaba_token_plan import AlibabaTokenPlanProvider

    monkeypatch.delenv("ALIBABA_TOKEN_PLAN_BASE_URL", raising=False)
    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_REGION", "china")
    provider = AlibabaTokenPlanProvider(api_key=KEY)
    assert "cn-beijing" in provider.base_url

    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_BASE_URL", INTERNATIONAL)
    provider = AlibabaTokenPlanProvider(api_key=KEY)
    assert provider.base_url == INTERNATIONAL.rstrip("/")


async def test_alibaba_token_plan_reports_network_errors():
    from app.providers.alibaba_token_plan import AlibabaTokenPlanProvider

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider = AlibabaTokenPlanProvider(api_key=KEY)
    async with mock_client(handler) as client:
        result = await provider.fetch(client)
    assert not result.ok
    assert "Network error" in result.error


@pytest.mark.parametrize(
    "value,expected",
    [("1.50", 1.5), (3, 3.0), (None, None), ("", None), ("abc", None), (True, None)],
)
def test_to_float(value, expected):
    from app.providers.base import to_float

    assert to_float(value) == expected
