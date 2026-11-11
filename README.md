# Sbmesh.com/king config files

**Notes:**

mapcycle.txt is for ALL MAPS server.. mapcycle3.txt is for nofun server

`default_plugins2.txt` / `3` / `5` / `6` are the AngelScript plugin lists for CSguns (`svenserver-2`), nofun (`svenserver-3`), InsALL (`svenserver-5`), and Testing (`svenserver-6`). Ban Manager edits the live copies in `serverfiles/svencoop/` and syncs these files on save. Disable a plugin by commenting out its `"plugin" { ... }` block.

hidden_nom_maps.txt in svencoop_addon/scripts/plugins/cfg are maps with a large nom cooldown that never randomly show up in the RTV menu (meme/hidden noms). A map should be in mapvote **or** hidden_nom, not both.

admins can create survival checkpoints with `.survival` command

## New maps: updatemaps.sh → gusjs `mapintake`

New maps from SCmapUpdater (scmapdb and the upload portal) now get a card in Discord #tasks. Staff pick the lists there (both servers, one server, hidden_nom, custom or skip), and gusjs on chicago edits these files, deploys them to chicago and london, runs `fix`, and commits and pushes here. Only the first map of a series is asked about. `updatemaps.sh` still works as a manual fallback.

## Merged map analysis

`analyze_merged_maps.py` scans BSP entity lumps (same data as `ripent -export`, without loading whole BSPs into RAM) to find merged series maps and unmerged parts that should move into `hidden_nom_maps{,3}.txt`.

```bash
# Enable 4G swap once (needs your sudo password) — image is preallocated at ~/swapfile
sudo bash setup_swap.sh

# Low-RAM dry-run (recommended): only opens merge-candidate BSPs
./analyze_merged_maps.py --fast

# Apply list edits after reviewing analyze_merged_maps_report.txt
./analyze_merged_maps.py --fast --apply

# Deploy mapcycle / mapvote / hidden_nom_maps (both server variants) to serverfiles
./deploy_maps.sh
# Optional: also rsync those lists to every other box in the fleet (auto-detected, see FLEET_HOSTS)
# SYNC_REMOTE=1 ./deploy_maps.sh
```

## Series map near-duplicate cleanup

`dedupe_series_maps.py` removes incomplete duplicate lines from `series_maps.txt` (same strict-subset idea as commit `b58d869`), scoring each candidate with BSP `trigger_changelevel` edges so removals are confirmed rather than name-guessed.

```bash
# Dry-run → dedupe_series_maps_report.txt
./dedupe_series_maps.py

# Rewrite series_maps.txt (keeps .bak)
./dedupe_series_maps.py --apply
```

Does **not** collapse high-overlap non-subsets (difficulty routes, mid-start forks, version packs). Those need a more aggressive mode if wanted later.
