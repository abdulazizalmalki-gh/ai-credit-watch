#!/usr/bin/env bash
# Install this repo's git hooks (currently: the pre-push privacy sweep).
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
hooks_dir="$root/.git/hooks"
mkdir -p "$hooks_dir"

installed=0
for hook in "$root"/dev/hooks/*; do
    [ -f "$hook" ] || continue
    name="$(basename "$hook")"
    install -m 0755 "$hook" "$hooks_dir/$name"
    echo "installed .git/hooks/$name"
    installed=$((installed + 1))
done

if [ "$installed" -eq 0 ]; then
    echo "no hooks found under dev/hooks/" >&2
    exit 1
fi

echo "done — 'git push' now runs the privacy sweep first"
