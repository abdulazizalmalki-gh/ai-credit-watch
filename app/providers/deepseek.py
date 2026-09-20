"""DeepSeek balance provider.

GET https://api.deepseek.com/user/balance
  -> {"is_available": true,
      "balance_infos": [{"currency": "USD", "total_balance": "110.00",
                         "granted_balance": "10.00", "topped_up_balance": "100.00"}]}
"""

from __future__ import annotations

import httpx

from .base import (
    KIND_INFO,
    KIND_USED,
    Balance,
    Provider,
    ProviderResult,
    to_float,
)


class DeepSeekProvider(Provider):
    id = "deepseek"
    name = "DeepSeek"
    description = "Prepaid balance on the DeepSeek platform (api.deepseek.com)."
    docs_url = "https://api-docs.deepseek.com/api/get-user-balance"
    signup_url = "https://platform.deepseek.com/top_up"
    keys_url = "https://platform.deepseek.com/api_keys"
    env_keys = ("DEEPSEEK_API_KEY",)
    default_base_url = "https://api.deepseek.com"
    base_url_env = "DEEPSEEK_API_BASE"

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:
        url = f"{self.base_url}/user/balance"
        try:
            response = await client.get(url, headers=self.auth_headers())
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if response.status_code == 401:
            return ProviderResult(ok=False, error="Unauthorized — the API key was rejected.")
        if response.status_code >= 400:
            return ProviderResult(
                ok=False,
                error=f"HTTP {response.status_code}: {response.text[:200]}",
            )

        try:
            payload = response.json()
        except ValueError:
            return ProviderResult(ok=False, error="Unexpected (non-JSON) response")

        infos = payload.get("balance_infos") or []
        if not infos:
            return ProviderResult(
                ok=True,
                note="No balance information returned for this account.",
                meta={"usable": bool(payload.get("is_available"))},
            )

        balances: list[Balance] = []
        for index, info in enumerate(infos):
            currency = info.get("currency")
            suffix = "" if index == 0 else f" ({currency})"
            balances.append(
                Balance(
                    label=f"Total balance{suffix}",
                    amount=to_float(info.get("total_balance")),
                    currency=currency,
                    primary=index == 0,
                )
            )
            granted = to_float(info.get("granted_balance"))
            topped_up = to_float(info.get("topped_up_balance"))
            if granted:
                balances.append(
                    Balance(label=f"Granted (unexpired){suffix}", amount=granted, currency=currency, kind=KIND_INFO)
                )
            if topped_up:
                balances.append(
                    Balance(label=f"Topped up{suffix}", amount=topped_up, currency=currency, kind=KIND_USED)
                )

        return ProviderResult(
            ok=True,
            balances=balances,
            meta={"usable": bool(payload.get("is_available"))},
        )
