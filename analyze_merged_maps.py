#!/usr/bin/env python3
"""
Analyze Sven Co-op BSPs to find merged map-series maps vs their unmerged parts,
then (optionally) move superseded unmerged maps into hidden_nom_maps{,3}.txt
so only the merged versions stay in mapcycle / mapvote.

Entity data is read by seeking to the BSP entity lump only (same payload as
`ripent -export`) — the full BSP is never loaded into RAM. Pass --ripent to
shell out to ripent instead. Use --fast to only open merge-candidate BSPs.

Default is dry-run: prints a report and writes analyze_merged_maps_report.txt.
Use --apply to edit the staging list files under this repo.

If the machine has no swap, run once:  sudo bash setup_swap.sh

Examples:
  ./analyze_merged_maps.py --fast          # recommended: low RAM
  ./analyze_merged_maps.py                 # full scan, lump-only reads
  ./analyze_merged_maps.py --fast --apply
"""

from __future__ import annotations

import argparse
import gc
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Soft cap on entity-lump decode size (bytes). Real lumps are usually << 1 MiB.
MAX_ENT_LUMP = 4 * 1024 * 1024
# Run a full GC every N maps to keep RSS flat on long scans.
GC_EVERY = 250

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_MAP_DIRS = [
    Path("/home/svenserver/serverfiles/svencoop_addon/maps"),
    Path("/home/svenserver/serverfiles/svencoop/maps"),
    Path("/home/svenserver/serverfiles/svencoop_downloads/maps"),
]

DEFAULT_RIPENT = Path("/home/svenserver/serverfiles/svencoop/ripent")

# Staging list files (relative to KINGconfig root)
LIST_FILES = {
    "mapvote": Path("svencoop_addon/scripts/plugins/cfg/mapvote.txt"),
    "mapvote3": Path("svencoop_addon/scripts/plugins/cfg/mapvote3.txt"),
    "hidden": Path("svencoop_addon/scripts/plugins/cfg/hidden_nom_maps.txt"),
    "hidden3": Path("svencoop_addon/scripts/plugins/cfg/hidden_nom_maps3.txt"),
    "mapcycle": Path("svencoop/mapcycle.txt"),
    "mapcycle3": Path("svencoop/mapcycle3.txt"),
    "series": Path("svencoop_addon/scripts/plugins/RockTheVote/series_maps.txt"),
}

# Clear merge-name patterns
RE_RANGE_M = re.compile(r"^(?P<stem>.+?)(?P<start>\d+)-(?P<end>\d+)m$", re.I)
RE_RANGE = re.compile(r"^(?P<stem>.+?)(?P<start>\d+)-(?P<end>\d+)$", re.I)
RE_MERGED = re.compile(r"^(?P<stem>.+?)_?merged(?P<suffix>.*)$", re.I)

# Names that look like N-M but are chapter/part labels, not merges.
RANGE_FALSE_POSITIVES = {
    "assaultmesa2-2",
    "leprechaun3-2",
    "oprv1-1",
    "oprv1-2",
    "oprv2-1",
    "oprv2-2",
    "peanyworld_r_1-1",
    "peanyworld_r_1-2",
    "peanyworld_r_2-2",
    "peanyworldbeta1-1",
    "peanyworldbeta1-2",
    "sc_outpost3-1",
    "sc_tombofdeath_v12-1",
    "sc_tombofdeath_v12-2",
    "sc_tombofdeath_v12-3",
    "sc_tombofdeath_v12-4",
    "sc_tombofdeath_v12-5",
    "sc_tombofdeath_v12-6",
    "svor01-2",
    "tower1-5",
    "tox_surface5-1",
    "tox_surface5-2",
    "tox_surface8-0",
    "tox_surface8-1",
    "tox_surface8-2",
    "tox_xen1-1",
    "tox_xen1-2",
    "tox_xen3-1",
    "tox_xen3-2",
    "tox_xen3-3",
    "ub_megaman1-3",
}

# Manual merge -> unmerged parts (lowercase). Used when naming heuristics fail
# or when the merged name does not encode the part names cleanly.
PART_OVERRIDES: Dict[str, List[str]] = {
    "7hourswarep1-2m": ["7hourswar_ep1"],
    "adayinhell1-2m": ["adayinhelle1", "adayinhellf1"],
    "airport1-2m": ["airport_beta", "airport_2_beta"],
    "biglolly1-5m": ["biglolly_v1"],
    "darkvoyagep1-2m": ["darkvoyage", "darkvoyagept2"],
    "ze_letonguesc_labv3_1-2m": ["ze_letonguesc_lab_v3", "ze_letonguesc_lab2_v3"],
    "5waystodie1-2": ["5waystodie", "5waystodie2"],
    "hidoi_map1-2": ["hidoi_map1", "hidoi_map2"],
    "hlm_mission1-2": ["hlm_mission01", "hlm_mission02"],
    "jailbreak1-2": ["jailbreak", "jailbreak2"],
    "uboa_argentino1-2": ["uboa_argentino", "uboa_argentino2"],
    # Curated: merged nuclear-winter chain supersedes the classic split maps.
    "nwmerged0": [
        "nwintro", "nw0", "nw1", "nw2", "nw3", "nw4", "nw5", "nw6", "nw7",
        "nw8", "nw9", "nw9a", "y10", "nw10", "nw11", "nw12", "nw13", "nw14",
        "nw15", "nw16", "nw17", "nw18", "nw19", "nw20", "nw21", "nw22",
        "nw23", "nw24", "nw25",
    ],
}

# If a series line (from series_maps / changelevels) contains any of these
# merge stems, treat every non-merged map in that line as superseded.
MERGED_SERIES_STEMS = (
    "asmerged",
    "cad_merged",
    "po_merged",
    "pv_merged",
    "darkstar_merged",
    "nwmerged",
    "doomfest_merged",
    "normall_merged",
)


# ---------------------------------------------------------------------------
# BSP / ripent entity loading (memory-light)
# ---------------------------------------------------------------------------

_RE_KV = re.compile(r'^"([^"]+)"\s+"([^"]*)"')


def read_ent_lump_bytes(bsp_path: Path) -> bytes:
    """Read ONLY the entity lump — never the full BSP (maps can be 50–70 MiB)."""
    with bsp_path.open("rb") as f:
        header = f.read(12)
        if len(header) < 12:
            raise ValueError(f"BSP too small: {bsp_path}")
        offset = struct.unpack_from("i", header, 4)[0]
        length = struct.unpack_from("i", header, 8)[0]
        if offset < 0 or length < 0:
            raise ValueError(f"Bad entity lump header in {bsp_path}")
        if length > MAX_ENT_LUMP:
            length = MAX_ENT_LUMP
        f.seek(offset)
        return f.read(length)


def scan_ent_lump(raw: bytes) -> dict:
    """
    Stream-parse the entity lump for changelevels + merge markers.
    Does not retain per-entity dicts — only the few fields we need.
    """
    # Marker checks on raw bytes (no full unicode decode of the lump).
    markers = {
        "newbspguy": b"NewBSPGuy" in raw,
        "dup_classname": b"classname#1" in raw or b"mapversion#1" in raw,
    }

    changelevels: List[str] = []
    in_ent = False
    classname = ""
    map_val = ""

    # Decode line-by-line via split to avoid holding a giant str + list of dicts.
    # Entity lumps are ASCII; ignore undecodable bytes.
    text = raw.decode("ascii", "ignore")
    # Drop raw ASAP so only `text` lives until we finish scanning.
    del raw

    for line in text.splitlines():
        if not line:
            continue
        c0 = line[0]
        if c0 == "{":
            in_ent = True
            classname = ""
            map_val = ""
            continue
        if c0 == "}":
            if in_ent and "trigger_changelevel" in classname and map_val:
                changelevels.append(map_val.lower().strip())
            in_ent = False
            classname = ""
            map_val = ""
            continue
        if not in_ent:
            continue
        m = _RE_KV.match(line)
        if not m:
            # Fall back for odd spacing / comments stripped loosely
            if line.find("//") != -1:
                line = line[: line.find("//")]
            quotes = [i for i, ch in enumerate(line) if ch == '"']
            if len(quotes) < 4:
                continue
            key = line[quotes[0] + 1 : quotes[1]]
            value = line[quotes[2] + 1 : quotes[3]]
        else:
            key, value = m.group(1), m.group(2)
        if key == "classname":
            classname = value
        elif key == "map":
            map_val = value

    del text
    return {
        "changelevels": changelevels,
        "markers": markers,
    }


def analyze_bsp_direct(bsp_path: Path) -> dict:
    return scan_ent_lump(read_ent_lump_bytes(bsp_path))


def analyze_bsp_ripent(bsp_path: Path, ripent: Path, work_dir: Path) -> dict:
    """Export entities via ripent into a temp .ent and scan it (still no full BSP load)."""
    tmp_bsp = work_dir / bsp_path.name
    if not tmp_bsp.exists():
        try:
            tmp_bsp.symlink_to(bsp_path)
        except OSError:
            # Avoid copying multi-10MB BSPs — require symlink support.
            raise RuntimeError(
                f"Cannot symlink {bsp_path} into {work_dir}; "
                "refusing to copy large BSPs. Use direct mode (default)."
            )
    ent_path = tmp_bsp.with_suffix(".ent")
    subprocess.run(
        [str(ripent), "-export", str(tmp_bsp)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(work_dir),
    )
    if not ent_path.exists():
        alt = work_dir / (bsp_path.stem + ".ent")
        if alt.exists():
            ent_path = alt
        else:
            raise FileNotFoundError(f"ripent did not write {ent_path}")
    raw = ent_path.read_bytes()
    try:
        ent_path.unlink()
    except OSError:
        pass
    if len(raw) > MAX_ENT_LUMP:
        raw = raw[:MAX_ENT_LUMP]
    return scan_ent_lump(raw)


# ---------------------------------------------------------------------------
# Map discovery / series building
# ---------------------------------------------------------------------------

def discover_bsps(map_dirs: Sequence[Path]) -> Dict[str, Path]:
    """mapname_lower -> bsp path (first dir wins)."""
    found: Dict[str, Path] = {}
    for d in map_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if not f.name.lower().endswith(".bsp"):
                continue
            if "@" in f.name:
                continue
            key = f.stem.lower()
            found.setdefault(key, f)
    return found


def build_series_from_changelevels(
    changelevels: Dict[str, List[str]],
) -> List[List[str]]:
    """Rebuild series chains the same way series_maps.py does."""
    all_next: Set[str] = set()
    for targets in changelevels.values():
        all_next.update(targets)

    first_maps = [m for m in changelevels if m not in all_next]
    series_list: List[List[str]] = []
    for start in sorted(first_maps):
        maplist = [start]
        nextmaps = list(changelevels.get(start, []))
        while nextmaps:
            nxt: List[str] = []
            for nm in nextmaps:
                if nm in maplist:
                    continue
                maplist.append(nm)
                if nm in changelevels:
                    nxt.extend(changelevels[nm])
            nextmaps = nxt
        if len(maplist) > 1:
            series_list.append(maplist)
    return series_list


def load_series_file(path: Path) -> List[List[str]]:
    series: List[List[str]] = []
    if not path.exists():
        return series
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        # Curated lists may start with -label
        parts = line.split()
        if parts and parts[0].startswith("-"):
            parts = parts[1:]
        parts = [p.lower() for p in parts if p]
        if len(parts) > 1:
            series.append(parts)
    return series


def read_map_list(path: Path) -> List[str]:
    if not path.exists():
        return []
    out: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        out.append(s)
    return out


def write_map_list(path: Path, maps: Iterable[str]) -> None:
    # Preserve simple one-map-per-line, sorted case-insensitive unique.
    unique = sorted({m for m in maps}, key=lambda s: s.lower())
    path.write_text("\n".join(unique) + ("\n" if unique else ""), encoding="utf-8")


# ---------------------------------------------------------------------------
# Merge detection + part resolution
# ---------------------------------------------------------------------------

def is_merge_name(name: str) -> bool:
    n = name.lower()
    if n in RANGE_FALSE_POSITIVES:
        return False
    if "merged" in n:
        return True
    if RE_RANGE_M.match(n):
        return True
    return False


def is_probable_range_merge(name: str, info: Optional[dict], all_bsp: Set[str]) -> bool:
    """N-M without trailing m: treat as merge only with supporting evidence."""
    n = name.lower()
    if n in RANGE_FALSE_POSITIVES:
        return False
    if is_merge_name(n):
        return False
    m = RE_RANGE.match(n)
    if not m:
        return False
    stem = m.group("stem")
    start, end = int(m.group("start")), int(m.group("end"))
    if end <= start:
        return False
    # Evidence: NewBSPGuy markers, or no changelevels + resolvable parts exist.
    markers = (info or {}).get("markers", {})
    changes = (info or {}).get("changelevels", [])
    parts, _ = guess_parts(stem, start, end, all_bsp)
    if markers.get("newbspguy") or markers.get("dup_classname"):
        return len(parts) >= 2
    if not changes and len(parts) >= 2:
        return True
    # Explicit override
    if n in PART_OVERRIDES:
        return True
    return False


def guess_parts(stem: str, start: int, end: int, all_bsp: Set[str]) -> Tuple[List[str], List[int]]:
    found: List[str] = []
    missing: List[int] = []
    stem = stem.lower().rstrip("_")

    def opts_for(i: int) -> List[str]:
        opts: List[str] = []
        if start == 1 and i == 1:
            opts += [
                stem,
                f"{stem}1",
                f"{stem}_1",
                f"{stem}01",
                f"{stem}_01",
                f"{stem}p1",
                f"{stem}_p1",
                f"{stem}ep1",
                f"{stem}_ep1",
                f"{stem}pt1",
                f"{stem}_pt1",
            ]
        opts += [
            f"{stem}{i}",
            f"{stem}_{i}",
            f"{stem}{i:02d}",
            f"{stem}_{i:02d}",
            f"{stem}p{i}",
            f"{stem}_p{i}",
            f"{stem}ep{i}",
            f"{stem}_ep{i}",
            f"{stem}pt{i}",
            f"{stem}_pt{i}",
        ]
        return opts

    for i in range(start, end + 1):
        hit = None
        for o in opts_for(i):
            if o in all_bsp:
                hit = o
                break
        if hit:
            if hit not in found:
                found.append(hit)
        else:
            missing.append(i)
    return found, missing


def resolve_parts_for_merge(
    merge_name: str,
    all_bsp: Set[str],
    info: Optional[dict] = None,
) -> Tuple[List[str], str]:
    """Return (parts, method)."""
    n = merge_name.lower()
    if n in PART_OVERRIDES:
        parts = [p for p in PART_OVERRIDES[n] if p in all_bsp or True]
        # Keep override entries even if bsp missing (still useful for list edits)
        return list(PART_OVERRIDES[n]), "override"

    m = RE_RANGE_M.match(n)
    if m:
        stem, a, b = m.group("stem"), int(m.group("start")), int(m.group("end"))
        parts, _ = guess_parts(stem, a, b, all_bsp)
        return parts, "range-m"

    if is_probable_range_merge(n, info, all_bsp):
        m2 = RE_RANGE.match(n)
        assert m2
        stem, a, b = m2.group("stem"), int(m2.group("start")), int(m2.group("end"))
        parts, _ = guess_parts(stem, a, b, all_bsp)
        return parts, "range"

    return [], "none"


def series_superseded_by_merged(
    series_lists: Sequence[Sequence[str]],
) -> Dict[str, str]:
    """
    Map unmerged_map -> representative merged_map for series lines that mix
    *merged* maps with split maps (e.g. asmerged* + asmap*).
    """
    superseded: Dict[str, str] = {}
    for series in series_lists:
        merged_in = [m for m in series if is_merge_name(m) or any(m.startswith(s) for s in MERGED_SERIES_STEMS)]
        if not merged_in:
            continue
        # Only treat as a merge-supersession series if it also has non-merged maps
        # OR the merge stem is one of the known multi-map merged campaigns.
        non_merged = [
            m for m in series
            if not is_merge_name(m) and not any(m.startswith(s) for s in MERGED_SERIES_STEMS)
        ]
        if not non_merged:
            continue
        rep = merged_in[0]
        for m in non_merged:
            superseded.setdefault(m, rep)
    return superseded


# ---------------------------------------------------------------------------
# List mutation
# ---------------------------------------------------------------------------

def move_to_hidden(
    maps_to_hide: Set[str],
    root: Path,
    apply: bool,
) -> Dict[str, dict]:
    """
    Remove maps from mapvote/mapcycle (both server variants) and add them to
    the matching hidden_nom_maps file. Returns per-file change summary.
    """
    pairs = [
        ("mapvote", "hidden"),
        ("mapvote3", "hidden3"),
        ("mapcycle", "hidden"),
        ("mapcycle3", "hidden3"),
    ]
    summary: Dict[str, dict] = {}

    # Load once
    lists: Dict[str, List[str]] = {
        key: read_map_list(root / rel) for key, rel in LIST_FILES.items() if key != "series"
    }

    hide_lower = {m.lower() for m in maps_to_hide}

    for src_key, hidden_key in pairs:
        src = lists[src_key]
        hidden = lists[hidden_key]
        hidden_lower = {m.lower() for m in hidden}

        removed = [m for m in src if m.lower() in hide_lower]
        already_hidden = [m for m in removed if m.lower() in hidden_lower]
        to_add = [m for m in removed if m.lower() not in hidden_lower]

        new_src = [m for m in src if m.lower() not in hide_lower]
        new_hidden = list(hidden) + to_add

        summary[src_key] = {
            "removed": sorted(removed, key=str.lower),
            "added_to": hidden_key,
            "newly_hidden": sorted(to_add, key=str.lower),
            "already_hidden": sorted(already_hidden, key=str.lower),
        }

        if apply:
            write_map_list(root / LIST_FILES[src_key], new_src)
            # Defer hidden writes until both sources for that hidden file processed
            lists[src_key] = new_src
            lists[hidden_key] = new_hidden

    if apply:
        write_map_list(root / LIST_FILES["hidden"], lists["hidden"])
        write_map_list(root / LIST_FILES["hidden3"], lists["hidden3"])

    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--config-root",
        type=Path,
        default=SCRIPT_DIR,
        help="KINGconfig root (default: directory of this script)",
    )
    ap.add_argument(
        "--maps-dir",
        type=Path,
        action="append",
        dest="maps_dirs",
        help="BSP directory (repeatable). Defaults to serverfiles svencoop{,_addon,_downloads}/maps",
    )
    ap.add_argument(
        "--ripent",
        nargs="?",
        const=str(DEFAULT_RIPENT),
        default=None,
        help="Use ripent -export instead of direct BSP reads. Optional path to ripent binary.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes: move superseded unmerged maps into hidden_nom_maps{,3}.txt "
             "and remove them from mapvote/mapcycle (both variants).",
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write full report to this path (default: <config-root>/analyze_merged_maps_report.txt)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only scan the first N BSPs (debug)",
    )
    ap.add_argument(
        "--fast",
        action="store_true",
        help="RAM/CPU saver: only open BSPs that look like merges (N-Mm / *merged* / "
             "ambiguous N-M). Series chains come from series_maps.txt, not a full rescan.",
    )
    args = ap.parse_args()

    root: Path = args.config_root.resolve()
    map_dirs = args.maps_dirs if args.maps_dirs else DEFAULT_MAP_DIRS
    report_path = args.report or (root / "analyze_merged_maps_report.txt")

    print(f"Config root: {root}")
    print(f"Map dirs: {[str(d) for d in map_dirs]}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Entity source: {'ripent ' + args.ripent if args.ripent else 'direct entity lump (seek+read)'}")
    print(f"Scan scope: {'FAST (merge candidates only)' if args.fast else 'ALL BSPs'}")

    bsps = discover_bsps(map_dirs)
    all_names = sorted(bsps)
    if args.limit:
        all_names = all_names[: args.limit]

    if args.fast:
        names = [
            n for n in all_names
            if is_merge_name(n) or RE_RANGE.match(n)
        ]
    else:
        names = all_names

    print(f"Found {len(bsps)} BSPs (scanning {len(names)})")

    # Minimal retained state: name -> {changelevels, markers}. Never store ents/text.
    changelevels: Dict[str, List[str]] = {}
    infos: Dict[str, dict] = {}
    scan_errors: List[str] = []

    ripent_work: Optional[Path] = None
    ripent_bin: Optional[Path] = None
    if args.ripent:
        ripent_bin = Path(args.ripent)
        if not ripent_bin.is_file():
            print(f"ERROR: ripent not found: {ripent_bin}", file=sys.stderr)
            return 1
        ripent_work = Path(tempfile.mkdtemp(prefix="ripent_scan_"))

    try:
        for idx, name in enumerate(names):
            path = bsps[name]
            if idx % 200 == 0 or idx + 1 == len(names):
                print(f"  scanning {idx + 1}/{len(names)}: {name}", end="\r", flush=True)
            try:
                if ripent_bin and ripent_work:
                    info = analyze_bsp_ripent(path, ripent_bin, ripent_work)
                else:
                    info = analyze_bsp_direct(path)
                # Keep only what later stages need (no entity list, no raw text).
                slim = {
                    "changelevels": info["changelevels"],
                    "markers": info["markers"],
                }
                infos[name] = slim
                if slim["changelevels"]:
                    changelevels[name] = slim["changelevels"]
            except Exception as e:  # noqa: BLE001 - keep scanning
                scan_errors.append(f"{name}: {e}")
            if (idx + 1) % GC_EVERY == 0:
                gc.collect()
        print()
        gc.collect()
    finally:
        if ripent_work and ripent_work.exists():
            shutil.rmtree(ripent_work, ignore_errors=True)

    all_bsp_names = set(bsps)
    scanned_series = build_series_from_changelevels(changelevels)
    file_series = load_series_file(root / LIST_FILES["series"])
    # Prefer the on-disk series_maps.txt (includes curated -label lists) and
    # fall back to freshly scanned chains for anything missing.
    series_lists = list(file_series) + [
        s for s in scanned_series
        if s[0] not in {x[0] for x in file_series}
    ]

    # --- Identify merged maps ---
    merged: Dict[str, dict] = {}
    for name in sorted(all_bsp_names):
        info = infos.get(name)
        if is_merge_name(name) or is_probable_range_merge(name, info, all_bsp_names):
            parts, method = resolve_parts_for_merge(name, all_bsp_names, info)
            # Filter parts: never list the merge itself
            parts = [p for p in parts if p != name]
            markers = (info or {}).get("markers", {})
            merged[name] = {
                "parts": parts,
                "method": method,
                "markers": markers,
                "changelevels": (info or {}).get("changelevels", []),
                "reason": (
                    "name-merged" if "merged" in name else
                    "name-range-m" if RE_RANGE_M.match(name) else
                    "name-range+evidence"
                ),
            }

    # Parts implied by name/override
    superseded: Dict[str, str] = {}  # unmerged -> merge
    for merge_name, meta in merged.items():
        for p in meta["parts"]:
            # Don't supersede other merges
            if is_merge_name(p):
                continue
            superseded.setdefault(p, merge_name)

    # Parts implied by mixed merged/unmerged series lines
    for unmerged, rep in series_superseded_by_merged(series_lists).items():
        if is_merge_name(unmerged):
            continue
        superseded.setdefault(unmerged, rep)

    # nwmerged1..N should share nwmerged0's override parts (already set via override on 0);
    # also mark any other nwmerged* as merged without re-adding parts.
    for name in list(all_bsp_names):
        if name.startswith("nwmerged") and name != "nwmerged0" and "nwmerged0" in PART_OVERRIDES:
            for p in PART_OVERRIDES["nwmerged0"]:
                superseded.setdefault(p, "nwmerged0")

    # Unmerged series (length > 1) that have NO merged replacement for any map
    unmerged_series: List[List[str]] = []
    unmerged_series_with_merge: List[Tuple[List[str], Set[str]]] = []
    for series in series_lists:
        merge_hits = {m for m in series if m in merged or is_merge_name(m)}
        # Also count if any map in series is superseded
        super_hits = {m for m in series if m in superseded}
        if merge_hits or super_hits:
            unmerged_series_with_merge.append((series, merge_hits | {superseded[m] for m in super_hits}))
        else:
            if len(series) > 1 and not any(is_merge_name(m) for m in series):
                unmerged_series.append(series)

    # Maps to hide: superseded unmerged maps that currently sit in vote/cycle
    vote = {m.lower() for m in read_map_list(root / LIST_FILES["mapvote"])}
    vote3 = {m.lower() for m in read_map_list(root / LIST_FILES["mapvote3"])}
    cycle = {m.lower() for m in read_map_list(root / LIST_FILES["mapcycle"])}
    cycle3 = {m.lower() for m in read_map_list(root / LIST_FILES["mapcycle3"])}
    hidden = {m.lower() for m in read_map_list(root / LIST_FILES["hidden"])}
    hidden3 = {m.lower() for m in read_map_list(root / LIST_FILES["hidden3"])}

    in_active = vote | vote3 | cycle | cycle3
    already_hidden_all = hidden & hidden3

    def merge_is_votable(merge_name: str) -> bool:
        """True if this merge (or another map in the same merged campaign) is in vote/cycle."""
        if merge_name in in_active:
            return True
        for stem in MERGED_SERIES_STEMS:
            if merge_name.startswith(stem):
                return any(m.startswith(stem) and m in in_active for m in merged)
        return False

    to_hide: Set[str] = set()
    hide_details: List[Tuple[str, str, str]] = []  # map, merge, where
    skipped_merge_not_votable: List[Tuple[str, str]] = []
    for unmerged, merge_name in sorted(superseded.items()):
        if unmerged not in in_active:
            continue
        # Keep the merged map itself votable; only hide unmerged parts
        if unmerged in merged or is_merge_name(unmerged):
            continue
        # Only hide when a merged replacement is actually votable/cycleable
        # (otherwise we'd strip the only votable entry for that series).
        if not merge_is_votable(merge_name):
            skipped_merge_not_votable.append((unmerged, merge_name))
            continue
        where = []
        if unmerged in vote:
            where.append("mapvote")
        if unmerged in vote3:
            where.append("mapvote3")
        if unmerged in cycle:
            where.append("mapcycle")
        if unmerged in cycle3:
            where.append("mapcycle3")
        if unmerged in hidden:
            where.append("hidden")
        if unmerged in hidden3:
            where.append("hidden3")
        to_hide.add(unmerged)
        hide_details.append((unmerged, merge_name, ",".join(where)))

    # Merged maps that are NOT series starts (mid-series / continuation packs).
    # Only first maps belong in mapvote — e.g. nwmerged0 yes, nwmerged3 no;
    # sc_avp1-4m yes, sc_avp5-7m no when a lower-range merge of the same stem exists.
    merged_non_first: Set[str] = set()
    all_changelevel_targets: Set[str] = set()
    for targets in changelevels.values():
        all_changelevel_targets.update(targets)
    for series in series_lists:
        for m in series[1:]:
            merged_non_first.add(m)
            all_changelevel_targets.add(m)

    for m in merged:
        if m in all_changelevel_targets:
            merged_non_first.add(m)
        # Range continuation packs: stem5-7m when stem1-4m also exists
        rm = RE_RANGE_M.match(m)
        if rm:
            stem = rm.group("stem").lower()
            start = int(rm.group("start"))
            if start > 1:
                for other in merged:
                    rm2 = RE_RANGE_M.match(other)
                    if (
                        rm2
                        and rm2.group("stem").lower() == stem
                        and int(rm2.group("start")) < start
                    ):
                        merged_non_first.add(m)
                        break

    merged_first = sorted(m for m in merged if m not in merged_non_first)

    # First-of-series merged maps missing from vote (candidates to add to mapvote)
    merged_missing_vote = [
        m for m in merged_first
        if m not in vote and m not in vote3 and m in all_bsp_names
    ]

    # --- Report ---
    lines: List[str] = []
    lines.append(f"Merged map analysis — {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Config root: {root}")
    lines.append(f"BSPs scanned: {len(infos)} / {len(bsps)} (errors: {len(scan_errors)})")
    lines.append(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    lines.append("")

    lines.append("=" * 72)
    lines.append(f"MERGED MAPS DETECTED ({len(merged)})")
    lines.append("=" * 72)
    for name, meta in sorted(merged.items()):
        mk = meta["markers"]
        mark = []
        if mk.get("newbspguy"):
            mark.append("NewBSPGuy")
        if mk.get("dup_classname"):
            mark.append("classname#1")
        mark_s = ",".join(mark) if mark else "-"
        parts_s = " ".join(meta["parts"]) if meta["parts"] else "(no parts resolved)"
        lines.append(
            f"  {name:40} reason={meta['reason']:18} method={meta['method']:10} "
            f"markers={mark_s:20} parts: {parts_s}"
        )

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"UNMERGED SERIES WITH NO MERGED VERSION ({len(unmerged_series)})")
    lines.append("=" * 72)
    for s in sorted(unmerged_series, key=lambda x: x[0]):
        lines.append("  " + " ".join(s))

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"SUPERSEDED UNMERGED MAPS IN mapvote/mapcycle → move to hidden ({len(to_hide)})")
    lines.append("=" * 72)
    for unmerged, merge_name, where in hide_details:
        lines.append(f"  {unmerged:40} superseded_by={merge_name:30} currently_in={where}")

    if skipped_merge_not_votable:
        lines.append("")
        lines.append("=" * 72)
        lines.append(
            f"SKIPPED (merge exists on disk but is not in mapvote/mapcycle) ({len(skipped_merge_not_votable)})"
        )
        lines.append("=" * 72)
        for unmerged, merge_name in skipped_merge_not_votable[:80]:
            lines.append(f"  {unmerged:40} would_need_votable={merge_name}")
        if len(skipped_merge_not_votable) > 80:
            lines.append(f"  ... +{len(skipped_merge_not_votable) - 80} more")

    lines.append("")
    lines.append("=" * 72)
    lines.append(
        f"FIRST MERGED MAPS NOT IN mapvote/mapvote3 ({len(merged_missing_vote)}) — add these only"
    )
    lines.append("=" * 72)
    for m in merged_missing_vote:
        lines.append(f"  {m}")


    if scan_errors:
        lines.append("")
        lines.append("=" * 72)
        lines.append(f"SCAN ERRORS ({len(scan_errors)})")
        lines.append("=" * 72)
        for e in scan_errors[:50]:
            lines.append(f"  {e}")

    report_text = "\n".join(lines) + "\n"
    report_path.write_text(report_text, encoding="utf-8")
    print(report_text)
    print(f"Report written to {report_path}")

    if not to_hide:
        print("Nothing to move into hidden_nom_maps.")
        return 0

    summary = move_to_hidden(to_hide, root, apply=args.apply)
    print("")
    print("=" * 72)
    print("LIST FILE CHANGES" + ("" if args.apply else " (dry-run preview)"))
    print("=" * 72)
    for key, info in summary.items():
        print(f"  {key}: remove {len(info['removed'])} "
              f"(+{len(info['newly_hidden'])} new → {info['added_to']}, "
              f"{len(info['already_hidden'])} already hidden)")
        for m in info["removed"][:30]:
            print(f"      - {m}")
        if len(info["removed"]) > 30:
            print(f"      ... +{len(info['removed']) - 30} more")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to edit the staging list files.")
    else:
        print("\nApplied. Review git diff in KINGconfig, then deploy the list files to the server.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
