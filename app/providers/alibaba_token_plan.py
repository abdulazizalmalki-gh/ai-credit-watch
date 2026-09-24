"""Alibaba Cloud Model Studio — Token Plan (Personal Edition, Qwen).

Two sources, matching the product's two surfaces:

* **The ``sk-sp-`` plan key** — ``GET {base}/models`` on the OpenAI-compatible
  token-plan host proves the subscription is alive and lists its models. That is
  the whole public surface: every billing path on the inference host 404s, so the
  key never shows a fabricated balance.
* **The console link** — Credits usage (5-hour / 7-day / monthly windows, extra
  bundles) comes from the official Bailian CLI gateway, the same ``/cli/api.json``
  calls ``bl usage token-plan`` makes, authenticated with the short-lived console
  access token the ``console-login`` page hands to a loopback port (see
  ``app/console_link.py``; no cookies are stored). Headless installs can paste
  that token as ``ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN`` instead.

Region matters the same way it does for Moonshot: keys are issued per plan region
(Singapore ``ap-southeast-1`` or China ``cn-beijing``) and only work against their
matching host — a wrong host returns 401, which looks like a dead subscription.

Docs: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/token-plan-personal-quick-start
"""

from __future__ import annotations

import os

import httpx

from .base import KIND_INFO, Balance, Provider, ProviderResult

INTERNATIONAL_BASE = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
CHINA_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


class AlibabaTokenPlanProvider(Provider):
    id = "alibaba-token-plan"
    name = "Alibaba Token Plan (Qwen)"
    description = "Qwen Token Plan Personal Edition: subscription status and rolling Credits quota."
    docs_url = "https://docs.modelstudio.console.alibabacloud.com/en/model-studio/token-plan-personal-quick-start"
    signup_url = "https://modelstudio.console.alibabacloud.com/subscription/overview"
    keys_url = "https://modelstudio.console.alibabacloud.com/subscription/overview"
    env_keys = ("ALIBABA_TOKEN_PLAN_API_KEY",)
    default_base_url = INTERNATIONAL_BASE
    base_url_env = "ALIBABA_TOKEN_PLAN_BASE_URL"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key)
        # ALIBABA_TOKEN_PLAN_BASE_URL wins (handled by the base class); then REGION.
        if not os.getenv(self.base_url_env):
            region = (os.getenv("ALIBABA_TOKEN_PLAN_REGION") or "").strip().lower()
            if region in {"china", "cn", "cn-beijing", "beijing"}:
                self.base_url = CHINA_BASE

    @property
    def console(self) -> dict:
        """The linked console session, if any (token + region hints)."""
        from ..console_link import console_token

        return console_token() or {}

    @property
    def configured(self) -> bool:
        # A linked console session reads quota without any inference key.
        return bool(self.api_key or self.console)

    def catalog_entry(self) -> dict:
        entry = super().catalog_entry()
        entry["console_linked"] = bool(self.console)
        entry["linkable"] = True
        return entry

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:
        key_result = await self._fetch_key(client) if self.api_key else None
        quota = await self._fetch_quota(client) if self.console else None

        if quota is not None and quota.ok:
            balances = list(quota.balances)
            note = quota.note
            meta = dict(quota.meta)
            if key_result is not None and key_result.ok:
                balances.extend(key_result.balances)
                meta.update(key_result.meta)
            elif key_result is not None:
                meta["key_error"] = key_result.error
            return ProviderResult(ok=True, balances=balances, note=note, meta=meta)

        if quota is not None and not quota.ok and key_result is None:
            return quota  # console-only link and it failed
        if key_result is not None and quota is not None and not quota.ok:
            # The key still works; replace the "link the console" advice with
            # what actually went wrong, since a session IS linked.
            key_result.note = quota.error or "Console session failed."
            key_result.meta["console_error"] = quota.error
        if key_result is not None:
            return key_result
        return ProviderResult(
            ok=False,
            error=(
                "Not configured — set ALIBABA_TOKEN_PLAN_API_KEY, press Connect console "
                "session, or set ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN for Credits usage."
            ),
        )

    # --- console-token path (official Bailian CLI gateway) ----------------------
    async def _fetch_quota(self, client: httpx.AsyncClient) -> ProviderResult | None:
        from .console_gateway import fetch_token_plan_usage

        return await fetch_token_plan_usage(client, self.console)

    # --- sk-sp- key path ---------------------------------------------------------
    async def _fetch_key(self, client: httpx.AsyncClient) -> ProviderResult:
        try:
            response = await client.get(f"{self.base_url}/models", headers=self.auth_headers())
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=f"Network error: {exc}")

        if response.status_code in (401, 403):
            other = CHINA_BASE if "cn-beijing" not in self.base_url else INTERNATIONAL_BASE
            other_name = "China (Beijing)" if "cn-beijing" in other else "international (Singapore)"
            return ProviderResult(
                ok=False,
                error=(
                    "Unauthorized — the sk-sp- key was rejected. Token Plan keys are region-bound; "
                    f"if this plan was issued {other_name}, set ALIBABA_TOKEN_PLAN_BASE_URL={other} "
                    "or ALIBABA_TOKEN_PLAN_REGION accordingly."
                ),
                meta={"status": response.status_code},
            )
        if response.status_code >= 400:
            return ProviderResult(
                ok=False,
                error=f"HTTP {response.status_code}: {response.text[:200]}",
            )

        try:
            models = response.json().get("data") or []
        except ValueError:
            return ProviderResult(ok=False, error="Unexpected (non-JSON) response")

        return ProviderResult(
            ok=True,
            balances=[
                Balance(
                    label="Models available to this key",
                    amount=float(len(models)),
                    currency=None,
                    kind=KIND_INFO,
                    primary=True,
                )
            ],
            note=(
                "Subscription is active. Credits usage needs the console session — press "
                "\u201cConnect console session\u201d below, or set ALIBABA_TOKEN_PLAN_CONSOLE_TOKEN."
            ),
            meta={"key_valid": True, "models_visible": len(models)},
        )
