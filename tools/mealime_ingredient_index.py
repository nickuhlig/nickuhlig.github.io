"""
Fills in the "ingredient_id": null entries that authenticated-backfill recipes (the CDN path)
and Albertsons-native recipes (fetch_albertsons_native_recipes.py) are missing -- those paths
never had numeric ingredient ids at all, only name/quantity text -- using ingredient_master.json,
the master list of ingredients (see ingredient_master.py for the file shape and lookup rules).

Two passes, both purely local (no network requests, safe to re-run any time):

  1. SYNC   Scan --recipes-dir for every ingredient line that already has both an id and a name.
            An id the master doesn't know yet becomes a new master entry (violates: null, i.e.
            unreviewed); a known id seen under a new spelling gets that spelling added. This is
            how ids Mealime introduces in future scrapes reach the master.
  2. FILL   Re-scan; for every ingredient with ingredient_id: null, resolve its name (first hit wins):
                EXACT       a name or alias the master already has
                ALIAS       a hand-written alias, ignoring size words and plural endings
                NORMALIZED  a known name, ignoring size words (large/small/medium) and plural endings
                NEW         nothing matched: a new master entry, next id from 1000 up, violates: null
            and rewrite that recipe's file with the id. The same name always gets the same id, and
            new entries are saved to the master so later runs (and later fetches) reuse them.

Deliberately NOT ignored when matching: prep words (shredded, sliced, chopped, minced, diced --
they add a step to the recipe), "frozen" (Mealime keeps frozen and fresh as separate ids), "white",
and anything inside parentheses. "gluten-free", "vegan", "dairy-free" etc. are protected: a match may
never add or drop one. Aliases and exact_only names are edited by hand in the master itself.

Only ingredients whose id is null are touched in recipe files; an id already present is never changed.

New master entries have violates: null. They are your review queue: fill in "violates" in
ingredient_master.json (numeric restriction ids; [] for none) and re-run
albertsons_restriction_backfill.py -- recipes containing an undecided ingredient stay "unknown".

Usage:
    python mealime_ingredient_index.py --recipes-dir "C:\\path\\to\\recipes"
    python mealime_ingredient_index.py --recipes-dir "..." --dry-run    # full preview; writes no recipe files and does not save the master
    python mealime_ingredient_index.py --recipes-dir "..." --no-apply   # only the SYNC pass (updates the master, rewrites no recipes)

Every run writes ingredient_resolution_report.json next to this script: alias / normalized matches
(name -> the ingredient it matched), new entries, and normalized keys that were ambiguous between ids.
Review it after a --dry-run before the real run.
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from ingredient_master import (
    IngredientMaster, NEW_ID_START, load_master, master_path_for, normalize_name, save_master,
)

from library_paths import DEFAULT_LIBRARY_DIR as DEFAULT_RECIPES_DIR
DEFAULT_REPORT_PATH = Path(__file__).with_name("ingredient_resolution_report.json")
NON_RECIPE_FILENAMES = {
    "alt_variants.json", "authenticated_index.json", "ingredient_index.json", "ingredient_aliases.json",
    "ingredient_restrictions.json", "ingredient_master.json", DEFAULT_REPORT_PATH.name,
    "restriction_backfill_report.json",
}


def find_recipe_files(recipes_dir: Path):
    for path in recipes_dir.rglob("*.json"):
        if path.name in NON_RECIPE_FILENAMES:
            continue
        yield path


def load_recipe(path: Path):
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return record if isinstance(record, dict) else None


def sync_master(recipes_dir: Path, master: IngredientMaster) -> None:
    """Pass 1: teach the master every (id, name) pair the library already carries."""
    seen_pairs = set()
    scanned = 0
    for path in find_recipe_files(recipes_dir):
        record = load_recipe(path)
        if record is None:
            continue
        scanned += 1
        for ing in record.get("ingredients") or []:
            if not isinstance(ing, dict):
                continue
            ing_id, name = ing.get("ingredient_id"), ing.get("name")
            if ing_id is None or not name:
                continue
            pair = (ing_id, normalize_name(name))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            master.observe(ing_id, name)
    master.rebuild()
    print(f"Scanned {scanned} recipe files. Master now has {len(master.entries)} ingredients "
          f"({len(master.synced)} added from the library, {len(master.added_names)} new spelling(s) added).")


def apply_master(recipes_dir: Path, master: IngredientMaster, dry_run: bool):
    """Pass 2: fill every null ingredient_id."""
    filled = Counter()
    matches = defaultdict(Counter)       # how -> Counter((name, id))
    unresolved = 0
    files_changed = 0

    for path in find_recipe_files(recipes_dir):
        record = load_recipe(path)
        if record is None:
            continue
        changed = False
        for ing in record.get("ingredients") or []:
            if not isinstance(ing, dict) or ing.get("ingredient_id") is not None:
                continue
            name = ing.get("name")
            if not name:
                continue
            ing_id, how = master.resolve_or_create(name)
            if ing_id is None:
                unresolved += 1
                continue
            ing["ingredient_id"] = ing_id
            changed = True
            filled[how] += 1
            matches[how][(normalize_name(name), ing_id)] += 1
        if changed:
            files_changed += 1
            if not dry_run:
                path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    return filled, matches, unresolved, files_changed


def write_report(report_path: Path, master: IngredientMaster, matches, filled: Counter) -> None:
    def rows(how):
        return [
            {"name": name, "id": ing_id, "matched_to": master.display_name(ing_id), "lines": count}
            for (name, ing_id), count in sorted(matches[how].items(), key=lambda item: -item[1])
        ]

    report = {
        "lines_filled": dict(filled),
        "alias_matches": rows("alias"),
        "normalized_matches": rows("normalized"),
        "new_ids": rows("new"),
        "ambiguous_keys": master.ambiguous_keys,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {report_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fill in null ingredient_ids from ingredient_master.json (creating new master entries "
                    "for names it has never seen)."
    )
    parser.add_argument("--recipes-dir", type=Path, default=DEFAULT_RECIPES_DIR,
                        help=f"Folder containing scraped recipe JSON files (default: {DEFAULT_RECIPES_DIR})")
    parser.add_argument("--master", type=Path, default=None,
                        help="Master ingredient file (default: ingredient_master.json at the root of --recipes-dir)")
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH,
                        help=f"Where to write the resolution report (default: {DEFAULT_REPORT_PATH})")
    parser.add_argument("--no-apply", action="store_true",
                        help="Only sync the master from the library; don't fill ids or rewrite any recipe files.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything except writing: resolve ids, print the summary and write the "
                             "report, but leave every recipe file AND the master untouched.")
    return parser.parse_args()


def run(recipes_dir: Path, master_path: Path | None = None, report_path: Path = DEFAULT_REPORT_PATH,
        no_apply: bool = False, dry_run: bool = False) -> None:
    """Callable entry point (what main() below just wires up to argparse) -- also used directly by
    albertsons_pipeline.py so the orchestrator doesn't have to reimplement this logic. master_path
    defaults to ingredient_master.json at the root of recipes_dir."""
    master_path = master_path or master_path_for(recipes_dir)
    master = IngredientMaster(load_master(master_path))
    print(f"Loaded {len(master.entries)} ingredients from {master_path.name}.")
    sync_master(recipes_dir, master)

    if no_apply:
        if not dry_run:
            save_master(master.data, master_path)
            print(f"Saved: {master_path}")
        return

    filled, matches, unresolved, files_changed = apply_master(recipes_dir, master, dry_run)
    verb = "Would fill in" if dry_run else "Filled in"
    print(f"\n{verb} {sum(filled.values())} previously-null ingredient_id(s) across {files_changed} recipe file(s):")
    for how in ("exact", "alias", "normalized", "new"):
        print(f"  {how:<11} {filled[how]} line(s)")
    print(f"  new master entries created: {len(master.created)} (ids from {NEW_ID_START} up), all violates: null")
    if unresolved:
        print(f"{unresolved} ingredient mention(s) still unresolved (no usable name).")
    write_report(report_path, master, matches, filled)

    if dry_run:
        print("Dry run: no recipe files were changed and the master was not saved.")
    else:
        save_master(master.data, master_path)
        print(f"Saved: {master_path}")
    undecided = sum(1 for e in master.entries.values() if e["violates"] is None)
    if undecided:
        print(f"{undecided} master ingredient(s) have violates: null -- fill those in to decide recipes containing them.")


def main():
    args = parse_args()
    run(args.recipes_dir, args.master, args.report_path, no_apply=args.no_apply, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
