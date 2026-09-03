#!/bin/bash
set -euo pipefail

# Deploy map list text files from KINGconfig into a serverfiles-style tree.
# Mirrors deploy_as.sh conventions (DEST_DIR staging/live, optional SYNC_REMOTE)
# but only ships mapcycle / mapvote / hidden_nom_maps (both server variants).
#
# Usage:
#   ./deploy_maps.sh
#   DEST_DIR=/home/svenserver/as ./deploy_maps.sh
#   SYNC_REMOTE=1 ./deploy_maps.sh
#
# Override paths:
#   REPO_DIR=/path/to/KINGconfig DEST_DIR=/path/to/serverfiles ./deploy_maps.sh

# ── Configuration ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$SCRIPT_DIR}"

# Default: live serverfiles on this box (where the game reads these lists).
# Override to a staging dir if you prefer an intermediate copy step.
DEST_DIR="${DEST_DIR:-/home/svenserver/serverfiles}"

# ── Remote sync (opt-in) ─────────────────────────────────────────────────────
# Additive rsync of the same relative paths to a second server. Off by default.
# Uses rsync -auvP: -u skips dest files newer than the source.
SYNC_REMOTE="${SYNC_REMOTE:-0}"
REMOTE_HOST="${REMOTE_HOST:-svenserver@109.176.229.158}"
REMOTE_PORT="${REMOTE_PORT:-12515}"
REMOTE_PATH="${REMOTE_PATH:-/home/svenserver/serverfiles}"
# ─────────────────────────────────────────────────────────────────────────────

# Paths relative to REPO_DIR / DEST_DIR (svencoop[_addon]/... layout).
MAP_LIST_FILES=(
    "svencoop/mapcycle.txt"
    "svencoop/mapcycle3.txt"
    "svencoop_addon/scripts/plugins/cfg/mapvote.txt"
    "svencoop_addon/scripts/plugins/cfg/mapvote3.txt"
    "svencoop_addon/scripts/plugins/cfg/hidden_nom_maps.txt"
    "svencoop_addon/scripts/plugins/cfg/hidden_nom_maps3.txt"
)

# rsync -auvP: archive, only-update-if-newer, verbose, progress — same as deploy_as.sh.
safe_sync_file() {
    rsync -auvP "$1" "$2"
}

echo "=== Map list deploy ==="
echo "  source      : $REPO_DIR"
echo "  destination : $DEST_DIR"
echo ""

copied=0
missing=0

for rel in "${MAP_LIST_FILES[@]}"; do
    src="$REPO_DIR/$rel"
    dest="$DEST_DIR/$rel"
    if [[ ! -f "$src" ]]; then
        echo "  [WARN] missing source: $rel"
        missing=$((missing + 1))
        continue
    fi
    mkdir -p "$(dirname "$dest")"
    safe_sync_file "$src" "$dest"
    echo "  [txt] $rel"
    copied=$((copied + 1))
done

echo ""
echo "Done. $copied file(s) synced to $DEST_DIR ($missing missing)."

if [[ "$SYNC_REMOTE" == "1" ]]; then
    echo ""
    echo "=== Syncing map lists to remote: $REMOTE_HOST:$REMOTE_PATH (port $REMOTE_PORT) ==="
    for rel in "${MAP_LIST_FILES[@]}"; do
        src="$DEST_DIR/$rel"
        if [[ ! -f "$src" ]]; then
            echo "  [WARN] skip remote (not in DEST_DIR): $rel"
            continue
        fi
        remote_dir="$REMOTE_HOST:$REMOTE_PATH/$(dirname "$rel")/"
        rsync -auvP -e "ssh -p $REMOTE_PORT" "$src" "$remote_dir"
        echo "  [remote] $rel"
    done
    echo "Remote sync done."
fi
