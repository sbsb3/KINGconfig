#!/usr/bin/env bash
# Create and enable a 4G swapfile. Requires sudo.
# Usage: sudo bash /home/svenserver/dev/KINGconfig/setup_swap.sh
set -euo pipefail

SIZE="${SWAP_SIZE:-4G}"
# Prefer /swapfile; fall back to a preallocated ~/swapfile if present.
if [[ -n "${SWAP_FILE:-}" ]]; then
  FILE="$SWAP_FILE"
elif [[ -f /swapfile ]]; then
  FILE=/swapfile
elif [[ -f /home/svenserver/swapfile ]]; then
  FILE=/home/svenserver/swapfile
else
  FILE=/swapfile
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  echo "A 4G image may already exist at /home/svenserver/swapfile — this script will enable it." >&2
  exit 1
fi

if swapon --show | grep -q .; then
  echo "Swap already active:"
  swapon --show
  free -h
  exit 0
fi

if [[ -f "$FILE" ]]; then
  echo "Using existing $FILE"
else
  echo "Creating $SIZE swapfile at $FILE ..."
  if command -v fallocate >/dev/null 2>&1; then
    fallocate -l "$SIZE" "$FILE" || dd if=/dev/zero of="$FILE" bs=1M count=4096 status=progress
  else
    dd if=/dev/zero of="$FILE" bs=1M count=4096 status=progress
  fi
fi

chmod 600 "$FILE"
mkswap "$FILE"
swapon "$FILE"

if ! grep -qE "^${FILE}[[:space:]]" /etc/fstab; then
  echo "$FILE none swap sw 0 0" >> /etc/fstab
  echo "Added $FILE to /etc/fstab"
fi

echo "---"
swapon --show
free -h
echo "Done."
