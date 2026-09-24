"""Contract tests for the compose file and .env.example.

These guard the "add a provider" path, which is easy to get half-right: the provider
works, the tests pass, and the key still never reaches the container because nobody
added it to the compose passthrough or documented it. Dependency-free on purpose —
plain text parsing, so no YAML library is needed to run the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.providers import build_providers

REPO = Path(__file__).resolve().parents[1]
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
ENV_EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")


def effective(text: str) -> str:
    """The lines compose actually reads.

    This file explains its own decisions in comments — including the words it warns
    about — so a naive substring check matches the prose and fails on a correct file.
    """
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_compose_does_not_pin_a_container_name():
    """A hardcoded container_name makes a second instance (or a foreign container
    with the same name) fail with a daemon conflict, and COMPOSE_PROJECT_NAME
    cannot override it."""
    assert "container_name:" not in effective(COMPOSE), (
        "let compose name it <project>-credit-watch-N instead of pinning a name"
    )


def test_every_provider_key_is_passed_through_by_compose():
    """A key that only exists as an exported shell variable must still reach the
    container: compose publishes DEEPSEEK/OPENROUTER but silently dropped OpenAI,
    Anthropic and Moonshot until this was fixed."""
    missing = []
    for provider in build_providers():
        for env_name in provider.env_keys:
            if f"{env_name}:" not in COMPOSE:
                missing.append(f"{provider.id}:{env_name}")
    assert not missing, f"docker-compose.yml does not pass these through: {missing}"


def test_every_provider_documents_its_key_in_env_example():
    missing = [
        f"{p.id}:{p.env_keys[0]}"
        for p in build_providers()
        if p.env_keys[0] not in ENV_EXAMPLE
    ]
    assert not missing, f".env.example never mentions: {missing}"


def test_compose_publishes_an_interface_never_all_interfaces():
    """Every PUBLISHED PORT mapping pins an interface; container-internal bind
    env vars (e.g. CONSOLE_LINK_BIND=0.0.0.0 behind a 127.0.0.1 host mapping)
    are not exposures — but no mapping may bind all interfaces."""
    assert "${CREDIT_WATCH_BIND:-127.0.0.1}" in effective(COMPOSE)
    ports = re.search(r"ports:\n((?:[ \t]+-[^\n]+\n?)+)", effective(COMPOSE))
    assert ports, "compose lost its ports section"
    for line in ports.group(1).splitlines():
        mapping = line.strip().lstrip("- ")
        if mapping.startswith('"'):
            mapping = mapping[1:-1]
        left = mapping.rsplit(":", 1)[0]
        assert left and ":" in left, f"port mapping {mapping!r} binds all interfaces"
        assert not left.startswith("0.0.0.0"), f"port mapping {mapping!r} binds all interfaces"


def test_compose_keeps_the_container_hardened():
    for setting in ("read_only: true", "cap_drop:", "- ALL", "no-new-privileges:true"):
        assert setting in effective(COMPOSE), f"compose lost its hardening: {setting}"


# --- the test image must contain what the suite reads --------------------------
# Three separate CI failures came from this: the test stage enumerated the files it
# copied, a new test read a new file, and the build died on a missing path. The stage
# now copies the checkout, so the only thing that can still hide a file is
# .dockerignore — which is what these check.

TEST_STAGE_FILES = (
    "docker-compose.yml",
    ".env.example",
    "README.md",
    "run.sh",
    "pytest.ini",
    "requirements-dev.txt",
    "app/main.py",
    "tests/test_compose.py",
    "tests/test_docs.py",
    "dev/privacy-sweep.sh",
    "docs/dashboard.png",
    ".github/workflows/build-and-publish.yml",
)

# Nested caches must be excluded. Docker anchors a bare `__pycache__` at the context
# root only, so `tests/__pycache__` used to be copied in — and .pyc files carry the
# absolute path they were compiled from, i.e. the maintainer's home directory.
NEVER_SHIPPED = (
    "__pycache__/x.pyc",
    "tests/__pycache__/test_compose.cpython-312.pyc",
    "app/__pycache__/main.cpython-312.pyc",
    "app/providers/__pycache__/deepseek.cpython-312.pyc",
    ".pytest_cache/v/cache/nodeids",
    "app/.pytest_cache/v/cache/nodeids",
    ".env",
    ".privacy-deny.local",
    "keys.env",
    ".venv/lib/python3.12/site-packages/x.py",
)

DOCKERIGNORE = (REPO / ".dockerignore").read_text(encoding="utf-8")


def is_ignored(path: str, patterns: list[str]) -> bool:
    """Docker's .dockerignore rules.

    Patterns are matched against the path relative to the build context (so a bare
    name excludes only the top-level entry — NOT every directory of that name, which
    is the mistake this repo actually shipped once), a directory match excludes
    everything beneath it, last match wins, and ``!`` re-includes.
    """
    def to_regex(pattern: str) -> str:
        """Docker's pattern syntax: `**` spans directories (zero or more), `*` does not
        cross a slash, `?` is one character."""
        # a leading `**/` also matches the context root, i.e. `**/x` matches `x`
        prefix = ""
        if pattern.startswith("**/"):
            prefix, pattern = "(?:.*/)?", pattern[3:]
        out, i = "", 0
        while i < len(pattern):
            char = pattern[i]
            if pattern.startswith("**", i):
                out += ".*"
                i += 2
            elif char == "*":
                out += "[^/]*"
                i += 1
            elif char == "?":
                out += "[^/]"
                i += 1
            else:
                out += re.escape(char)
                i += 1
        return prefix + out

    ignored = False
    for raw in patterns:
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negate = pattern.startswith("!")
        pattern = pattern[1:].rstrip("/") if negate else pattern.rstrip("/")
        if re.fullmatch(to_regex(pattern) + r"(/.*)?", path):
            ignored = not negate
    return ignored


def test_dockerignore_does_not_hide_files_the_suite_reads():
    patterns = DOCKERIGNORE.splitlines()
    hidden = [p for p in TEST_STAGE_FILES if is_ignored(p, patterns)]
    assert not hidden, f"the test image would not contain: {hidden}"


def test_dockerignore_excludes_nested_caches_and_secrets():
    patterns = DOCKERIGNORE.splitlines()
    leaked = [p for p in NEVER_SHIPPED if not is_ignored(p, patterns)]
    assert not leaked, f"these would be baked into the image: {leaked}"


def test_dockerignore_still_ships_the_example_env():
    """The negations must survive: .env is private, .env.example is documentation."""
    patterns = DOCKERIGNORE.splitlines()
    assert is_ignored(".env", patterns)
    assert not is_ignored(".env.example", patterns)


def test_test_stage_copies_the_checkout_instead_of_a_file_list():
    """Re-enumerating files here is how the CI failures happened."""
    stage = (REPO / "Dockerfile").read_text(encoding="utf-8").split("FROM ")[1]
    copies = [l.strip() for l in stage.splitlines() if l.strip().startswith("COPY")]
    assert "COPY . ." in copies, f"test stage should copy the checkout, found: {copies}"


def test_ci_scans_the_built_image_not_just_the_source_tree():
    """Tree+history sweeps cannot see inside the artifact: a stale __pycache__ in the
    context puts the maintainer's home path into the image, and only --image catches it."""
    workflow = (REPO / ".github/workflows/build-and-publish.yml").read_text(encoding="utf-8")
    assert "privacy-sweep.sh --image" in workflow, (
        "the publish job must scan the image it is about to ship"
    )
