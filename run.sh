#!/usr/bin/env bash
# AI Credit Watch launcher.
#
#   ./run.sh                  detect this host's LAN IP and publish there (default)
#   ./run.sh --localhost      publish on 127.0.0.1 only
#   ./run.sh --bind 192.0.2.51  publish on a specific address
#   ./run.sh --port 9000      use a different host port
#
# Writes CREDIT_WATCH_BIND / CREDIT_WATCH_PORT into .env next to this script,
# then runs `docker compose up -d --build`. Keys stay in .env (never asked here).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SELF_DIR"

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

BIND=""
PORT=""
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -l|--localhost) BIND="127.0.0.1"; shift ;;
        -b|--bind)
            [ -n "${2:-}" ] || { echo "run.sh: --bind needs an address" >&2; exit 2; }
            BIND="$2"; shift 2 ;;
        -p|--port)
            [ -n "${2:-}" ] || { echo "run.sh: --port needs a number" >&2; exit 2; }
            PORT="$2"; shift 2 ;;
        *) echo "run.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
    esac
done

# First global IPv4 that is not a container/tunnel interface. Docker bridges are
# 172.x with names docker0/br-*, and tailscale/VPN interfaces are not LAN either.
detect_lan_ip() {
    command -v ip >/dev/null 2>&1 || return 1
    local candidates
    candidates="$(ip -4 -o addr show scope global 2>/dev/null \
        | awk '$2 !~ /^(lo|docker|br-|veth|tailscale|tun|zt|wg)/ {print $4}' \
        | cut -d/ -f1)"
    [ -n "$candidates" ] || return 1
    printf '%s\n' "${candidates%%$'\n'*}"
}

if [ -z "$BIND" ]; then
    if detected="$(detect_lan_ip)"; then
        BIND="$detected"
        echo "run.sh: publishing on LAN address $BIND (use --localhost for loopback only)"
    else
        BIND="127.0.0.1"
        echo "run.sh: could not detect a LAN address — falling back to 127.0.0.1 (use --bind <ip>)" >&2
    fi
fi

case "$BIND" in
    0.0.0.0|::) echo "run.sh: refusing 0.0.0.0 — bind the interface you actually want (--bind <ip>)" >&2; exit 2 ;;
esac

if [ ! -f .env ]; then
    umask 077
    cp .env.example .env
    chmod 600 .env 2>/dev/null || true
    echo "run.sh: created .env from .env.example — add your provider keys there"
fi

# Replace KEY=... in place, keeping every other line (and the file's permissions).
set_env_var() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    grep -v "^${key}=" .env > "$tmp" || true
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
    chmod 600 .env 2>/dev/null || true
}

set_env_var CREDIT_WATCH_BIND "$BIND"
if [ -n "$PORT" ]; then
    set_env_var CREDIT_WATCH_PORT "$PORT"
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "run.sh: docker not found — install Docker Engine + the compose plugin, then re-run." >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "run.sh: 'docker compose' (v2 plugin) not available — install docker-compose-plugin." >&2
    exit 1
fi

docker compose up -d --build

EFFECTIVE_PORT="$PORT"
if [ -z "$EFFECTIVE_PORT" ]; then
    EFFECTIVE_PORT="$(sed -n 's/^CREDIT_WATCH_PORT=//p' .env | tail -n1)"
fi
EFFECTIVE_PORT="${EFFECTIVE_PORT:-8760}"

echo
echo "AI Credit Watch is up:"
if [ "$BIND" = "127.0.0.1" ]; then
    echo "  http://127.0.0.1:${EFFECTIVE_PORT}    (this host only)"
else
    echo "  http://${BIND}:${EFFECTIVE_PORT}    (reachable from anything on that network)"
fi
echo "  keys:  edit .env in $SELF_DIR, then re-run ./run.sh"
echo "  auth:  not enabled — whoever can reach that URL can see the balances"
