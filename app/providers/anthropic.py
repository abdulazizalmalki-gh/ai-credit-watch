"""Anthropic (Claude) provider.

The Usage & Cost Admin API exposes usage and cost, but only to an *admin* credential
(an Admin API key ``sk-ant-admin01-…``, an ``org:admin`` OAuth token, or a
non-workspace-scoped key). Standard API keys get 403 there, and the API has no
credit-balance endpoint at all — the Console shows that.

* ``GET /v1/organizations/cost_report``             — service-level cost, admin credential
* ``GET /v1/organizations/usage_report/messages``   — token usage, admin credential
* ``GET /v1/models``                                — works with a standard key: validates it

Cost amounts come back as decimal strings in *cents*, so "123.78912" means $1.2378912.

Docs: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

from .base import KIND_INFO, KIND_USED, Balance, Provider, ProviderResult, to_float

ANTHROPIC_VERSION = "2023-06-01"
#: The cost report only supports daily buckets and caps at 31 buckets.
MAX_WINDOW_DAYS = 31


class AnthropicProvider(Provider):
    id = "anthropic"
    name = "Anthropic"
    description = "Claude API spend and token usage for the organization (admin key)."
    docs_url = "https://platform.claude.com/docs/en/manage-claude/usage-cost-api"
    signup_url = "https://console.anthropic.com/settings/billing"
    keys_url = "https://console.anthropic.com/settings/admin-keys"
    env_keys = ("ANTHROPIC_ADMIN_KEY", "ANTHROPIC_API_KEY")
    default_base_url = "https://api.anthropic.com/v1"
    base_url_env = "ANTHROPIC_BASE_URL"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key)
        requested = int(os.getenv("ANTHROPIC_COST_WINDOW_DAYS", "30"))
        self.window_days = max(1, min(requested, MAX_WINDOW_DAYS))

    def auth_headers(self) -> dict[str, str]:
        # Anthropic authenticates with x-api-key, not a bearer token, and wants the
        # API version pinned on every request.
        return {
            "x-api-key": self.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "Accept": "application/json",
        }

    def _window(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = now - timedelta(days=self.window_days)
        return start.isoformat().replace("+00:00", "Z"), now.isoformat().replace("+00:00", "Z")

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict | None = None):
        return await client.get(f"{self.base_url}{path}", headers=self.auth_headers(), params=params)

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:
        starting_at, ending_at = self._window()
        try:
            costs = await self._get(
                client,
                "/organizations/cost_report",
                {
                    "starting_at": starting_at,
                    "ending_at": ending_at,
                    "bucket_width": "1d",
                    "limit": self.window_days,
                },
            )
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if costs.status_code == 200:
            return await self._billing_result(client, costs, starting_at, ending_at)

        if costs.status_code == 429:
            return ProviderResult(
                ok=True,
                note="Anthropic rate limited the cost report — the key is valid, try again shortly.",
                meta={"costs_status": 429, "billing_available": False},
            )

        if costs.status_code in (401, 403):
            return await self._diagnose(client, costs)

        return ProviderResult(ok=False, error=f"HTTP {costs.status_code}: {costs.text[:200]}")

    # --- admin credential path --------------------------------------------------
    async def _billing_result(
        self, client: httpx.AsyncClient, costs: httpx.Response, starting_at: str, ending_at: str
    ) -> ProviderResult:
        try:
            buckets = costs.json().get("data") or []
        except ValueError:
            buckets = []

        today = datetime.now(timezone.utc).date()
        total = 0.0
        today_total = 0.0
        currency = "USD"
        active_days = 0
        for bucket in buckets:
            day_total = 0.0
            for result in bucket.get("results") or []:
                # values are decimal strings in the lowest unit (cents)
                cents = to_float(result.get("amount"))
                if cents is not None:
                    day_total += cents / 100.0
                if result.get("currency"):
                    currency = str(result["currency"]).upper()
            total += day_total
            if day_total:
                active_days += 1
            try:
                bucket_day = datetime.fromisoformat(
                    str(bucket.get("starting_at", "")).replace("Z", "+00:00")
                ).date()
            except ValueError:
                bucket_day = None
            if bucket_day == today:
                today_total = day_total

        balances = [
            Balance(
                label=f"Spend (last {self.window_days} days)",
                amount=round(total, 4),
                currency=currency,
                kind=KIND_USED,
                primary=True,
            ),
            Balance(label="Spend today (UTC)", amount=round(today_total, 4), currency=currency, kind=KIND_USED),
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
            "cost_api": "organizations/cost_report",
        }

        note: str | None = None
        if total == 0:
            note = (
                f"No spend in the last {self.window_days} days. Anthropic's Usage & Cost Admin API "
                "reports usage and cost but has no credit-balance endpoint — the Console shows that."
            )

        # Token usage: makes a zero-spend window readable rather than looking broken.
        try:
            usage = await self._get(
                client,
                "/organizations/usage_report/messages",
                {
                    "starting_at": starting_at,
                    "ending_at": ending_at,
                    "bucket_width": "1d",
                    "limit": self.window_days,
                },
            )
            if usage.status_code == 200:
                tokens_in = tokens_out = cache_read = cache_write = 0
                for bucket in usage.json().get("data") or []:
                    for result in bucket.get("results") or []:
                        tokens_in += int(result.get("uncached_input_tokens") or 0)
                        tokens_out += int(result.get("output_tokens") or 0)
                        cache_read += int(result.get("cache_read_input_tokens") or 0)
                        creation = result.get("cache_creation") or {}
                        cache_write += int(creation.get("ephemeral_5m_input_tokens") or 0)
                        cache_write += int(creation.get("ephemeral_1h_input_tokens") or 0)
                balances.append(
                    Balance(
                        label=f"Tokens (last {self.window_days} days)",
                        amount=float(tokens_in + tokens_out),
                        currency=None,
                        kind=KIND_INFO,
                    )
                )
                if cache_read or cache_write:
                    balances.append(
                        Balance(
                            label="Cache reads / writes",
                            amount=float(cache_read + cache_write),
                            currency=None,
                            kind=KIND_INFO,
                        )
                    )
                meta["usage"] = {
                    "uncached_input_tokens": tokens_in,
                    "output_tokens": tokens_out,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                }
        except (httpx.HTTPError, ValueError):
            pass  # usage is a bonus, never a failure

        return ProviderResult(ok=True, balances=balances, note=note, meta=meta)

    # --- standard-key path ------------------------------------------------------
    async def _diagnose(self, client: httpx.AsyncClient, costs: httpx.Response) -> ProviderResult:
        """Costs refused: is the key invalid, or just not an admin credential?"""
        try:
            models = await self._get(client, "/models")
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if models.status_code in (401, 403):
            detail = ""
            try:
                error = models.json().get("error") or {}
                detail = str(error.get("message") or "")[:160]
            except (ValueError, AttributeError):
                pass
            return ProviderResult(
                ok=False,
                error="Unauthorized — the API key was rejected." + (f" Anthropic said: {detail}" if detail else ""),
                meta={"costs_status": costs.status_code, "models_status": models.status_code},
            )

        if models.status_code == 429:
            return ProviderResult(
                ok=True,
                note="Anthropic rate limited this key — try again shortly.",
                meta={"costs_status": costs.status_code, "models_status": 429, "billing_available": False},
            )

        if models.status_code >= 400:
            return ProviderResult(
                ok=False,
                error=f"Key check failed: HTTP {models.status_code} {models.text[:160]}",
                meta={"costs_status": costs.status_code, "models_status": models.status_code},
            )

        try:
            model_count = len(models.json().get("data") or [])
        except ValueError:
            model_count = 0

        return ProviderResult(
            ok=True,
            balances=[
                Balance(label="Models visible to this key", amount=float(model_count), currency=None, kind=KIND_INFO)
            ],
            note=(
                "Anthropic reports usage and cost only to an admin credential, so this key cannot read "
                "billing. Create an Admin API key (sk-ant-admin01-…) and set it as ANTHROPIC_ADMIN_KEY."
            ),
            meta={"key_type": "standard", "billing_available": False, "models_visible": model_count},
        )
