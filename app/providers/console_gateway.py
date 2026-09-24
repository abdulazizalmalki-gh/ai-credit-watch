"""Qwen Token Plan console gateway — the API Alibaba's own CLI calls.

With a linked console access token (see ``app/console_link.py``) the CLI gateway

    POST https://<cs-gateway>/cli/api.json?action=<Action>&product=sfm_bailian&api=<zelda-api>
    Authorization: Bearer <access_token>
    body: params=<json {"Api":..,"V":"1.0","Data":{..}}> & region=<region>

answers the personal-edition data APIs. That is exactly how
``bl usage token-plan`` works, so no browser cookie ever touches this app.

Gateway hosts (region + site), from the CLI's own routing table:

    cn-beijing     domestic      bailian-cs.console.aliyun.com          BroadScopeAspnGateway
    cn-beijing     international bailian-cs.console.alibabacloud.com    BroadScopeAspnGateway
    ap-southeast-1 domestic      modelstudio-cs.console.aliyun.com      IntlBroadScopeAspnGateway
    ap-southeast-1 international bailian-singapore-cs.alibabacloud.com  IntlBroadScopeAspnGateway

APIs under ``zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/``:
    usage          -> per5Hour / per1Week / per1Month Percentage + ResetTime (fractions)
    subscription   -> specCode, remainingDays, startTime, endTime, status
    quota-config   -> {tier: {five_hour, weekly}} absolute Credit ceilings (the join)
    addon/summary  -> remainingCredits / totalCredits / activeCount (extra bundles)

Docs: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/token-plan-personal-quick-start
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from .base import KIND_INFO, KIND_USED, Balance, ProviderResult, to_float

USAGE_API_BASE = "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2"

GATEWAYS: dict[tuple[str, str], tuple[str, str]] = {
    ("cn-beijing", "domestic"): ("bailian-cs.console.aliyun.com", "BroadScopeAspnGateway"),
    ("cn-beijing", "international"): ("bailian-cs.console.alibabacloud.com", "BroadScopeAspnGateway"),
    ("ap-southeast-1", "domestic"): ("modelstudio-cs.console.aliyun.com", "IntlBroadScopeAspnGateway"),
    ("ap-southeast-1", "international"): ("bailian-singapore-cs.alibabacloud.com", "IntlBroadScopeAspnGateway"),
}

#: (usage field, reset field, quota-config key, human label)
WINDOWS = (
    ("per5HourPercentage", "per5HourResetTime", "five_hour", "5-hour"),
    ("per1WeekPercentage", "per1WeekResetTime", "weekly", "7-day"),
    ("per1MonthPercentage", "per1MonthResetTime", "monthly", "monthly"),
)


def gateway_for(console: dict) -> tuple[str, str]:
    """(host, action) for the linked session's region + site, with a safe default."""
    region = str(console.get("console_region") or "").strip() or "ap-southeast-1"
    site = str(console.get("console_site") or "").strip() or "international"
    if (region, site) in GATEWAYS:
        return GATEWAYS[(region, site)]
    # Unknown pairing: fall back by site (international Model Studio is the default audience).
    return GATEWAYS[("ap-southeast-1" if site == "international" else "cn-beijing", site)]


def used_fraction(value: object) -> float | None:
    """Window usage as a 0..1 fraction.

    The console sends ratios (0.15). A small value above 1 is an overshooting
    ratio — a window cannot be more than full, so it clamps to 1.0. Anything
    from 10 up reads as a percent.
    """
    parsed = to_float(value)
    if parsed is None or parsed < 0:
        return None
    if 1 < parsed < 10:
        return 1.0
    if parsed >= 10:
        parsed /= 100.0
    return min(1.0, parsed)


def unwrap_envelope(payload: dict) -> dict:
    """Peel `data` / `DataV2.data` layers until the result object shows through."""
    current: object = payload
    for _ in range(6):
        if not isinstance(current, dict):
            break
        inner = current.get("data")
        if isinstance(inner, dict):
            current = inner
            continue
        data_v2 = current.get("DataV2")
        if isinstance(data_v2, dict) and isinstance(data_v2.get("data"), dict):
            current = data_v2["data"]
            continue
        break
    return current if isinstance(current, dict) else payload


async def gateway_call(client: httpx.AsyncClient, console: dict, api: str) -> dict | None:
    """One gateway API call (``api`` is the suffix, e.g. ``usage``); None on any failure."""
    token = str(console.get("access_token") or "")
    if not token:
        return None
    host, action = gateway_for(console)
    region = str(console.get("console_region") or "").strip() or "ap-southeast-1"
    full_api = f"{USAGE_API_BASE}/{api}"
    params_payload = {
        "Api": full_api,
        "V": "1.0",
        "Data": {
            "cornerstoneParam": {
                "protocol": "V2",
                "console": "ONE_CONSOLE",
                "productCode": "p_efm",
                "switchUserType": 3,
            }
        },
    }
    url = f"https://{host}/cli/api.json?action={action}&product=sfm_bailian&api={full_api}"
    body = {"params": json.dumps(params_payload, separators=(",", ":"), ensure_ascii=False), "region": region}
    try:
        response = await client.post(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("successResponse") is False:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("success") is False:
        return None
    return unwrap_envelope(data)


def _iso_from_millis(millis: object) -> str | None:
    value = to_float(millis)
    if value is None or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def build_result(usage: dict, quota: dict, subscription: dict, addon: dict) -> ProviderResult:
    """Turn the four gateway payloads into balances + meta + note."""
    balances: list[Balance] = []
    meta: dict[str, object] = {"source": "console", "windows": {}}
    spec = str(subscription.get("specCode") or "")
    ceilings = quota.get(spec) if isinstance(quota.get(spec), dict) else {}
    remaining_lines: list[Balance] = []
    resets: dict[str, str] = {}

    for field, reset_field, quota_key, label in WINDOWS:
        fraction = used_fraction(usage.get(field))
        if fraction is None:
            continue
        meta["windows"][label] = round(fraction, 4)
        reset_iso = _iso_from_millis(usage.get(reset_field))
        if reset_iso:
            resets[label] = reset_iso
        ceiling = to_float(ceilings.get(quota_key)) if ceilings else None
        if ceiling:
            used = round(fraction * ceiling, 2)
            balances.append(Balance(label=f"Credits used ({label})", amount=used, currency="Credits", kind=KIND_USED))
            remaining_lines.append(
                Balance(label=f"Credits remaining ({label})", amount=round(max(0.0, ceiling - used), 2), currency="Credits")
            )
        else:
            balances.append(
                Balance(label=f"Window used ({label})", amount=round(fraction * 100, 2), currency="%", kind=KIND_USED)
            )
    balances = remaining_lines + balances
    if remaining_lines:
        # The tightest window is the one that will stop you first: make it the headline.
        min(remaining_lines, key=lambda b: b.amount if b.amount is not None else float("inf")).primary = True

    addon_remaining = to_float(addon.get("remainingCredits"))
    if addon.get("activeCount") and addon_remaining is not None:
        balances.append(Balance(label="Extra bundle Credits remaining", amount=addon_remaining, currency="Credits"))
        total = to_float(addon.get("totalCredits"))
        if total:
            balances.append(Balance(label="Extra bundle Credits total", amount=total, currency="Credits", kind=KIND_INFO))

    if spec:
        meta["spec"] = spec
    days_left = subscription.get("remainingDays")
    if days_left is not None:
        meta["subscription_days_left"] = days_left

    note_bits: list[str] = []
    if spec:
        plan = f"Plan: {spec}"
        if days_left is not None:
            plan += f", {days_left} day(s) left"
        status = subscription.get("status")
        if status and status != "VALID":
            plan += f", status {status}"
        note_bits.append(plan + ".")
    for label, iso in resets.items():
        note_bits.append(f"{label} window resets {iso}.")

    if not balances:
        return ProviderResult(
            ok=False,
            error=(
                "Console session linked, but the Token Plan usage API returned nothing — "
                "the account may not hold a Personal subscription."
            ),
            meta=meta,
        )
    if not remaining_lines:
        note_bits.append("Quota ceilings unavailable; lines show percentages only.")

    return ProviderResult(ok=True, balances=balances, note=" ".join(note_bits) or None, meta=meta)


async def fetch_token_plan_usage(client: httpx.AsyncClient, console: dict) -> ProviderResult | None:
    """Full quota read for a linked console session; None when nothing is linked."""
    if not console.get("access_token"):
        return None
    usage = await gateway_call(client, console, "usage")
    if usage is None:
        return ProviderResult(
            ok=False,
            error=(
                "The console session was rejected (expired, or it lacks a Token Plan). "
                "Re-link it from the provider card."
            ),
            meta={"source": "console", "gateway_rejected": True},
        )
    quota = await gateway_call(client, console, "quota-config") or {}
    subscription = await gateway_call(client, console, "subscription") or {}
    addon = await gateway_call(client, console, "addon/summary") or {}
    return build_result(usage, quota, subscription, addon)
