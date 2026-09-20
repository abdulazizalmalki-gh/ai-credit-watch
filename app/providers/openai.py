"""OpenAI provider.

OpenAI has no "remaining credits" endpoint, and the two key families see different
APIs — getting this order wrong rejects perfectly good keys:

* admin keys (``sk-admin-…``) can call the Admin API: ``/v1/organization/costs`` and
  ``/v1/organization/usage/*``. They are usually *denied* on ``/v1/models``
  (403 "Missing scopes: api.model.read"), so they must not be validated that way.
* project keys (``sk-proj-…``) can call ``/v1/models`` but get 403 on the Admin API.

So the provider asks for the money first, and only falls back to ``/v1/models`` to
explain *why* billing is unavailable (key rejected vs. key too limited).

Docs: https://platform.openai.com/docs/api-reference/usage/costs
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

from .base import KIND_INFO, KIND_USED, Balance, Provider, ProviderResult, to_float


class OpenAIProvider(Provider):
    id = "openai"
    name = "OpenAI"
    description = "API spend and token usage for the OpenAI organization (admin key)."
    docs_url = "https://platform.openai.com/docs/api-reference/usage/costs"
    signup_url = "https://platform.openai.com/settings/organization/billing/overview"
    keys_url = "https://platform.openai.com/settings/organization/admin-keys"
    env_keys = ("OPENAI_ADMIN_KEY", "OPENAI_API_KEY")
    default_base_url = "https://api.openai.com/v1"
    base_url_env = "OPENAI_BASE_URL"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key)
        self.window_days = max(1, int(os.getenv("OPENAI_COST_WINDOW_DAYS", "30")))

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict | None = None):
        return await client.get(f"{self.base_url}{path}", headers=self.auth_headers(), params=params)

    def _window(self) -> tuple[int, int]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=self.window_days)
        return int(start.timestamp()), int(now.timestamp())

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:
        try:
            costs = await self._get(
                client,
                "/organization/costs",
                {
                    "start_time": self._window()[0],
                    "end_time": self._window()[1],
                    "bucket_width": "1d",
                    "limit": min(self.window_days, 180),
                },
            )
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if costs.status_code == 200:
            return await self._billing_result(client, costs)

        if costs.status_code == 429:
            return ProviderResult(
                ok=True,
                note="OpenAI rate limited the costs endpoint — the key is valid, try again shortly.",
                meta={"costs_status": 429, "billing_available": False},
            )

        if costs.status_code in (401, 403):
            return await self._diagnose(client, costs)

        return ProviderResult(
            ok=False, error=f"HTTP {costs.status_code}: {costs.text[:200]}"
        )

    # --- admin key path ---------------------------------------------------------
    async def _billing_result(self, client: httpx.AsyncClient, costs: httpx.Response) -> ProviderResult:
        try:
            buckets = costs.json().get("data") or []
        except ValueError:
            buckets = []

        now = datetime.now(timezone.utc)
        total = 0.0
        today = 0.0
        currency = "USD"
        active_days = 0
        for bucket in buckets:
            amounts = [
                (to_float((r.get("amount") or {}).get("value")), (r.get("amount") or {}).get("currency"))
                for r in (bucket.get("results") or [])
            ]
            day_total = sum(v for v, _ in amounts if v is not None)
            for _, code in amounts:
                if code:
                    currency = str(code).upper()
            total += day_total
            if day_total:
                active_days += 1
            try:
                day = datetime.fromtimestamp(int(bucket.get("start_time", 0)), tz=timezone.utc).date()
            except (TypeError, ValueError, OSError):
                day = None
            if day == now.date():
                today = day_total

        balances = [
            Balance(
                label=f"Spend (last {self.window_days} days)",
                amount=round(total, 4),
                currency=currency,
                kind=KIND_USED,
                primary=True,
            ),
            Balance(label="Spend today (UTC)", amount=round(today, 4), currency=currency, kind=KIND_USED),
            Balance(
                label="Average per day",
                amount=round(total / max(1, self.window_days), 4),
                currency=currency,
                kind=KIND_INFO,
            ),
        ]

        meta: dict[str, object] = {
            "key_type": "admin",
            "billing_available": True,
            "window_days": self.window_days,
            "buckets_returned": len(buckets),
            "days_with_spend": active_days,
            "cost_api": "organization/costs",
        }

        # A zero here means "no usage", not "no credit" — say so, because OpenAI keeps the
        # prepaid credit balance behind a browser session key and no API key can read it.
        note: str | None = None
        if total == 0:
            note = (
                f"No spend in the last {self.window_days} days. OpenAI does not expose your prepaid "
                "credit balance to API keys — that endpoint only accepts a browser session key — so "
                "this card reports usage; check the balance in the OpenAI billing dashboard."
            )

        # Token usage makes a zero-spend account readable ("nothing used" vs "broken").
        try:
            usage = await self._get(
                client,
                "/organization/usage/completions",
                {
                    "start_time": self._window()[0],
                    "end_time": self._window()[1],
                    "bucket_width": "1d",
                    "limit": min(self.window_days, 180),
                },
            )
            if usage.status_code == 200:
                input_tokens = output_tokens = requests = 0
                for bucket in usage.json().get("data") or []:
                    for result in bucket.get("results") or []:
                        input_tokens += int(result.get("input_tokens") or 0)
                        output_tokens += int(result.get("output_tokens") or 0)
                        requests += int(result.get("num_model_requests") or 0)
                balances.append(
                    Balance(
                        label=f"Tokens (last {self.window_days} days)",
                        amount=float(input_tokens + output_tokens),
                        currency=None,
                        kind=KIND_INFO,
                    )
                )
                balances.append(
                    Balance(label="Model requests", amount=float(requests), currency=None, kind=KIND_INFO)
                )
                meta["usage"] = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "requests": requests,
                }
        except (httpx.HTTPError, ValueError):
            pass  # usage is a bonus, never a failure

        # A model count is nice-to-have and admin keys often cannot read /v1/models.
        try:
            models = await self._get(client, "/models")
            if models.status_code == 200:
                count = len([m for m in (models.json().get("data") or []) if m.get("id")])
                balances.append(Balance(label="Models visible", amount=float(count), currency=None, kind=KIND_INFO))
                meta["models_visible"] = count
        except (httpx.HTTPError, ValueError):
            pass

        return ProviderResult(ok=True, balances=balances, note=note, meta=meta)

    # --- not-an-admin-key path --------------------------------------------------
    async def _diagnose(self, client: httpx.AsyncClient, costs: httpx.Response) -> ProviderResult:
        """Billing refused: is the key bad, or just not privileged enough?"""
        try:
            models = await self._get(client, "/models")
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if models.status_code in (401, 403):
            detail = ""
            try:
                error = (models.json() or {}).get("error") or {}
                detail = str(error.get("message") or "")[:160]
            except ValueError:
                pass
            return ProviderResult(
                ok=False,
                error="Unauthorized — the API key was rejected."
                + (f" OpenAI said: {detail}" if detail else ""),
                meta={"costs_status": costs.status_code, "models_status": models.status_code},
            )

        if models.status_code == 429:
            return ProviderResult(
                ok=True,
                note="OpenAI rate limited this key — try again shortly.",
                meta={"costs_status": costs.status_code, "models_status": 429, "billing_available": False},
            )

        if models.status_code >= 400:
            return ProviderResult(
                ok=False,
                error=f"Key check failed: HTTP {models.status_code} {models.text[:160]}",
                meta={"costs_status": costs.status_code, "models_status": models.status_code},
            )

        try:
            count = len([m for m in (models.json().get("data") or []) if m.get("id")])
        except ValueError:
            count = 0

        return ProviderResult(
            ok=True,
            balances=[
                Balance(label="Models visible to this key", amount=float(count), currency=None, kind=KIND_INFO)
            ],
            note=(
                "OpenAI exposes spend and usage only to Admin keys, so this key cannot read "
                "billing. Create an sk-admin-… key and set it as OPENAI_ADMIN_KEY to see costs."
            ),
            meta={"key_type": "project", "billing_available": False, "models_visible": count},
        )
