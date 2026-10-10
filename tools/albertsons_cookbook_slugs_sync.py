"""
Stamps each recipe in the library with the Albertsons cookbook slugs the discovery sweep
found it under (a "cookbook_slugs" list, e.g. ["dessert", "super-simple"]), straight from
albertsons_full_sweep.json -- no live API calls -- AND adds the variety_tag_ids those
cookbooks imply, so a cookbook label and a variety tag are one system. recipe_export.py
turns variety_tag_ids into Norish tags (see mealime_id_reference.VARIETY_TAGS).

WHY THIS EXISTS: fetch_albertsons_native_recipes.py only used cookbook_slugs to derive
variety_tag_ids and kid_friendly and never wrote the list itself, so most cookbooks were
lost for every recipe already fetched. New fetches now store the list; this script covers
everything already in the library, and re-running it after a fresh sweep refreshes them.

WHICH RECIPES: the sweep isn't limited to Albertsons-native recipes -- it also lists
genuine Mealime recipes by their legacy_id, which is the same number as that variant's
"id" in the library. So this applies to genuine Mealime files too, not just
albertsons_native/albertsons_native_gap_fill ones.

DISH-LEVEL, NOT VARIANT-LEVEL: Albertsons lists ONE variant (one serving size, one units
system) of a dish in a cookbook, but cookbook membership describes the dish. So the slugs
of every sweep entry that lands on a dish -- identified as (recipe_id, group_label), the
same notion of "a dish" recipe_export.py's --servings fallback uses -- are unioned and
written to EVERY variant file of that dish. Without this, exporting --servings 4 would
drop the tags from any dish Albertsons happened to list at 2 servings.

VARIETY TAGS: for every dish it stamps, each cookbook that maps to a variety_tag_id
(mealime_id_reference.COOKBOOK_SLUG_TO_VARIETY_TAG_ID -- Mealime's own ten, plus our
Albertsons-only ids 35-43 for dessert, breakfast, snacks, ...) is ADDED to every
variant's variety_tag_ids if missing. Existing ids are never removed or reordered; a list
that was sorted stays sorted. "Kid friendly" (id 43, the kids-recipe-hub cookbook) is added
too -- also for any record whose kid_friendly flag is already True, so no existing tag is lost.
The kid_friendly boolean itself is left as it is.

Recipes the sweep never matched are left untouched (field absent = unknown): the sweep is
a capped snapshot, so "not found" doesn't mean "in no cookbook".

Only cookbook_slugs and variety_tag_ids are ever written; a file already correct is not
rewritten. Run with --dry-run first to see the counts.

Usage:
    python albertsons_cookbook_slugs_sync.py --recipes-dir "D:\\path\\to\\free-recipe-database" --sweep-json "albertsons_sweep_results\\albertsons_full_sweep.json" --dry-run
    python albertsons_cookbook_slugs_sync.py --recipes-dir "D:\\path\\to\\free-recipe-database" --sweep-json "albertsons_sweep_results\\albertsons_full_sweep.json"
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from mealime_id_reference import VARIETY_TAGS, variety_tag_ids_from_cookbook_slugs

FAMILIES_DIRNAME = "families"

Dish = tuple  # (recipe_id, group_label)


def scan_library(recipes_dir: Path):
    """(variant id -> its dish, dish -> every variant file of it, every variety id already in use).
    Reads each file once but keeps only a few small fields, since the library is ~27k files."""
    dish_by_id: dict[int, Dish] = {}
    files_by_dish: dict[Dish, list[Path]] = defaultdict(list)
    variety_ids_in_use: Counter = Counter()
    kid_friendly_dishes: set[Dish] = set()
    scanned = 0
    for fdir in (recipes_dir / FAMILIES_DIRNAME).iterdir():
        if not fdir.is_dir():
            continue
        for fpath in fdir.glob("*.json"):
            if fpath.name == "alt_variants.json":
                continue
            scanned += 1
            if scanned % 2000 == 0:
                print(f"...{scanned} files scanned")
            try:
                record = json.loads(fpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(record, dict) or "id" not in record or "recipe_id" not in record:
                continue
            dish = (record["recipe_id"], record.get("group_label"))
            dish_by_id[record["id"]] = dish
            files_by_dish[dish].append(fpath)
            variety_ids_in_use.update(record.get("variety_tag_ids") or [])
            if record.get("kid_friendly") is True:
                kid_friendly_dishes.add(dish)
    print(f"Scanned {scanned} files, {len(files_by_dish)} distinct dishes.")
    return dish_by_id, files_by_dish, variety_ids_in_use, kid_friendly_dishes


def slugs_by_dish(sweep: list[dict], dish_by_id: dict[int, Dish]) -> tuple[dict[Dish, set[str]], int]:
    result: dict[Dish, set[str]] = defaultdict(set)
    unmatched = 0
    for entry in sweep:
        dish = dish_by_id.get(entry.get("legacy_id"))
        if dish is None:
            unmatched += 1
            continue
        result[dish].update(entry.get("cookbook_slugs") or [])
    return result, unmatched


KID_FRIENDLY_VARIETY_ID = 43


def merged_variety_ids(existing, dish_slugs, kid_friendly: bool = False) -> tuple[list[int], list[int]]:
    """(the variety ids to store, the ids that were newly added). Existing ids are kept exactly
    as they are; a list that was sorted stays sorted. kid_friendly (an already-set flag) implies
    the "Kid friendly" id even if this dish's slugs don't currently include the kids hub."""
    existing = list(existing or [])
    wanted = set(variety_tag_ids_from_cookbook_slugs(dish_slugs))
    if kid_friendly:
        wanted.add(KID_FRIENDLY_VARIETY_ID)
    missing = [i for i in sorted(wanted) if i not in existing]
    if not missing:
        return existing, []
    merged = existing + missing
    if existing == sorted(existing):
        merged = sorted(merged)
    return merged, missing


def sync(recipes_dir: Path, sweep_json: Path, dry_run: bool) -> None:
    sweep = json.loads(sweep_json.read_text(encoding="utf-8"))
    dish_by_id, files_by_dish, variety_ids_in_use, kid_friendly_dishes = scan_library(recipes_dir)
    dish_slugs, unmatched = slugs_by_dish(sweep, dish_by_id)

    # Any variety id in use must be one we know. An unknown id above Mealime's range (e.g. the old
    # 1001-1009 numbering) means variety_tag_renumber.py hasn't been run yet.
    unknown = sorted(i for i in variety_ids_in_use if i not in VARIETY_TAGS)
    if unknown:
        raise SystemExit(f"variety_tag_ids in use that aren't in VARIETY_TAGS: {unknown} -- "
                         f"if these are 1001-1009, run variety_tag_renumber.py first")

    slugs_changed = variety_changed = files_touched = unchanged = 0
    added = Counter()
    dishes_gaining_variety = set()
    # Every dish the sweep matched, plus any dish that already has a kid_friendly file even though the
    # sweep never listed it (those get the Kid friendly id but no cookbook_slugs stamp).
    for dish in set(dish_slugs) | kid_friendly_dishes:
        matched = dish in dish_slugs
        new_slugs = sorted(dish_slugs[dish]) if matched else []
        for fpath in files_by_dish[dish]:
            record = json.loads(fpath.read_text(encoding="utf-8"))
            changed = False
            if matched and record.get("cookbook_slugs") != new_slugs:
                record["cookbook_slugs"] = new_slugs
                slugs_changed += 1
                changed = True
            merged, missing = merged_variety_ids(record.get("variety_tag_ids"), new_slugs,
                                                 kid_friendly=record.get("kid_friendly") is True)
            if missing:
                record["variety_tag_ids"] = merged
                added.update(missing)
                variety_changed += 1
                dishes_gaining_variety.add(dish)
                changed = True
            if not changed:
                unchanged += 1
                continue
            files_touched += 1
            if not dry_run:
                fpath.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nSweep entries: {len(sweep)} ({len(sweep) - unmatched} matched a library dish, "
          f"{unmatched} not in the library yet)")
    print(f"Dishes stamped: {len(dish_slugs)}")
    verb = "Would touch" if dry_run else "Updated"
    print(f"{verb} {files_touched} file(s) (already correct: {unchanged}): "
          f"{slugs_changed} need cookbook_slugs changed, {variety_changed} need variety_tag_ids added "
          f"({len(dishes_gaining_variety)} dishes)")
    if added:
        print("Variety ids added (files):")
        for variety_id, count in sorted(added.items()):
            print(f"  {variety_id:>5} {VARIETY_TAGS.get(variety_id, '?'):<26} {count}")
    if dry_run:
        print("Dry run: no files were changed.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Stamp cookbook_slugs from the Albertsons sweep onto library recipes and add the variety tags they imply."
    )
    parser.add_argument("--recipes-dir", type=Path, required=True,
                        help="Library folder containing families/ (e.g. free-recipe-database)")
    parser.add_argument("--sweep-json", type=Path, required=True,
                        help="albertsons_full_sweep.json")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sync(args.recipes_dir, args.sweep_json, args.dry_run)
