"""Frontend contract tests.

The page is plain HTML/CSS/JS with no build step, so these tests pin down the
seams that a typo would silently break: every element the script looks up must
exist in index.html, and the asset paths must resolve.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def test_static_assets_exist_and_are_not_empty():
    for asset in ("index.html", "app.js", "style.css"):
        path = STATIC / asset
        assert path.is_file(), f"{asset} missing"
        assert path.stat().st_size > 200, f"{asset} looks truncated"


def test_every_element_looked_up_by_js_exists_in_html():
    wanted = set(re.findall(r'document\.getElementById\("([^"]+)"\)', JS))
    assert wanted, "expected the script to look up elements by id"
    present = set(re.findall(r'\bid="([^"]+)"', HTML))
    assert wanted <= present, f"ids missing from index.html: {sorted(wanted - present)}"


def test_html_loads_the_assets_that_exist():
    for match in re.findall(r'(?:src|href)="(/static/[^"]+)"', HTML):
        assert (STATIC / Path(match).name).is_file(), f"index.html references missing {match}"


def test_frontend_never_renders_a_full_key():
    # Only the masked hint from the API is displayed; the script must not ask for
    # or print anything resembling a raw key.
    assert "key_hint" in JS
    assert not re.search(r"\b(?:sk|sk-or)-[A-Za-z0-9]", JS)


def test_frontend_calls_the_documented_endpoints():
    assert "/api/balances" in JS


def test_frontend_hides_unconfigured_providers():
    """No placeholder card unless the server opts in; the env var is named in the footer."""
    assert "show_unconfigured" in JS
    assert "configured" in JS
    assert 'id="missing"' in HTML


def test_frontend_handles_the_abuse_blocker():
    """The 429 contract: surface it, disable the button, honour retry_after."""
    assert "429" in JS
    assert "retry_after" in JS
    assert "retry in" in JS
    assert 'id="cooldown"' in HTML


# The relay copy button must survive http:// pages, where navigator.clipboard
# is undefined — the execCommand fallback and the select-and-Ctrl+C last resort
# are load-bearing; pin them so a refactor can't silently drop the fallback.
@pytest.mark.parametrize("needle", ["isSecureContext", "execCommand", "press Ctrl+C"])
def test_copy_button_has_non_secure_context_fallback(needle):
    assert needle in JS


def test_secret_like_strings_are_not_hardcoded_in_assets():
    for asset, text in (("index.html", HTML), ("app.js", JS), ("style.css", CSS)):
        assert not re.search(r"(?i)(api[_-]?key|bearer)\s*[:=]\s*[\"'][^\"']{12,}", text), asset


@pytest.mark.parametrize(
    "needle", ["127.0.0.1", "docker run", "CREDIT_WATCH_BIND", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"]
)
def test_help_block_tells_users_where_keys_go(needle):
    assert needle in HTML
