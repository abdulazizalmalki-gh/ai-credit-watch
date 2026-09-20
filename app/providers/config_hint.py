"""Re-export of :func:`app.config.key_hint` so providers can use it without
importing the app package twice (keeps provider modules self-contained)."""

from __future__ import annotations

from ..config import key_hint

__all__ = ["key_hint"]
