#!/bin/bash
# Interactive new-map intake on Chicago, then deploy mapcycle/mapvote lists
# to local serverfiles and London. Git commit lives in this repo.

set -uo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$SCRIPT_DIR}"
DEST_DIR="${DEST_DIR:-/home/svenserver/serverfiles}"

NEWMAPS_FILE="${NEWMAPS_FILE:-/home/svenserver/newmapcycle.txt}"
REMOTE_COMMAND="${REMOTE_COMMAND:-/home/svenserver/fix}"

# London (same defaults as deploy_maps.sh)
REMOTE_HOST="${REMOTE_HOST:-svenserver@109.176.229.158}"
REMOTE_PORT="${REMOTE_PORT:-12515}"
REMOTE_PATH="${REMOTE_PATH:-/home/svenserver/serverfiles}"
REMOTE_NEWMAPS_FILE="${REMOTE_NEWMAPS_FILE:-/home/svenserver/newmapcycle.txt}"
SSH_CMD="${SSH_CMD:-ssh -p $REMOTE_PORT}"

MAX_ATTEMPTS=3
RETRY_DELAY=5

MAP_LIST_FILES=(
    "svencoop/mapcycle.txt"
    "svencoop/mapcycle3.txt"
    "svencoop_addon/scripts/plugins/cfg/mapvote.txt"
    "svencoop_addon/scripts/plugins/cfg/mapvote3.txt"
)

SERIES_MAPS_REL="svencoop_addon/scripts/plugins/RockTheVote/series_maps.txt"
# ─────────────────────────────────────────────────────────────────────────────

retry_command() {
    local attempt
    local exit_code=1

    for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
        "$@"
        exit_code=$?

        if [ "$exit_code" -eq 0 ]; then
            echo "  > Success."
            return 0
        fi

        echo "  > [Attempt $attempt/$MAX_ATTEMPTS] Command failed with code $exit_code."

        if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
            echo "  > Retrying in $RETRY_DELAY seconds..."
            sleep "$RETRY_DELAY"
        fi
    done

    echo "  [FAIL] Command failed after $MAX_ATTEMPTS attempts."
    return "$exit_code"
}

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

echo "=== updatemaps ==="
echo "  repo        : $REPO_DIR"
echo "  chicago dest: $DEST_DIR"
echo "  london      : $REMOTE_HOST:$REMOTE_PATH (port $REMOTE_PORT)"
echo ""

# === 1. Collect pending maps (Chicago + London) ===
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${NEWMAPS_FILE}.${TIMESTAMP}.bak"

if [ ! -f "$NEWMAPS_FILE" ]; then
    echo "Local $NEWMAPS_FILE missing; creating empty file."
    : > "$NEWMAPS_FILE"
fi

echo "Backing up local newmapcycle.txt -> $BACKUP_FILE"
cp "$NEWMAPS_FILE" "$BACKUP_FILE"

REMOTE_TMP=$(mktemp)
echo "Pulling newmapcycle.txt from London..."
if ! retry_command rsync -auvP -e "$SSH_CMD" \
    "${REMOTE_HOST}:${REMOTE_NEWMAPS_FILE}" \
    "$REMOTE_TMP"; then
    rm -f "$REMOTE_TMP"
    echo "Error: could not fetch London newmapcycle.txt"
    exit 1
fi

echo "Merging Chicago + London pending maps (sort -u)..."
sort -u "$NEWMAPS_FILE" "$REMOTE_TMP" -o "$NEWMAPS_FILE"
rm -f "$REMOTE_TMP"
echo "Pending list ready: $NEWMAPS_FILE"
echo ""

# === 2. Interactive add into repo map lists ===
echo "Processing pending maps against repo lists..."
while IFS= read -r line || [ -n "$line" ]; do
    line=$(trim "$line")
    echo "Processing line: '$line'"
    if [[ -z "$line" ]]; then
        echo "Skipping empty line"
        continue
    fi

    skip=false
    for rel in "${MAP_LIST_FILES[@]}"; do
        file="$REPO_DIR/$rel"
        if [ -f "$file" ] && grep -Fxi "$line" "$file" > /dev/null; then
            echo "Match found: '$line' already exists in $rel"
            skip=true
            break
        fi
    done

    if [ "$skip" = true ]; then
        continue
    fi

    echo "Add '$line' to mapcycle files? (y/n)"
    if ! read -r answer < /dev/tty; then
        echo "Skipping '$line' (no tty)"
        continue
    fi
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        for rel in "${MAP_LIST_FILES[@]}"; do
            file="$REPO_DIR/$rel"
            if [ ! -f "$file" ]; then
                echo "  [WARN] missing $rel — skip append"
                continue
            fi
            real_file=$(readlink -f "$file")
            sed -i -e '$a\' "$real_file"
            echo "$line" >> "$real_file"
        done
        echo "'$line' added to all mapcycle files"
    elif [[ "$answer" =~ ^[Nn]$ ]]; then
        echo "Skipping '$line' (user typed no)"
    else
        echo "Skipping '$line' (no input given)"
    fi
done < "$NEWMAPS_FILE"
echo "Local processing complete."
echo ""

# === 3. Deduplicate and sort repo lists ===
echo "Sorting and de-duplicating repo mapcycle files..."
for rel in "${MAP_LIST_FILES[@]}"; do
    file="$REPO_DIR/$rel"
    if [ ! -f "$file" ]; then
        echo "  [WARN] missing $rel"
        continue
    fi
    real_file=$(readlink -f "$file")
    LC_ALL=C sort -u -o "$real_file" "$real_file"
done
echo "Sort complete."
echo ""

# === 4. Deploy four lists to Chicago + London ===
echo "Deploying mapcycle/mapvote to Chicago and London..."
deploy_failed=0
for rel in "${MAP_LIST_FILES[@]}"; do
    src="$REPO_DIR/$rel"
    if [ ! -f "$src" ]; then
        echo "  [WARN] missing source: $rel"
        deploy_failed=1
        continue
    fi

    echo "--- $rel ---"
    mkdir -p "$DEST_DIR/$(dirname "$rel")"
    if ! retry_command rsync -auvP "$src" "$DEST_DIR/$rel"; then
        deploy_failed=1
    fi

    remote_dir="${REMOTE_HOST}:${REMOTE_PATH}/$(dirname "$rel")/"
    if ! retry_command rsync -auvP -e "$SSH_CMD" "$src" "$remote_dir"; then
        deploy_failed=1
    fi
done

echo "Running 'fix' on Chicago..."
if ! retry_command "$REMOTE_COMMAND"; then
    deploy_failed=1
fi

echo "Running 'fix' on London..."
if ! retry_command ssh -p "$REMOTE_PORT" -t "$REMOTE_HOST" "$REMOTE_COMMAND"; then
    deploy_failed=1
fi

if [ "$deploy_failed" -ne 0 ]; then
    echo "Error: deploy or fix failed. Not truncating newmapcycle.txt or committing."
    exit 1
fi
echo "Deploy complete."
echo ""

# === 5. Truncate pending lists on both servers ===
echo "Truncating $NEWMAPS_FILE on Chicago..."
truncate -s 0 "$NEWMAPS_FILE" || :
echo "Truncating $REMOTE_NEWMAPS_FILE on London..."
retry_command ssh -p "$REMOTE_PORT" -n "$REMOTE_HOST" "truncate -s 0 $REMOTE_NEWMAPS_FILE || :"
echo "Truncate complete."
echo ""

# === 6. Pull live series_maps.txt into the repo (Chicago only) ===
echo "Copying live series_maps.txt into repo..."
retry_command rsync -auvP \
    "$DEST_DIR/$SERIES_MAPS_REL" \
    "$REPO_DIR/$SERIES_MAPS_REL"
echo "series_maps.txt sync complete."
echo ""

# === 7. Commit only the map list files ===
cd "$REPO_DIR"
git add \
    "${MAP_LIST_FILES[@]}" \
    "$SERIES_MAPS_REL"

if git diff --cached --quiet; then
    echo "No map list changes to commit."
else
    git commit -m "mapcycle/mapvote files update"
    echo "Rebasing onto origin/main before push..."
    if git pull --rebase --autostash origin main && git push origin main; then
        echo "Push complete."
    else
        echo "Error: git pull --rebase / push failed. Local commit is kept; resolve and push manually."
        exit 1
    fi
fi

echo "All tasks finished."
