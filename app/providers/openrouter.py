"""OpenRouter credit provider.

Uses two endpoints because they need different key scopes:

* ``GET /api/v1/key``     — works with any API key: per-key limit/usage.
* ``GET /api/v1/credits`` — needs a *management* key: account credits purchased/used.

The credits call is best-effort: a 403 there just means the key is a normal one,
and the dashboard falls back to what ``/key`` reports.
"""

from __future__ import annotations

import httpx

from .base import KIND_INFO, KIND_LIMIT, KIND_USED, Balance, Provider, ProviderResult, to_float


class OpenRouterProvider(Provider):
    id = "openrouter"
    name = "OpenRouter"
    description = "Credits left on OpenRouter (account and per-key)."
    docs_url = "https://openrouter.ai/docs/api-reference/limits"
    signup_url = "https://openrouter.ai/settings/credits"
    keys_url = "https://openrouter.ai/settings/keys"
    env_keys = ("OPENROUTER_API_KEY",)
    default_base_url = "https://openrouter.ai/api/v1"
    base_url_env = "OPENROUTER_API_BASE"

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:
        headers = self.auth_headers()

        try:
            key_response = await client.get(f"{self.base_url}/key", headers=headers)
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if key_response.status_code in (401, 403):
            return ProviderResult(ok=False, error="Unauthorized — the API key was rejected.")
        if key_response.status_code >= 400:
            return ProviderResult(
                ok=False,
                error=f"HTTP {key_response.status_code}: {key_response.text[:200]}",
            )

        try:
            key_data = (key_response.json() or {}).get("data") or {}
        except ValueError:
            return ProviderResult(ok=False, error="Unexpected (non-JSON) response")

        key_limit = to_float(key_data.get("limit"))
        key_remaining = to_float(key_data.get("limit_remaining"))
        key_usage = to_float(key_data.get("usage")) or 0.0

        account_remaining: float | None = None
        account_credits: dict[str, float] = {}
        try:
            credits_response = await client.get(f"{self.base_url}/credits", headers=headers)
            if credits_response.status_code < 400:
                credits_data = (credits_response.json() or {}).get("data") or {}
                total_credits = to_float(credits_data.get("total_credits"))
                total_usage = to_float(credits_data.get("total_usage"))
                if total_credits is not None and total_usage is not None:
                    account_remaining = round(total_credits - total_usage, 6)
                    account_credits = {"purchased": total_credits, "used": total_usage}
        except (httpx.HTTPError, ValueError):
            pass  # management-key endpoint is optional

        balances: list[Balance] = []
        note: str | None = None

        if account_remaining is not None:
            balances.append(
                Balance(label="Account credits remaining", amount=account_remaining, currency="USD", primary=True)
            )
            balances.append(
                Balance(label="Credits purchased", amount=account_credits["purchased"], currency="USD", kind=KIND_INFO)
            )
            balances.append(
                Balance(label="Credits used", amount=account_credits["used"], currency="USD", kind=KIND_USED)
            )
            if key_remaining is not None:
                balances.append(
                    Balance(label="Remaining on this key", amount=key_remaining, currency="USD", kind=KIND_INFO)
                )
        elif key_remaining is not None:
            balances.append(
                Balance(label="Remaining on this key", amount=key_remaining, currency="USD", primary=True)
            )
        else:
            note = (
                "No credit cap on this key, so OpenRouter reports no remaining amount. "
                "Add a management key to see account credits."
            )

        if key_limit is not None:
            balances.append(Balance(label="Key credit limit", amount=key_limit, currency="USD", kind=KIND_LIMIT))
        balances.append(
            Balance(label="Usage on this key (all time)", amount=key_usage, currency="USD", kind=KIND_USED)
        )

        return ProviderResult(
            ok=True,
            balances=balances,
            note=note,
            meta={
                "is_free_tier": bool(key_data.get("is_free_tier")),
                "usage_daily": to_float(key_data.get("usage_daily")),
                "usage_weekly": to_float(key_data.get("usage_weekly")),
                "usage_monthly": to_float(key_data.get("usage_monthly")),
                "account_credits_available": account_remaining is not None,
                "limit_reset": key_data.get("limit_reset"),
                "key_label": key_data.get("label"),
            },
        )
