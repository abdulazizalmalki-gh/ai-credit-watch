"""Xiaomi MiMo provider.

Two different APIs are involved, and only one of them is reachable with an API key.

**Inference** — ``https://api.xiaomimimo.com/v1`` (OpenAI-compatible; the docs use an
``api-key:`` header, a plain ``Authorization: Bearer`` works too). ``GET /models`` is the
only account-ish endpoint a key can read: it proves the key is live and reports which
models the account may call. Every balance path on this host answers 404 —
``/user/balance``, ``/users/me/balance``, ``/v1/balance``, ``/v1/credits``, ``/v1/usage``
were all checked against a real key. MiMo simply does not publish balance to API keys.

**Console** — ``https://platform.xiaomimimo.com/api/v1`` is what the web console itself
calls: ``/balance`` (``data.balance``, ``data.currency``, optional ``cashBalance`` /
``giftBalance``), ``/tokenPlan/detail`` (``planCode``, ``currentPeriodEnd``, ``expired``)
and ``/tokenPlan/usage`` (``data.monthUsage.percent`` + ``items[]`` with used/limit). It is
authenticated with the *browser session*, not the key, so it needs the
``api-platform_serviceToken`` and ``userId`` cookies from a signed-in console tab —
paste the whole ``Cookie:`` header value into ``XIAOMI_MIMO_COOKIE``.

So the card is honest in both states: with the key alone it confirms the key works and
says where the balance actually lives; with the cookie as well it shows the real balance,
the paid vs. granted split, and the token plan's monthly usage.
"""

from __future__ import annotations

import httpx

from .base import KIND_BALANCE, KIND_INFO, KIND_USED, Balance, Provider, ProviderResult, to_float
from .config_hint import get_key

CONSOLE_BASE_URL = "https://platform.xiaomimimo.com/api/v1"
CONSOLE_ORIGIN = "https://platform.xiaomimimo.com"
CONSOLE_REFERER = "https://platform.xiaomimimo.com/#/console/balance"
#: The console rejects a session without these two; the rest of the header is optional.
REQUIRED_COOKIE_NAMES = ("api-platform_serviceToken", "userId")


def cookie_names(cookie_header: str) -> set[str]:
    """Cookie names present in a raw ``Cookie:`` header value."""
    names: set[str] = set()
    for part in cookie_header.split(";"):
        name = part.split("=", 1)[0].strip()
        if name:
            names.add(name)
    return names


def missing_cookie_names(cookie_header: str) -> list[str]:
    present = cookie_names(cookie_header)
    return [name for name in REQUIRED_COOKIE_NAMES if name not in present]


class XiaomiProvider(Provider):
    id = "xiaomi"
    name = "Xiaomi MiMo"
    description = "Prepaid balance and token-plan usage on the Xiaomi MiMo platform."
    docs_url = "https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call"
    signup_url = "https://platform.xiaomimimo.com/#/console/balance"
    keys_url = "https://platform.xiaomimimo.com/#/console/api-keys"
    env_keys = ("XIAOMI_MIMO_API_KEY", "MIMO_API_KEY")
    default_base_url = "https://api.xiaomimimo.com/v1"
    base_url_env = "XIAOMI_MIMO_API_BASE"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key)
        # Read through config so the cookie can live in the environment *or* a keys file.
        self.cookie = (get_key(("XIAOMI_MIMO_COOKIE",)) or "").strip()
        self.console_base_url = (
            get_key(("XIAOMI_MIMO_CONSOLE_API_BASE",)) or CONSOLE_BASE_URL
        ).rstrip("/")

    @property
    def configured(self) -> bool:
        """The console cookie alone can read the balance, even with no key stored."""
        return bool(self.api_key or self.cookie)

    def auth_headers(self) -> dict[str, str]:
        headers = super().auth_headers()
        if self.api_key:
            # Both spellings are accepted; sending both survives either gateway.
            headers["api-key"] = self.api_key
        return headers

    def console_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "x-timeZone": "UTC+00:00",
            "Origin": CONSOLE_ORIGIN,
            "Referer": CONSOLE_REFERER,
            "Cookie": self.cookie,
        }

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:
        if self.cookie:
            return await self._console_result(client)
        return await self._key_only_result(client)

    # --- console session path -----------------------------------------------------
    async def _console_result(self, client: httpx.AsyncClient) -> ProviderResult:
        missing = missing_cookie_names(self.cookie)
        if missing:
            return ProviderResult(
                ok=False,
                error=(
                    "XIAOMI_MIMO_COOKIE is missing the cookies the console needs: "
                    + ", ".join(missing)
                    + ". Copy the whole Cookie header from a signed-in "
                    "platform.xiaomimimo.com tab."
                ),
                meta={"missing_cookies": missing},
            )

        headers = self.console_headers()
        try:
            balance = await client.get(f"{self.console_base_url}/balance", headers=headers)
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if self._session_expired(balance):
            return ProviderResult(
                ok=False,
                error=(
                    "MiMo console session expired — log in again at platform.xiaomimimo.com, "
                    "copy a fresh Cookie header and update XIAOMI_MIMO_COOKIE."
                ),
                meta={"console_status": balance.status_code},
            )
        if balance.status_code == 403:
            return ProviderResult(
                ok=False,
                error="MiMo rejected the console cookies (403) — copy a fresh Cookie header.",
                meta={"console_status": 403},
            )
        if balance.status_code >= 400:
            return ProviderResult(
                ok=False,
                error=f"MiMo console returned HTTP {balance.status_code}: {balance.text[:200]}",
                meta={"console_status": balance.status_code},
            )

        try:
            payload = balance.json()
        except ValueError:
            return ProviderResult(
                ok=False,
                error="MiMo console returned a non-JSON page — the session is probably signed out.",
                meta={"console_status": balance.status_code},
            )

        code = payload.get("code")
        if code not in (0, None):
            message = str(payload.get("message") or "").strip()
            if code in (401, 403):
                return ProviderResult(
                    ok=False,
                    error="MiMo console session expired — log in again and refresh XIAOMI_MIMO_COOKIE.",
                    meta={"console_code": code},
                )
            return ProviderResult(
                ok=False,
                error=f"MiMo console error {code}: {message or 'unknown'}",
                meta={"console_code": code},
            )

        data = payload.get("data") or {}
        amount = to_float(data.get("balance"))
        if amount is None:
            return ProviderResult(ok=False, error="MiMo console returned no balance value.")

        currency = str(data.get("currency") or "").strip() or None
        balances = [
            Balance(label="Total balance", amount=amount, currency=currency, primary=True)
        ]
        meta: dict[str, object] = {"source": "console", "billing_available": True}

        cash = to_float(data.get("cashBalance"))
        gift = to_float(data.get("giftBalance"))
        if cash is not None:
            balances.append(Balance(label="Paid (cash)", amount=cash, currency=currency, kind=KIND_INFO))
            meta["cash_balance"] = cash
        if gift is not None:
            balances.append(Balance(label="Granted (gift)", amount=gift, currency=currency, kind=KIND_INFO))
            meta["gift_balance"] = gift

        note = await self._token_plan(client, headers, balances, meta)
        return ProviderResult(ok=True, balances=balances, note=note, meta=meta)

    async def _token_plan(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        balances: list[Balance],
        meta: dict[str, object],
    ) -> str | None:
        """Token-plan extras: nice to have, never fatal (a pay-as-you-go account has none)."""
        detail = await self._console_get(client, f"{self.console_base_url}/tokenPlan/detail", headers)
        usage = await self._console_get(client, f"{self.console_base_url}/tokenPlan/usage", headers)

        note: str | None = None
        if detail:
            plan = detail.get("data") or {}
            plan_code = str(plan.get("planCode") or "").strip()
            period_end = str(plan.get("currentPeriodEnd") or "").strip()
            expired = bool(plan.get("expired"))
            if plan_code:
                meta["plan_code"] = plan_code
            if period_end:
                meta["plan_period_end"] = period_end
                note = f"Token plan {plan_code or ''}".strip() + (
                    f" — expired {period_end}." if expired else f" — current period ends {period_end}."
                )
            elif expired:
                note = "Token plan is marked expired."

        if usage:
            month = (usage.get("data") or {}).get("monthUsage") or {}
            items = [item for item in (month.get("items") or []) if isinstance(item, dict)]
            used = sum(to_float(item.get("used")) or 0.0 for item in items)
            limit = sum(to_float(item.get("limit")) or 0.0 for item in items)
            percent = to_float(month.get("percent"))
            if items:
                balances.append(
                    Balance(label="Token plan credits used", amount=used, currency=None, kind=KIND_USED)
                )
                if limit:
                    balances.append(
                        Balance(
                            label="Token plan credits left",
                            amount=max(0.0, limit - used),
                            currency=None,
                            kind=KIND_BALANCE,
                        )
                    )
                meta["token_plan_used"] = used
                meta["token_plan_limit"] = limit
                if percent is not None:
                    meta["token_plan_percent"] = percent
        return note

    async def _console_get(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str]
    ) -> dict | None:
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if payload.get("code") not in (0, None):
            return None
        return payload

    @staticmethod
    def _session_expired(response: httpx.Response) -> bool:
        """A signed-out console 401s, and can also bounce the request to the login page."""
        if response.status_code == 401:
            return True
        for hop in response.history:
            if "account.xiaomi.com" in str(hop.headers.get("location", "")):
                return True
        return "account.xiaomi.com" in str(response.url)

    # --- API-key-only path --------------------------------------------------------
    async def _key_only_result(self, client: httpx.AsyncClient) -> ProviderResult:
        url = f"{self.base_url}/models"
        try:
            response = await client.get(url, headers=self.auth_headers())
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if response.status_code in (401, 403):
            detail = self._error_message(response)
            return ProviderResult(
                ok=False,
                error="Unauthorized — the API key was rejected."
                + (f" MiMo said: {detail}" if detail else ""),
                meta={"models_status": response.status_code},
            )
        if response.status_code == 429:
            return ProviderResult(
                ok=True,
                note="MiMo rate limited this key — try again shortly.",
                meta={"models_status": 429, "billing_available": False},
            )
        if response.status_code >= 400:
            return ProviderResult(
                ok=False,
                error=f"Key check failed: HTTP {response.status_code} {response.text[:160]}",
                meta={"models_status": response.status_code},
            )

        try:
            count = len([m for m in (response.json().get("data") or []) if m.get("id")])
        except ValueError:
            count = 0

        return ProviderResult(
            ok=True,
            balances=[
                Balance(
                    label="Models visible to this key",
                    amount=float(count),
                    currency=None,
                    kind=KIND_INFO,
                )
            ],
            note=(
                "The key works, but MiMo keeps credit balance inside its web console: the API "
                "has no balance endpoint for keys (…/v1/balance, …/user/balance and …/v1/usage "
                "all 404). Paste the console's Cookie header into XIAOMI_MIMO_COOKIE to show "
                "the balance here."
            ),
            meta={"key_valid": True, "billing_available": False, "models_visible": count},
        )

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            error = (response.json() or {}).get("error") or {}
        except ValueError:
            return ""
        if isinstance(error, dict):
            return str(error.get("message") or "")[:160]
        return str(error)[:160]


__all__ = ["XiaomiProvider", "missing_cookie_names", "cookie_names"]
