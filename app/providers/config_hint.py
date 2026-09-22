"""Re-export of :func:`app.config.key_hint` / :func:`app.config.get_key` so providers can
use them without importing the app package twice (keeps provider modules self-contained)."""

from __future__ import annotations

from ..config import get_key, key_hint

__all__ = ["get_key", "key_hint"]

