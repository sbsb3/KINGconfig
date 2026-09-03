#!/usr/bin/env python3
"""
Remove remaining near-duplicate lines from series_maps.txt.

Extends the strict-subset cleanup from KINGconfig commit b58d869, with BSP
trigger_changelevel analysis (same entity-lump approach as analyze_merged_maps.py)
to score/confirm that a shorter line is an incomplete duplicate of a longer one.

Default is dry-run: prints a report and writes dedupe_series_maps_report.txt.
Use --apply to rewrite series_maps.txt (keeps a .bak).

Confidence:
  HIGH   – proper subset AND consecutive changelevel edges confirm the shorter
           series (or it is a contiguous sublist of the longer line).
  MEDIUM – proper subset AND an order-preserving subsequence of the longer line
           (BSP missing/inconclusive).
  LOW    – proper subset only; no order or BSP evidence. Not removed unless
           --include-unconfirmed.

Examples:
  ./dedupe_series_maps.py
  ./dedupe_series_maps.py --apply
  ./dedupe_series_maps.py --apply --include-unconfirmed
"""

from __future__ import annotations

import argparse
import gc
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Reuse memory-light BSP entity lump reader from the merged-maps analyzer.
from analyze_merged_maps import (
    DEFAULT_MAP_DIRS,
    GC_EVERY,
    analyze_bsp_direct,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SERIES = SCRIPT_DIR / "svencoop_addon/scripts/plugins/RockTheVote/series_maps.txt"
DEFAULT_REPORT = SCRIPT_DIR / "dedupe_series_maps_report.txt"

# Fraction of consecutive pairs that must have a directed changelevel edge
# for BSP confirmation (HIGH).
EDGE_CONFIRM_RATIO = 0.5


def parse_series_file(path: Path) -> Tuple[List[str], List[dict]]:
    """Return (raw_lines_with_newlines, parsed series entries)."""
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    entries: List[dict] = []
    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        disabled = stripped.startswith("-")
        body = stripped[1:].lstrip() if disabled else stripped
        # Drop inline comments
        if "//" in body:
            body = body[: body.find("//")].rstrip()
        maps = body.split()
        if not maps:
            continue
        entries.append(
            {
                "idx": len(entries),
                "lineno": i + 1,  # 1-based in file
                "line_index": i,  # 0-based into raw_lines
                "disabled": disabled,
                "maps": maps,
                "set": set(maps),
                "raw": line.rstrip("\n\r"),
            }
        )
    return raw_lines, entries


def is_contiguous_sublist(shorter: List[str], longer: List[str]) -> bool:
    n, m = len(shorter), len(longer)
    if n == 0 or n > m:
        return False
    for start in range(m - n + 1):
        if longer[start : start + n] == shorter:
            return True
    return False


def is_order_preserving_subsequence(shorter: List[str], longer: List[str]) -> bool:
    """True if every map in shorter appears in longer in the same order (gaps ok)."""
    it = iter(longer)
    for name in shorter:
        for cand in it:
            if cand == name:
                break
        else:
            return False
    return True


def build_bsp_index(map_dirs: List[Path]) -> Dict[str, Path]:
    index: Dict[str, Path] = {}
    for d in map_dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*.bsp"):
            key = p.stem.lower()
            # Prefer first hit; addon usually listed before stock.
            if key not in index:
                index[key] = p
    return index


def load_changelevels(
    needed: Set[str], bsp_index: Dict[str, Path]
) -> Dict[str, List[str]]:
    """map_name -> list of trigger_changelevel targets (lowercase)."""
    out: Dict[str, List[str]] = {}
    for i, name in enumerate(sorted(needed)):
        path = bsp_index.get(name)
        if path is None:
            continue
        try:
            info = analyze_bsp_direct(path)
            out[name] = list(info.get("changelevels") or [])
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"  WARN: {name}: {exc}", file=sys.stderr)
        if (i + 1) % GC_EVERY == 0:
            gc.collect()
    return out


def undirected_components(edges: Dict[str, List[str]], nodes: Set[str]) -> Dict[str, int]:
    """Union-find over undirected changelevel links; return node -> component id."""
    parent: Dict[str, str] = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for src, targets in edges.items():
        if src not in parent:
            parent[src] = src
        for t in targets:
            if t not in parent:
                parent[t] = t
            union(src, t)

    # Compact ids
    roots = {find(n) for n in parent}
    root_id = {r: i for i, r in enumerate(sorted(roots))}
    return {n: root_id[find(n)] for n in parent}


def consecutive_edge_hits(
    maps: List[str], changelevels: Dict[str, List[str]]
) -> Tuple[int, int, int]:
    """
    Returns (hits, known_pairs, total_pairs).
    known_pairs = pairs where the source BSP was readable.
    hits = directed changelevel from maps[i] to maps[i+1].
    """
    hits = 0
    known = 0
    total = max(0, len(maps) - 1)
    for a, b in zip(maps, maps[1:]):
        if a not in changelevels:
            continue
        known += 1
        if b in changelevels[a]:
            hits += 1
    return hits, known, total


def same_component(
    maps: List[str], comp: Dict[str, int]
) -> Tuple[bool, Optional[int]]:
    ids = {comp[m] for m in maps if m in comp}
    if not ids:
        return False, None
    if len(ids) == 1:
        return True, next(iter(ids))
    return False, None


def classify_subset(
    shorter: dict,
    longer: dict,
    changelevels: Dict[str, List[str]],
    comp: Dict[str, int],
) -> Tuple[str, str]:
    """
    Return (confidence, reason).
    confidence in {HIGH, MEDIUM, LOW}.
    """
    s_maps: List[str] = shorter["maps"]
    l_maps: List[str] = longer["maps"]

    contiguous = is_contiguous_sublist(s_maps, l_maps)
    subsequence = is_order_preserving_subsequence(s_maps, l_maps)
    hits, known, total = consecutive_edge_hits(s_maps, changelevels)
    ok_comp, _ = same_component(s_maps, comp)

    edge_ratio = (hits / known) if known else 0.0
    bsp_confirms = known > 0 and edge_ratio >= EDGE_CONFIRM_RATIO

    if contiguous:
        return "HIGH", f"contiguous sublist of L{longer['lineno']}"
    if bsp_confirms and (ok_comp or known >= max(1, total // 2)):
        return (
            "HIGH",
            f"BSP edges {hits}/{known} known consecutive "
            f"(of {total}); same_component={ok_comp}",
        )
    if subsequence:
        return (
            "MEDIUM",
            f"order-preserving subsequence of L{longer['lineno']}"
            + (f"; BSP edges {hits}/{known}" if known else "; no BSP edges checked"),
        )
    if bsp_confirms:
        return (
            "HIGH",
            f"BSP edges {hits}/{known} despite non-subsequence ordering "
            f"vs L{longer['lineno']}",
        )
    return (
        "LOW",
        f"set subset of L{longer['lineno']} only"
        + (f"; BSP {hits}/{known}" if known else "; no BSP data"),
    )


def find_subset_duplicates(
    entries: List[dict],
    changelevels: Dict[str, List[str]],
    comp: Dict[str, int],
) -> List[dict]:
    """
    For each entry that is a proper subset of at least one other, pick the
    best (longest) superset and classify confidence.
    """
    results: List[dict] = []
    for a in entries:
        supers = [b for b in entries if a["idx"] != b["idx"] and a["set"] < b["set"]]
        if not supers:
            continue
        best = max(supers, key=lambda b: (len(b["set"]), -b["lineno"]))
        conf, reason = classify_subset(a, best, changelevels, comp)
        results.append(
            {
                "entry": a,
                "superset": best,
                "all_supersets": supers,
                "confidence": conf,
                "reason": reason,
            }
        )
    return results


def write_report(
    path: Path,
    results: List[dict],
    to_remove: List[dict],
    scanned: int,
    bsp_found: int,
) -> None:
    lines: List[str] = []
    lines.append("dedupe_series_maps report")
    lines.append("=" * 72)
    lines.append(f"Subset candidates: {len(results)}")
    lines.append(f"Marked for removal: {len(to_remove)}")
    lines.append(f"BSPs scanned: {bsp_found} / {scanned} needed names indexed-or-missing")
    lines.append("")

    by_conf: Dict[str, List[dict]] = defaultdict(list)
    for r in results:
        by_conf[r["confidence"]].append(r)

    for conf in ("HIGH", "MEDIUM", "LOW"):
        group = by_conf.get(conf, [])
        lines.append(f"--- {conf} ({len(group)}) ---")
        for r in group:
            a, b = r["entry"], r["superset"]
            action = "REMOVE" if r in to_remove else "keep"
            lines.append(
                f"  [{action}] L{a['lineno']} ({len(a['set'])} maps) "
                f"{a['maps'][0]}  ⊂  L{b['lineno']} ({len(b['set'])} maps) {b['maps'][0]}"
            )
            lines.append(f"           {r['reason']}")
            preview = " ".join(a["maps"][:10])
            if len(a["maps"]) > 10:
                preview += " ..."
            lines.append(f"           shorter: {preview}")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_removals(raw_lines: List[str], to_remove: List[dict], dest: Path) -> None:
    drop_indices = {r["entry"]["line_index"] for r in to_remove}
    bak = dest.with_suffix(dest.suffix + ".bak")
    bak.write_text("".join(raw_lines), encoding="utf-8")
    new_lines = [ln for i, ln in enumerate(raw_lines) if i not in drop_indices]
    # Ensure file ends with newline
    text = "".join(new_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    dest.write_text(text, encoding="utf-8")
    print(f"Wrote {dest} (backup: {bak})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--series",
        type=Path,
        default=DEFAULT_SERIES,
        help=f"Path to series_maps.txt (default: {DEFAULT_SERIES})",
    )
    ap.add_argument(
        "--maps-dir",
        action="append",
        dest="maps_dirs",
        type=Path,
        help="BSP directory (repeatable). Default: serverfiles svencoop*_maps paths.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite series_maps.txt (default is dry-run).",
    )
    ap.add_argument(
        "--include-unconfirmed",
        action="store_true",
        help="Also remove LOW-confidence set-only subsets.",
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"Report path (default: {DEFAULT_REPORT})",
    )
    ap.add_argument(
        "--skip-bsp",
        action="store_true",
        help="Skip BSP scan; classify using list order only (contiguous/subsequence).",
    )
    args = ap.parse_args()

    series_path: Path = args.series
    if not series_path.is_file():
        print(f"ERROR: series file not found: {series_path}", file=sys.stderr)
        return 1

    raw_lines, entries = parse_series_file(series_path)
    print(f"Loaded {len(entries)} series lines from {series_path}")

    # First pass: which entries are set-subsets? Only those need BSP data.
    subset_pairs = []
    for a in entries:
        supers = [b for b in entries if a["idx"] != b["idx"] and a["set"] < b["set"]]
        if supers:
            subset_pairs.append((a, supers))

    needed: Set[str] = set()
    for a, supers in subset_pairs:
        needed.update(a["maps"])
        for b in supers:
            needed.update(b["maps"])

    changelevels: Dict[str, List[str]] = {}
    comp: Dict[str, int] = {}
    bsp_found = 0
    if not args.skip_bsp and needed:
        map_dirs = args.maps_dirs if args.maps_dirs else list(DEFAULT_MAP_DIRS)
        print(f"Indexing BSPs under {len(map_dirs)} dir(s) for {len(needed)} map names...")
        bsp_index = build_bsp_index(map_dirs)
        present = {n for n in needed if n in bsp_index}
        print(f"Found {len(present)}/{len(needed)} BSPs; scanning entity lumps...")
        changelevels = load_changelevels(present, bsp_index)
        bsp_found = len(changelevels)
        # Component over all nodes we saw in edges + needed names
        nodes = set(needed)
        for src, tgts in changelevels.items():
            nodes.add(src)
            nodes.update(tgts)
        comp = undirected_components(changelevels, nodes)
        print(f"Parsed changelevels from {bsp_found} BSPs.")
    else:
        print("Skipping BSP scan.")

    results = find_subset_duplicates(entries, changelevels, comp)
    remove_confs = {"HIGH", "MEDIUM"}
    if args.include_unconfirmed:
        remove_confs.add("LOW")
    to_remove = [r for r in results if r["confidence"] in remove_confs]

    write_report(args.report, results, to_remove, len(needed), bsp_found)
    print(f"Report: {args.report}")
    print(
        f"Candidates: {len(results)} "
        f"(HIGH={sum(1 for r in results if r['confidence']=='HIGH')}, "
        f"MEDIUM={sum(1 for r in results if r['confidence']=='MEDIUM')}, "
        f"LOW={sum(1 for r in results if r['confidence']=='LOW')})"
    )
    print(f"Would remove: {len(to_remove)}")
    for r in to_remove:
        a = r["entry"]
        print(
            f"  - L{a['lineno']}: {' '.join(a['maps'][:8])}"
            f"{' ...' if len(a['maps']) > 8 else ''}  [{r['confidence']}]"
        )

    if args.apply:
        if not to_remove:
            print("Nothing to apply.")
            return 0
        apply_removals(raw_lines, to_remove, series_path)
        print(f"Removed {len(to_remove)} line(s).")
    else:
        print("Dry-run only. Re-run with --apply to rewrite series_maps.txt.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
