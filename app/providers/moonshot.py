"""Moonshot / Kimi provider.

One endpoint does everything: ``GET /v1/users/me/balance`` returns
``available_balance`` (= cash + voucher), ``voucher_balance`` and ``cash_balance``.
No admin key needed — the API key itself can read its own balance.

Two real-world traps this handles:

* **Region.** API keys are issued per platform: an international key must talk to
  ``api.moonshot.ai`` and a China-issued key to ``api.moonshot.cn``. Using the wrong
  host returns 401, which looks exactly like a bad key. If the configured host
  refuses the key, the other region is tried, and a successful answer is labelled.
* **Currency.** The API returns bare numbers with no currency, so it is inferred from
  the host in use (``*.cn`` bills in CNY, otherwise USD).

Docs: https://platform.moonshot.ai/docs/api/overview  (Check Balance)
"""

from __future__ import annotations

import os

import httpx

from .base import KIND_INFO, KIND_USED, Balance, Provider, ProviderResult, to_float

INTERNATIONAL_BASE = "https://api.moonshot.ai/v1"
CHINA_BASE = "https://api.moonshot.cn/v1"


def currency_for_base(base_url: str) -> str:
    """The API does not say which currency it is reporting; the host does."""
    return "CNY" if "moonshot.cn" in base_url else "USD"


class MoonshotProvider(Provider):
    id = "moonshot"
    name = "Moonshot / Kimi"
    description = "Prepaid balance on the Moonshot (Kimi) open platform."
    docs_url = "https://platform.moonshot.ai/docs/api/overview"
    signup_url = "https://platform.moonshot.ai/console"
    keys_url = "https://platform.moonshot.ai/console/api-keys"
    env_keys = ("MOONSHOT_API_KEY", "KIMI_API_KEY")
    default_base_url = INTERNATIONAL_BASE
    base_url_env = "MOONSHOT_BASE_URL"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key)
        # MOONSHOT_BASE_URL wins (handled by the base class); then MOONSHOT_REGION.
        if not os.getenv(self.base_url_env):
            region = (os.getenv("MOONSHOT_REGION") or "").strip().lower()
            if region in {"china", "cn", "mainland"}:
                self.base_url = CHINA_BASE

    @property
    def currency(self) -> str:
        return currency_for_base(self.base_url)

    def other_region(self) -> tuple[str, str]:
        other = CHINA_BASE if "moonshot.cn" not in self.base_url else INTERNATIONAL_BASE
        return other, currency_for_base(other)

    async def _balance(self, client: httpx.AsyncClient, base_url: str) -> httpx.Response:
        return await client.get(f"{base_url}/users/me/balance", headers=self.auth_headers())

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:
        try:
            response = await self._balance(client, self.base_url)
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        note: str | None = None

        if response.status_code in (401, 403):
            # Same key, other platform: the usual cause of a 401 here.
            other_base, other_currency = self.other_region()
            try:
                retry = await self._balance(client, other_base)
            except httpx.HTTPError as exc:
                return ProviderResult(ok=False, error=f"Network error: {exc}")
            if retry.status_code == 200:
                note = (
                    f"This key belongs to the {'China' if other_currency == 'CNY' else 'international'} "
                    f"platform ({other_base.split('//')[1].split('/')[0]}). Set "
                    f"MOONSHOT_REGION={'china' if other_currency == 'CNY' else 'international'} to stop the extra probe."
                )
                return self._result(retry, other_base, other_currency, note)
            detail = self._error_message(retry) or self._error_message(response)
            return ProviderResult(
                ok=False,
                error="Unauthorized — the API key was rejected." + (f" Moonshot said: {detail}" if detail else ""),
                meta={"status": response.status_code, "tried": [self.base_url, other_base]},
            )

        if response.status_code == 429:
            return ProviderResult(
                ok=True,
                note="Moonshot rate limited the balance endpoint — try again shortly.",
                meta={"status": 429},
            )

        if response.status_code >= 400:
            return ProviderResult(ok=False, error=f"HTTP {response.status_code}: {response.text[:200]}")

        return self._result(response, self.base_url, self.currency, note)

    # --- parsing ----------------------------------------------------------------
    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or "")[:160]
        return str(error or "")[:160]

    def _result(
        self, response: httpx.Response, base_url: str, currency: str, note: str | None
    ) -> ProviderResult:
        try:
            body = response.json()
        except ValueError:
            return ProviderResult(ok=False, error="Unexpected (non-JSON) response")

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or "available_balance" not in data:
            return ProviderResult(
                ok=False, error=f"Unexpected response shape: {str(body)[:160]}"
            )

        available = to_float(data.get("available_balance"))
        cash = to_float(data.get("cash_balance"))
        voucher = to_float(data.get("voucher_balance"))

        balances = [
            Balance(label="Available balance", amount=available, currency=currency, primary=True),
            Balance(label="Cash balance", amount=cash, currency=currency, kind=KIND_INFO),
            Balance(label="Voucher balance", amount=voucher, currency=currency, kind=KIND_USED),
        ]

        notes: list[str] = []
        if note:
            notes.append(note)
        if available is not None and available <= 0:
            notes.append("Available balance is zero or negative: Moonshot blocks inference in this state until you top up.")
        elif cash is not None and cash < 0:
            notes.append("Cash balance is negative (in arrears); the available balance is covered by vouchers.")

        return ProviderResult(
            ok=True,
            balances=balances,
            note=" ".join(notes) if notes else None,
            meta={
                "key_type": "api",
                "host": base_url.split("//")[1].split("/")[0],
                "currency": currency,
                "currency_source": "inferred from host (the API reports bare numbers)",
                "region_auto_detected": bool(note and "belongs to" in note),
            },
        )
