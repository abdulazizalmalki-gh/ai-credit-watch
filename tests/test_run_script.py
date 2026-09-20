"""Tests for run.sh — the documented default entrypoint.

The real script runs end-to-end with fake ``ip`` and ``docker`` binaries prepended to
PATH, so the host's Docker daemon, containers and .env are never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "run.sh"

# Mirrors the shape of `ip -4 -o addr show scope global` (one line per address,
# with the interface names that must be skipped: loopback, docker bridges, VPN).
# Every address here is from an RFC5737 documentation range — never a real one;
# the sweep has no exception for RFC1918 or CGNAT space, and neither should a test.
IP_OUTPUT = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 192.0.2.50/24 brd 192.0.2.255 scope global dynamic eth0\\       valid_lft 63040sec preferred_lft 63040sec
3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever
4: tailscale0    inet 198.51.100.7/32 scope global tailscale0\\       valid_lft forever preferred_lft forever
"""


def write_bin(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def sandbox(tmp_path):
    work = tmp_path / "app"
    work.mkdir()
    shutil.copy2(SCRIPT, work / "run.sh")  # copy2 keeps the executable bit
    shutil.copy2(REPO / ".env.example", work / ".env.example")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"

    write_bin(
        bin_dir,
        "docker",
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {log}
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then
    echo 'Docker Compose version v5.4.0'
fi
exit 0
""",
    )
    write_bin(bin_dir, "ip", f"#!/usr/bin/env bash\ncat <<'IP_OUT'\n{IP_OUTPUT}IP_OUT\n")

    return {"dir": work, "bin": bin_dir, "log": log, "tmp": tmp_path}


def run_script(sandbox, *args, path_prefix=None):
    env = dict(os.environ)
    env["PATH"] = f"{path_prefix or sandbox['bin']}:{env['PATH']}"
    return subprocess.run(
        ["./run.sh", *args],
        cwd=sandbox["dir"],
        env=env,
        input="",  # non-interactive: must never hang waiting for a prompt
        capture_output=True,
        text=True,
        timeout=60,
    )


def env_lines(sandbox, key):
    text = (sandbox["dir"] / ".env").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.startswith(f"{key}=")]


def test_repo_script_is_executable():
    """CI/git must ship the exec bit, otherwise the README command fails."""
    assert os.access(SCRIPT, os.X_OK)


def test_default_publishes_on_the_lan_ip(sandbox):
    result = run_script(sandbox)
    assert result.returncode == 0, result.stderr
    assert env_lines(sandbox, "CREDIT_WATCH_BIND") == ["CREDIT_WATCH_BIND=192.0.2.50"]
    assert "http://192.0.2.50:8760" in result.stdout
    assert "compose up -d --build" in sandbox["log"].read_text()


def test_env_is_created_from_example_and_locked_down(sandbox):
    assert not (sandbox["dir"] / ".env").exists()
    assert run_script(sandbox).returncode == 0
    created = sandbox["dir"] / ".env"
    assert created.is_file()
    assert (created.stat().st_mode & 0o777) == 0o600
    assert "DEEPSEEK_API_KEY=" in created.read_text(encoding="utf-8")


def test_localhost_flag_keeps_it_on_loopback(sandbox):
    result = run_script(sandbox, "--localhost")
    assert result.returncode == 0, result.stderr
    assert env_lines(sandbox, "CREDIT_WATCH_BIND") == ["CREDIT_WATCH_BIND=127.0.0.1"]
    assert "http://127.0.0.1:8760" in result.stdout
    assert "this host only" in result.stdout


def test_bind_and_port_flags_are_honoured(sandbox):
    result = run_script(sandbox, "--bind", "192.0.2.51", "--port", "9123")
    assert result.returncode == 0, result.stderr
    assert env_lines(sandbox, "CREDIT_WATCH_BIND") == ["CREDIT_WATCH_BIND=192.0.2.51"]
    assert env_lines(sandbox, "CREDIT_WATCH_PORT") == ["CREDIT_WATCH_PORT=9123"]
    assert "http://192.0.2.51:9123" in result.stdout


def test_existing_keys_are_preserved(sandbox):
    (sandbox["dir"] / ".env").write_text(
        "# my keys\nDEEPSEEK_API_KEY=test-key-preserved-marker\nOPENROUTER_API_KEY=\n",
        encoding="utf-8",
    )
    assert run_script(sandbox).returncode == 0
    text = (sandbox["dir"] / ".env").read_text(encoding="utf-8")
    assert "# my keys" in text
    assert "DEEPSEEK_API_KEY=test-key-preserved-marker" in text
    assert text.count("CREDIT_WATCH_BIND=") == 1


def test_running_twice_does_not_duplicate_settings(sandbox):
    assert run_script(sandbox).returncode == 0
    assert run_script(sandbox).returncode == 0
    assert len(env_lines(sandbox, "CREDIT_WATCH_BIND")) == 1
    assert len(env_lines(sandbox, "CREDIT_WATCH_PORT")) == 0  # untouched: never set


def test_zero_bind_is_refused(sandbox):
    result = run_script(sandbox, "--bind", "0.0.0.0")
    assert result.returncode == 2
    assert "refusing 0.0.0.0" in result.stderr
    assert not sandbox["log"].exists()  # never reached compose


def test_unknown_flag_is_an_error(sandbox):
    result = run_script(sandbox, "--wat")
    assert result.returncode == 2
    assert "unknown option" in result.stderr


def test_help_documents_the_flags(sandbox):
    result = run_script(sandbox, "--help")
    assert result.returncode == 0
    for flag in ("--localhost", "--bind", "--port"):
        assert flag in result.stdout


def test_falls_back_to_loopback_when_no_lan_address_is_found(sandbox):
    write_bin(sandbox["bin"], "ip", "#!/usr/bin/env bash\nexit 0\n")  # no output
    result = run_script(sandbox)
    assert result.returncode == 0, result.stderr
    assert env_lines(sandbox, "CREDIT_WATCH_BIND") == ["CREDIT_WATCH_BIND=127.0.0.1"]
    assert "could not detect a LAN address" in result.stderr
    assert "compose up -d --build" in sandbox["log"].read_text()


def test_missing_compose_plugin_is_reported(sandbox):
    write_bin(
        sandbox["bin"],
        "docker",
        "#!/usr/bin/env bash\nexit 1\n",  # `docker compose version` fails
    )
    result = run_script(sandbox)
    assert result.returncode == 1
    assert "docker-compose-plugin" in result.stderr


def test_documented_entrypoints_match_this_script(sandbox):
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "./run.sh" in readme
    for flag in ("--localhost", "--bind", "--port"):
        assert flag in readme
    assert "CREDIT_WATCH_BIND" in (REPO / ".env.example").read_text(encoding="utf-8")


def test_quick_start_leads_with_compose_and_calls_the_options_alternatives():
    """The three entrypoints are alternatives; compose is documented first."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    quick_start = readme.split("## Quick start", 1)[1].split("\n## ", 1)[0]

    compose_at = quick_start.index("docker compose up -d")
    run_sh_at = quick_start.index("./run.sh")
    docker_run_at = quick_start.index("docker run -d")
    assert compose_at < run_sh_at, "compose must be the first option"
    assert compose_at < docker_run_at
    assert "Pick one" in quick_start
    assert "alternatives" in quick_start
