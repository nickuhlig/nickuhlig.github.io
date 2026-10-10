"""
Builds a fetch_albertsons_native_recipes.py --ids-json file listing every
Albertsons-sourced recipe ALREADY in your library, for re-fetching with --overwrite --
e.g. to pick up a fix to that script (like the per-step quantity fix) in recipes it
wrote before the fix existed.

WHY NOT albertsons_library_diff.py's own output (albertsons_new_recipes.json): that
file is deliberately the OPPOSITE list -- ids the sweep found that AREN'T in your
library yet. Feeding it to --overwrite looks like it's doing something (it'll happily
fetch those genuinely-new ones) but silently skips every already-fetched recipe, since
they were never in that list to begin with -- confirmed: it undershot a 1,102-recipe
re-fetch down to just 591 new ones.

HOW THIS WORKS: scans your library for every file with source "albertsons_native" or
"albertsons_native_gap_fill" (skipping Metric-variant files, since the US variant's own
"id" field already IS the legacy_id -- see fetch_albertsons_native_recipes.py's module
docstring), then looks each legacy_id up in albertsons_full_sweep.json for the
Albertsons RECIPE... id, parent_id, name, and cookbook_slugs that
fetch_albertsons_native_recipes.py's target records need. A legacy_id no longer present
in the sweep (the sweep is a point-in-time snapshot; Albertsons could have removed a
recipe since) is reported but left out, rather than guessed at.

Usage:
    python albertsons_build_refetch_ids.py --recipes-dir "D:\\path\\to\\free-recipe-database" --sweep-json "albertsons_sweep_results\\albertsons_full_sweep.json" --out-json "albertsons_sweep_results\\albertsons_refetch_existing_ids.json"
    python fetch_albertsons_native_recipes.py --ids-json albertsons_sweep_results\\albertsons_refetch_existing_ids.json --out-dir "D:\\path\\to\\free-recipe-database" --overwrite
"""

import argparse
import json
from pathlib import Path

FAMILIES_DIRNAME = "families"
ALBERTSONS_SOURCES = {"albertsons_native", "albertsons_native_gap_fill"}


def existing_albertsons_legacy_ids(recipes_dir: Path) -> dict[int, str]:
    """legacy_id -> source, for every US-variant Albertsons-sourced file in the
    library (the US variant's own "id" field is the real legacy_id; the Metric
    sibling's is a synthetic legacy_id*10+1 -- see synthetic_metric_variant_id() --
    so only US variants are read here to avoid double-counting)."""
    result = {}
    families_dir = recipes_dir / FAMILIES_DIRNAME
    for fdir in families_dir.iterdir():
        if not fdir.is_dir():
            continue
        for fpath in fdir.glob("*.json"):
            if fpath.name == "alt_variants.json":
                continue
            try:
                record = json.loads(fpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(record, dict) or record.get("source") not in ALBERTSONS_SOURCES:
                continue
            if record.get("units") != "US":
                continue
            result[record["id"]] = record["source"]
    return result


def build_targets(recipes_dir: Path, sweep_json: Path) -> tuple[list[dict], list[int]]:
    sweep = json.loads(sweep_json.read_text(encoding="utf-8"))
    sweep_by_legacy = {r["legacy_id"]: r for r in sweep if r.get("legacy_id") is not None}

    existing = existing_albertsons_legacy_ids(recipes_dir)
    targets = []
    missing = []
    for legacy_id in sorted(existing):
        entry = sweep_by_legacy.get(legacy_id)
        if entry is None:
            missing.append(legacy_id)
            continue
        targets.append({
            "id": entry["id"],
            "legacy_id": entry["legacy_id"],
            "parent_id": entry["parent_id"],
            "name": entry["name"],
            "cookbook_slugs": entry.get("cookbook_slugs") or [],
            "violated_restriction_ids": entry.get("violated_restriction_ids"),
        })
    return targets, missing


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build a --ids-json file of Albertsons-sourced recipes already in "
                    "your library, for re-fetching with --overwrite."
    )
    parser.add_argument("--recipes-dir", type=Path, required=True,
                        help="Library folder containing families/ (e.g. free-recipe-database)")
    parser.add_argument("--sweep-json", type=Path, required=True,
                        help="albertsons_full_sweep.json (or equivalent) to look up fetch metadata in")
    parser.add_argument("--out-json", type=Path, required=True,
                        help="Where to write the resulting --ids-json file")
    return parser.parse_args()


def main():
    args = parse_args()
    targets, missing = build_targets(args.recipes_dir, args.sweep_json)
    args.out_json.write_text(json.dumps(targets, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Built {len(targets)} re-fetchable target(s) -> {args.out_json}")
    if missing:
        print(f"{len(missing)} existing legacy_id(s) not found in the sweep (left out): {missing[:10]}"
              f"{'...' if len(missing) > 10 else ''}")


if __name__ == "__main__":
    main()
