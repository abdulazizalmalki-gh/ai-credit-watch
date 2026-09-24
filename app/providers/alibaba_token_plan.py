"""Alibaba Cloud Model Studio — Token Plan (Personal Edition, Qwen).

The ``sk-sp-`` plan key works against the OpenAI-compatible token-plan host, so
``GET {base}/models`` proves the subscription is alive and lists the models it can
call. That is as far as the public API goes: the inference host routes only
inference paths and every billing/quota path 404s, and Alibaba exposes Personal
Edition Credits usage only through the console UI — there is no API endpoint a key
can read it from. The card therefore shows validity + model count and a note
saying so; it never shows a fabricated balance.

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
    description = "Qwen Token Plan Personal Edition: subscription status and callable models."
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

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:
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
                "Subscription is active. Alibaba exposes Token Plan Credits usage only in the "
                "console (Model Studio > Subscription > Token Plan) — there is no API a key can "
                "read it from, so no balance is shown here."
            ),
            meta={"key_valid": True, "models_visible": len(models)},
        )
