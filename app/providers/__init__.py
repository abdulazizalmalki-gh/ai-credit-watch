"""Provider registry with automatic discovery."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

from .. import config
from .base import Balance, Provider, ProviderResult, to_float  # noqa: F401  (re-export)

_REGISTRY: dict[str, type[Provider]] = {}


def _discover() -> None:
    package_dir = Path(__file__).parent
    names = sorted(
        mod.name
        for mod in pkgutil.iter_modules([str(package_dir)])
        if not mod.name.startswith("_")
    )
    for name in names:
        module = importlib.import_module(f"{__name__}.{name}")
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, Provider)
                and obj is not Provider
                and getattr(obj, "id", "")
            ):
                _REGISTRY[obj.id] = obj


_discover()


def provider_ids() -> list[str]:
    return sorted(_REGISTRY)


def get_provider_class(provider_id: str) -> type[Provider]:
    return _REGISTRY[provider_id]


def build_providers() -> list[Provider]:
    """Instantiate every known provider, loading its key if present."""
    return [cls(config.get_key(cls.env_keys)) for _, cls in sorted(_REGISTRY.items())]


def catalog() -> list[dict[str, Any]]:
    return [p.catalog_entry() for p in build_providers()]
