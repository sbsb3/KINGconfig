# Sbmesh.com/king config files

**Notes:**

mapcycle.txt is for ALL MAPS server.. mapcycle3.txt is for nofun server

hidden_nom_maps.txt in svencoop_addon/scripts/plugins/cfg are maps with a large nom cooldown that never randomly show up in the RTV menu (meme/hidden noms). A map should be in mapvote **or** hidden_nom, not both.

admins can create survival checkpoints with `.survival` command

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
# Optional: also rsync those lists to the second server
# SYNC_REMOTE=1 ./deploy_maps.sh
```
