"""Docs consistency checks.

A README is the first thing anyone reads, and a broken image or a stale claim is the
first thing they notice. These keep the docs honest and cheap to fix.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = (REPO / "README.md").read_text(encoding="utf-8")

# [text](target) — includes images, since they use the same syntax
MARKDOWN_LINKS = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def test_relative_links_and_images_exist():
    """Every relative target in the README must be a real file (badges are absolute)."""
    missing = [
        target
        for target in MARKDOWN_LINKS.findall(README)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
        and not (REPO / target.split("#")[0]).exists()
    ]
    assert not missing, f"README points at files that do not exist: {missing}"


def test_readme_shows_the_dashboard():
    """A dashboard with no picture is a hard sell; the screenshot must be committed."""
    assert "docs/dashboard.png" in README
    assert (REPO / "docs" / "dashboard.png").is_file()
    assert (REPO / "docs" / "dashboard.png").stat().st_size > 10_000, "screenshot looks empty"


def test_every_documented_env_var_exists_in_the_example_file():
    """The configuration table is the contract; .env.example must cover it."""
    table = README.split("## Configuration", 1)[1].split("## ", 1)[0]
    documented = {
        name
        for row in table.splitlines()
        if row.startswith("|")
        for name in re.findall(r"`([A-Z][A-Z0-9_]{2,})`", row)
    }
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    undocumented = sorted(n for n in documented if n not in example)
    assert not undocumented, f"documented but absent from .env.example: {undocumented}"


def test_readme_states_the_providers_it_actually_ships():
    """The provider table must list every provider module, so adding one can't
    silently leave the README claiming fewer."""
    from app.providers import build_providers

    rows = [l for l in README.splitlines() if l.startswith("| **")]
    listed = " ".join(rows).lower()
    missing = [p.id for p in build_providers() if p.id not in listed and p.name.lower() not in listed]
    assert not missing, f"shipped providers absent from the README table: {missing}"
