"""Provider interface.

To add a provider: drop a new module in ``app/providers/`` defining one subclass
of :class:`Provider` with a unique ``id``. It is discovered automatically at
startup (see ``app/providers/__init__.py``) and shown in the UI.

Note: :class:`Provider` is a plain class on purpose. Subclass metadata lives in
class attributes, which a ``@dataclass``-generated ``__init__`` would ignore.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

#: Kinds of balance lines, used by the UI for ordering/styling.
KIND_BALANCE = "balance"  # money you can still spend
KIND_USED = "used"  # money already spent
KIND_LIMIT = "limit"  # a cap that was configured
KIND_INFO = "info"  # anything else that is useful context


def to_float(value: Any) -> float | None:
    """Best-effort numeric conversion; providers return strings or numbers."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class Balance:
    label: str
    amount: float | None
    currency: str | None = None
    kind: str = KIND_BALANCE
    primary: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "amount": self.amount,
            "currency": self.currency,
            "kind": self.kind,
            "primary": self.primary,
        }


@dataclass
class ProviderResult:
    ok: bool
    balances: list[Balance] = field(default_factory=list)
    error: str | None = None
    note: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "note": self.note,
            "balances": [b.as_dict() for b in self.balances],
            "meta": self.meta,
        }


class Provider:
    """Base class for provider integrations.

    Subclasses set the class attributes below and implement :meth:`fetch`.
    """

    # --- identity -----------------------------------------------------------
    id: str = ""
    name: str = ""
    description: str = ""
    docs_url: str = ""
    signup_url: str = ""  # where to buy credits
    keys_url: str = ""  # where to create a key
    env_keys: tuple[str, ...] = ()  # env var names accepted, first one wins
    default_base_url: str = ""
    base_url_env: str = ""  # optional env var overriding the API base URL

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        override = os.getenv(self.base_url_env) if self.base_url_env else None
        self.base_url = (override or self.default_base_url).rstrip("/")

    # --- helpers ------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def catalog_entry(self) -> dict[str, Any]:
        from .config_hint import key_hint  # avoid importing the app package here

        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "docs_url": self.docs_url,
            "signup_url": self.signup_url,
            "keys_url": self.keys_url,
            "key_env": self.env_keys[0] if self.env_keys else "",
            "configured": self.configured,
            "key_hint": key_hint(self.api_key),
        }

    async def fetch(self, client: httpx.AsyncClient) -> ProviderResult:  # pragma: no cover
        raise NotImplementedError
