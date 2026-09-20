"""Configuration helpers.

Keys are read, in order of precedence, from:

1. real environment variables (``docker run -e`` / ``env_file`` / shell export)
2. an optional keys file, default ``/run/secrets/credit-watch.env`` when running
   in Docker, else ``keys.env`` / ``.env`` next to the working directory.

The file format is plain ``KEY=value`` lines; ``#`` starts a comment.
Values are never logged or returned to the browser.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

DEFAULT_KEYS_FILES = ("/run/secrets/credit-watch.env", "keys.env", ".env")


def _parse_env_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return parsed
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            parsed[name] = value
    return parsed


@lru_cache(maxsize=1)
def _file_keys() -> dict[str, str]:
    candidates: list[Path] = []
    explicit = os.getenv("KEYS_FILE")
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(Path(p) for p in DEFAULT_KEYS_FILES)
    merged: dict[str, str] = {}
    for path in candidates:
        if path.is_file():
            merged.update(_parse_env_file(path))
    return merged


def get_key(env_names: tuple[str, ...]) -> str | None:
    """Return the first non-empty value found in the environment or keys file."""
    for name in env_names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    file_values = _file_keys()
    for name in env_names:
        value = file_values.get(name)
        if value and value.strip():
            return value.strip()
    return None


def key_hint(secret: str | None) -> str | None:
    """A non-reversible-ish hint: last 4 characters only."""
    if not secret:
        return None
    return f"…{secret[-4:]}" if len(secret) > 4 else "…"
