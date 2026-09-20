"""Tests for the privacy sweep — the guard that runs before anything is published.

The sweep must pass on this repo and must actually fail on planted private data;
a guard that silently passes is worse than no guard.

NOTE: every planted "leak" below is assembled at runtime (``ip(192, 168, 44, 7)``)
instead of written out literally, because this file is published too — a literal
192.168.x.x example here would itself be the leak we are guarding against, and the
sweep (correctly) flags its own repo. Keep it that way.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SWEEP = REPO / "dev" / "privacy-sweep.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not SWEEP.is_file(),
    reason="the sweep needs git and dev/privacy-sweep.sh present",
)


def ip(*octets: int) -> str:
    """Build an address without ever spelling one out in this file."""
    return ".".join(str(o) for o in octets)


def home_path(user: str) -> str:
    return "/home/" + user


def fake_key(prefix: str = "sk-or-v1-") -> str:
    return prefix + "al" * 12


def run_sweep(root: Path, *args: str, deny_file: Path | None = None):
    env = dict(os.environ)
    env["SWEEP_ROOT"] = str(root)
    env["PRIVACY_DENY_FILE"] = str(deny_file if deny_file else root / ".privacy-deny.local")
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"  # never read the developer's git config
    return subprocess.run(
        ["bash", str(SWEEP), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "dev").mkdir(parents=True)
    shutil.copy2(SWEEP, root / "dev" / "privacy-sweep.sh")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    return root


def commit_all(root: Path, message: str = "init") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


def test_sweep_is_executable_in_repo():
    assert os.access(SWEEP, os.X_OK), "the guard must be runnable as ./dev/privacy-sweep.sh"


def test_this_repo_passes_its_own_sweep():
    """This is the pre-push gate: the repo itself must always be publishable."""
    result = run_sweep(REPO)
    assert result.returncode == 0, f"repo sweep failed:\n{result.stdout}\n{result.stderr}"
    assert "PASSED" in result.stdout


def test_image_scan_is_scoped_to_the_content_we_ship():
    """The image mode must look at /app only.

    Scanning /etc flags the base image's own documentation (openssl.cnf, access.conf,
    networks and gai.conf all contain private-range examples) and runtime-mounted
    files: /etc/resolv.conf in a container is a copy of the host's, so the scan would
    print the operator's real DNS servers — a false positive that leaks what the
    guard exists to protect.
    """
    script = SWEEP.read_text(encoding="utf-8")
    image_scan = [l for l in script.splitlines() if "docker run" in l and "grep" in l]
    assert image_scan, "the image scan vanished from the sweep"
    for line in image_scan:
        assert "/etc" not in line, f"image scan must not read /etc: {line.strip()}"
        assert "/app" in line


def test_catches_private_ipv4(tmp_path):
    root = make_repo(tmp_path)
    leak = ip(192, 168, 44, 7)
    (root / "README.md").write_text(f"bind: {leak}\n", encoding="utf-8")
    commit_all(root)
    result = run_sweep(root)
    assert result.returncode == 1
    assert leak in result.stdout
    assert "FAILED" in result.stdout


def test_catches_cgnat_or_tailnet_range(tmp_path):
    root = make_repo(tmp_path)
    leak = ip(100, 110, 121, 68)
    (root / "notes.txt").write_text(f"tailnet {leak}\n", encoding="utf-8")
    commit_all(root)
    assert run_sweep(root).returncode == 1


def test_cgnat_range_has_no_address_level_exception(tmp_path):
    """Regression: an earlier ALLOWED entry exempted the first CGNAT address, so a
    tailnet-looking address in a test fixture sailed through. No CGNAT address may
    be allowlisted — 'it is only a fixture' is how a real address gets published."""
    root = make_repo(tmp_path)
    leak = ip(100, 64, 0, 7)
    (root / "tests").mkdir()
    (root / "tests" / "fixture.txt").write_text(f"tailscale0 inet {leak}/32\n", encoding="utf-8")
    commit_all(root)
    result = run_sweep(root)
    assert result.returncode == 1, "a CGNAT address is private data no matter how it is used"
    assert leak in result.stdout


def test_catches_absolute_home_path(tmp_path):
    root = make_repo(tmp_path)
    (root / "run.sh").write_text(f"#!/bin/sh\necho {home_path('testuser')}/app\n", encoding="utf-8")
    commit_all(root)
    assert run_sweep(root).returncode == 1


def test_catches_key_material(tmp_path):
    root = make_repo(tmp_path)
    (root / "keys.env").write_text(f"OPENROUTER_API_KEY={fake_key()}\n", encoding="utf-8")
    commit_all(root)
    assert run_sweep(root).returncode == 1


def test_catches_private_ipv6(tmp_path):
    root = make_repo(tmp_path)
    leak = "fd" + "a2:91c:ee7c:7400::1"
    (root / "net.txt").write_text(f"addr {leak}\n", encoding="utf-8")
    commit_all(root)
    assert run_sweep(root).returncode == 1


def test_catches_private_data_hidden_in_history(tmp_path):
    """A file that was committed and then deleted must still be caught."""
    root = make_repo(tmp_path)
    leak = ip(10, 1, 2, 3)
    secret_file = root / "config.txt"
    secret_file.write_text(f"host {leak}\n", encoding="utf-8")
    commit_all(root)
    secret_file.unlink()
    commit_all(root, "remove config")
    result = run_sweep(root)
    assert result.returncode == 1
    assert leak in result.stdout


def test_catches_private_data_in_commit_message(tmp_path):
    root = make_repo(tmp_path)
    leak = ip(172, 31, 5, 9)
    (root / "a.txt").write_text("nothing here\n", encoding="utf-8")
    commit_all(root, message=f"deploy to {leak} please")
    result = run_sweep(root)
    assert result.returncode == 1
    assert leak in result.stdout


def test_local_deny_file_adds_rules_without_publishing_them(tmp_path):
    root = make_repo(tmp_path)
    internal = "UNIQUE-INTERNAL-NAME"
    (root / "a.txt").write_text(f"target host {internal}\n", encoding="utf-8")
    commit_all(root)
    assert run_sweep(root).returncode == 0  # generic patterns cannot know the name

    (root / ".privacy-deny.local").write_text(f"# my private values\n{internal}\n", encoding="utf-8")
    result = run_sweep(root)
    assert result.returncode == 1
    assert internal in result.stdout


def test_documentation_addresses_are_allowed(tmp_path):
    root = make_repo(tmp_path)
    (root / "README.md").write_text(
        "Default is 127.0.0.1, docker bridge is 172.17.0.1, example LAN is 192.0.2.50, "
        "home is /home/user.\n",
        encoding="utf-8",
    )
    commit_all(root)
    result = run_sweep(root)
    assert result.returncode == 0, result.stdout


def test_staged_mode_only_scans_the_index(tmp_path):
    root = make_repo(tmp_path)
    (root / "clean.txt").write_text("hello\n", encoding="utf-8")
    commit_all(root)
    leak = ip(192, 168, 44, 8)
    (root / "leak.txt").write_text(f"db {leak}\n", encoding="utf-8")  # untracked
    assert run_sweep(root, "--staged").returncode == 0
    subprocess.run(["git", "add", "leak.txt"], cwd=root, check=True)
    result = run_sweep(root, "--staged")
    assert result.returncode == 1
    assert leak in result.stdout


def test_unknown_option_is_rejected(tmp_path):
    root = make_repo(tmp_path)
    assert run_sweep(root, "--wat").returncode == 2


def test_ci_workflow_blocks_publishing_on_the_sweep():
    workflow = (REPO / ".github" / "workflows" / "build-and-publish.yml").read_text(encoding="utf-8")
    assert "dev/privacy-sweep.sh" in workflow
    assert "needs: [test, privacy-sweep]" in workflow


# --- the local pre-push hook ---------------------------------------------------


def install_hook(root: Path) -> None:
    (root / "dev" / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "dev" / "hooks" / "pre-push", root / "dev" / "hooks" / "pre-push")
    (root / "dev" / "hooks" / "pre-push").chmod(0o755)


def run_hook(root: Path):
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return subprocess.run(
        ["bash", "dev/hooks/pre-push"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_pre_push_hook_lets_a_clean_repo_through(tmp_path):
    root = make_repo(tmp_path)
    install_hook(root)
    (root / "README.md").write_text("all good\n", encoding="utf-8")
    commit_all(root)
    result = run_hook(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sweep passed" in result.stdout


def test_pre_push_hook_blocks_a_leaky_push(tmp_path):
    root = make_repo(tmp_path)
    install_hook(root)
    leak = ip(192, 168, 77, 5)
    (root / "run.sh").write_text(f"#!/bin/sh\nbind {leak}\n", encoding="utf-8")
    commit_all(root)
    result = run_hook(root)
    assert result.returncode == 1
    assert "PRIVACY SWEEP FAILED" in result.stderr
    assert leak in result.stdout


def test_install_hooks_script_installs_a_runnable_hook(tmp_path):
    root = make_repo(tmp_path)
    shutil.copy2(REPO / "dev" / "install-hooks.sh", root / "dev" / "install-hooks.sh")
    install_hook(root)
    result = subprocess.run(
        ["bash", "dev/install-hooks.sh"], cwd=root, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    installed = root / ".git" / "hooks" / "pre-push"
    assert installed.is_file()
    assert os.access(installed, os.X_OK)
    assert "privacy-sweep.sh" in installed.read_text(encoding="utf-8")


def test_documented_install_command_exists():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "./dev/install-hooks.sh" in readme


@pytest.mark.parametrize("needle", ["PRIVATE KEY", "sk-", "--staged", "privacy-deny.local"])
def test_sweep_documents_its_patterns(needle):
    assert needle in SWEEP.read_text(encoding="utf-8")
