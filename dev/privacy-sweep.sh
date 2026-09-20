#!/usr/bin/env bash
# Privacy sweep: fail if anything that looks private is about to be published.
#
#   ./dev/privacy-sweep.sh                  scan tracked files, all of git history
#                                           and commit messages
#   ./dev/privacy-sweep.sh --local-only     scan this branch's history only (used by
#                                           the pre-push hook, so an already-published
#                                           finding cannot block the push that fixes it)
#   ./dev/privacy-sweep.sh --staged         scan only what is staged (pre-commit use)
#   ./dev/privacy-sweep.sh --image <ref>    also grep a built image's filesystem
#
# Generic patterns live here so the check itself is safe to publish. Put your own
# values (machine names, tailnet, your subnet) in .privacy-deny.local — it is
# gitignored and read automatically when present.
set -uo pipefail

ROOT="${SWEEP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DENY_FILE="${PRIVACY_DENY_FILE:-$ROOT/.privacy-deny.local}"
cd "$ROOT" || exit 2

IMAGE=""
MODE="all"
RANGE="--all"
while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="${2:-}"; shift 2 ;;
        --staged) MODE="staged"; shift ;;
        # Scan only this branch's history. Used by the pre-push hook: findings that
        # already live in published refs must not block the push that fixes them.
        --local-only) RANGE="HEAD"; shift ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "privacy-sweep: unknown option '$1'" >&2; exit 2 ;;
    esac
done

# --- what counts as private ---------------------------------------------------
# RFC1918 + link-local + CGNAT/tailnet IPv4, private IPv6 (ULA/link-local),
# absolute home paths, key material, and anything from the local deny file.
BANNED='((^|[^0-9])(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}|169\.254\.[0-9]{1,3}\.[0-9]{1,3})([^0-9]|$))'
BANNED="$BANNED"'|(\bfd[0-9a-f]{2}:|\bfe80:)'
BANNED="$BANNED"'|(/home/[a-z][a-z0-9_-]*|/Users/[A-Za-z][A-Za-z0-9_-]*|C:\\Users\\[A-Za-z]+)'
BANNED="$BANNED"'|(\bsk-[A-Za-z0-9_-]{20,}|\bsk-or-v1-[A-Za-z0-9]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bglpat-[A-Za-z0-9_-]{16,}|\bAKIA[0-9A-Z]{16})'
BANNED="$BANNED"'|(-----BEGIN [A-Z ]*PRIVATE KEY-----)'

# Addresses that are legitimately in the docs: loopback, Docker's default bridge
# (identical on every machine, so not identifying) and the RFC5737 documentation
# ranges. There is deliberately NO exception for RFC1918 or CGNAT/tailnet space —
# "it is only a test fixture" is how a real address gets published. The digits
# following a match are checked too, so the docker-bridge entry cannot excuse a
# neighbouring address in the same subnet.
ALLOWED='(^|[^0-9])(127\.0\.0\.1|172\.17\.0\.1|172\.17\.255\.255|0\.0\.0\.0|192\.0\.2\.[0-9]{1,3}|198\.51\.100\.[0-9]{1,3}|203\.0\.113\.[0-9]{1,3})([^0-9]|$)'
ALLOWED="$ALLOWED"'|(/home/user|/Users/you|C:\\Users\\you)'

if [ -f "$DENY_FILE" ]; then
    extra="$(grep -vE '^[[:space:]]*(#|$)' "$DENY_FILE" | paste -sd'|' -)"
    [ -n "$extra" ] && BANNED="$BANNED|($extra)"
    echo "privacy-sweep: local deny file in use ($DENY_FILE)"
else
    echo "privacy-sweep: no $DENY_FILE — generic patterns only"
    echo "privacy-sweep: tip: put your machine names / subnet there (it is gitignored)"
fi

HITS=0

scan() {
    local label="$1" text="$2" hit
    while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        if printf '%s\n' "$hit" | grep -qE "$ALLOWED"; then
            continue
        fi
        printf '  HIT  %s: %s\n' "$label" "$hit"
        HITS=1
    done < <(printf '%s\n' "$text" | grep -oE "$BANNED" 2>/dev/null | head -n 200)
}

echo
echo "== scanning tracked files =="
if [ "$MODE" = "staged" ]; then
    files="$(git diff --cached --name-only --diff-filter=ACMR)"
else
    files="$(git ls-files)"
fi
if [ -z "$files" ]; then
    echo "  (nothing to scan)"
else
    for file in $files; do
        [ -f "$file" ] || continue
        scan "$file" "$(grep -aInE "$BANNED" "$file" 2>/dev/null | head -n 200)"
    done
fi

if [ "$MODE" != "staged" ]; then
    echo "== scanning every commit ever made (diff content, refs: $RANGE) =="
    scan "git-history" "$(git log $RANGE -p 2>/dev/null | grep -anE "$BANNED" | head -n 400)"

    echo "== scanning commit messages, author and committer =="
    scan "commit-meta" "$(git log $RANGE --format='%h %s%n%b%n%an <%ae>%n%cn <%ce>' 2>/dev/null | grep -anE "$BANNED" | head -n 200)"
fi

if [ -n "$IMAGE" ]; then
    echo "== scanning image filesystem: $IMAGE =="
    if ! command -v docker >/dev/null 2>&1; then
        echo "  skipped: docker is not available" >&2
    else
        # Scope is /app — the content this repo puts into the image. Scanning /etc
        # reports the base image's own documentation (openssl.cnf, access.conf,
        # networks, gai.conf all ship private-range examples) and, worse,
        # runtime-mounted files: /etc/resolv.conf inside a container is a copy of the
        # host's, so it echoes the operator's real DNS servers into the output.
        scan "image:$IMAGE" "$(docker run --rm --entrypoint sh "$IMAGE" -c "grep -rInaE '$BANNED' /app 2>/dev/null | head -n 200")"
    fi
fi

echo
if [ "$HITS" -ne 0 ]; then
    echo "privacy-sweep: FAILED — private-looking data found, do not publish"
    exit 1
fi
echo "privacy-sweep: PASSED — nothing private-looking in the scanned sources"
