"""
Merges one ingredient id into another: for ingredients the master holds twice under different ids
(e.g. "chili crisp" and "chili crunch"). Two things change, together:

  1. ingredient_master.json -- the source entry is removed and folded into the target: its spellings
     (with use counts), aliases and exact_only names are added to the target, so every name that used
     to resolve to the source now resolves to the target. The target keeps its own display name,
     restrictions and source. The merge refuses if the two entries have different "violates" (decide
     which is right first -- edit one -- then merge), unless --force.
  2. Every recipe file under --recipes-dir: each ingredient line with the source id gets the target id.
     Nothing else in a file changes (2-space JSON formatting, as the rest of the library).

Merging ids rewrites recipe files, so run --dry-run first. If this library is published or imported
elsewhere, remember the merged id disappears from every recipe that used it.

Usage:
    python ingredient_master_merge.py --from 1247 --into 1250 --recipes-dir "D:\\...\\free-recipe-database\\families" --dry-run
    python ingredient_master_merge.py --from 1247 --into 1250 --recipes-dir "D:\\...\\free-recipe-database\\families"
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from ingredient_master import load_master, master_path_for, save_master

NON_RECIPE_FILENAMES = {
    "alt_variants.json", "authenticated_index.json", "ingredient_index.json", "ingredient_master.json",
    "ingredient_resolution_report.json", "restriction_backfill_report.json",
}


def merge_entries(master: dict, source_id: str, target_id: str, force: bool) -> None:
    entries = master["ingredients"]
    if source_id not in entries or target_id not in entries:
        raise SystemExit(f"Both ids must be in the master (have source: {source_id in entries}, target: {target_id in entries}).")
    source, target = entries[source_id], entries[target_id]
    if sorted(source["violates"] or []) != sorted(target["violates"] or []) or (source["violates"] is None) != (target["violates"] is None):
        message = (f"'violates' differs: {source['name']!r} {source['violates']} vs {target['name']!r} {target['violates']}")
        if not force:
            raise SystemExit(f"Refusing to merge -- {message}. Fix one entry first, or pass --force to keep the target's.")
        print(f"  --force: {message}; keeping the target's.")
    for name, count in source["names"].items():
        target["names"][name] = target["names"].get(name, 0) + count
    for key in ("aliases", "exact_only"):
        for name in source.get(key, []):
            if name not in target[key]:
                target[key].append(name)
    target["recipes_seen"] = target.get("recipes_seen", 0) + source.get("recipes_seen", 0)
    note = f"merged from {source_id} ({source['name']})"
    target["note"] = f"{target['note']}; {note}" if target.get("note") else note
    del entries[source_id]


def rewrite_recipes(recipes_dir: Path, source_id: int, target_id: int, dry_run: bool):
    files_changed = lines_changed = scanned = 0
    per_source = Counter()
    for path in recipes_dir.rglob("*.json"):
        if path.name in NON_RECIPE_FILENAMES:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(record, dict):
            continue
        scanned += 1
        changed = 0
        for ing in record.get("ingredients") or []:
            if isinstance(ing, dict) and ing.get("ingredient_id") == source_id:
                ing["ingredient_id"] = target_id
                changed += 1
        if changed:
            files_changed += 1
            lines_changed += changed
            per_source[record.get("source") or "genuine"] += 1
            if not dry_run:
                path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return scanned, files_changed, lines_changed, per_source


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge one ingredient id into another (master + recipe files).")
    parser.add_argument("--from", dest="source_id", type=int, required=True, help="the id that disappears")
    parser.add_argument("--into", dest="target_id", type=int, required=True, help="the id that stays")
    parser.add_argument("--recipes-dir", type=Path, required=True)
    parser.add_argument("--master", type=Path, default=None,
                        help="Master ingredient file (default: ingredient_master.json at the root of --recipes-dir)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    parser.add_argument("--force", action="store_true", help="Merge even if the two entries' 'violates' differ.")
    args = parser.parse_args()
    args.master = args.master or master_path_for(args.recipes_dir)

    master = load_master(args.master)
    src, tgt = master["ingredients"].get(str(args.source_id)), master["ingredients"].get(str(args.target_id))
    if src and tgt:
        print(f"Merging {args.source_id} {src['name']!r} (violates {src['violates']}, {len(src['names'])} spelling(s))\n"
              f"   into {args.target_id} {tgt['name']!r} (violates {tgt['violates']}, {len(tgt['names'])} spelling(s))")
    merge_entries(master, str(args.source_id), str(args.target_id), args.force)
    scanned, files_changed, lines_changed, per_source = rewrite_recipes(args.recipes_dir, args.source_id, args.target_id, args.dry_run)
    verb = "Would change" if args.dry_run else "Changed"
    print(f"Scanned {scanned} recipe files. {verb} {lines_changed} ingredient line(s) in {files_changed} file(s): {dict(per_source)}")
    if args.dry_run:
        print("Dry run: nothing was written (master and recipes untouched).")
    else:
        save_master(master, args.master)
        print(f"Saved {args.master.name}: {len(master['ingredients'])} ingredients.")


if __name__ == "__main__":
    main()
