#!/usr/bin/env bash
# Put `mousectl` (and the legacy `ashark` / `vt3pro` names) on your PATH.
# No packages, no venv: the launcher runs the repo with the system python3.
# An existing non-symlink file at a target path is kept as <name>.orig.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BIN="${BIN:-$HOME/.local/bin}"
mkdir -p "$BIN"

for name in mousectl ashark vt3pro; do
    dest="$BIN/$name"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
        mv "$dest" "$dest.orig"
        echo "kept previous $dest as $dest.orig"
    fi
    ln -sfn "$ROOT/bin/mousectl" "$dest"
    echo "linked $dest -> $ROOT/bin/mousectl"
done

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "note: $BIN is not on your PATH" ;;
esac

echo
echo "If 'mousectl list' shows your mouse but reading it fails with a permission"
echo "error, run 'mousectl install-udev' and follow the two commands it prints."
