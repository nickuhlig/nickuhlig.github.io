"""
Fills in violated_restriction_ids for Albertsons-native / gap-fill recipes that don't have
it, by looking up each ingredient's restrictions in ingredient_master.json and taking the
union. This is the "de novo" replacement for what Mealime's own data gives genuine recipes.

WHERE IT FITS IN THE PIPELINE -- run it AFTER mealime_ingredient_index.py, not inside the
fetch script: a freshly fetched Albertsons recipe has ingredient_id: null on every line
until the index script assigns ids, and the lookup here is by id.

    1. fetch_albertsons_native_recipes.py   (recipes land with null ingredient ids)
    2. mealime_ingredient_index.py          (fills / assigns ids; new ingredients enter the master with violates: null)
    3. albertsons_restriction_backfill.py   (this script)

Semantics, matching the rest of the library:
  violated_restriction_ids  None = unknown, [] = verified to violate nothing, list = violations.
  A recipe is only decided when EVERY ingredient line has an id AND that ingredient's "violates" in the
  master is decided (a list, possibly empty -- not null). If even one isn't, the recipe is left as None,
  never guessed, and the blocking ingredients are listed in restriction_backfill_report.json -- a review
  queue. Fill in their "violates" in ingredient_master.json and re-run.

Only records whose violated_restriction_ids is None are touched, and only records from Albertsons
sources; existing lists (Mealime's own, or sweep-derived) are never changed.

Usage:
    python albertsons_restriction_backfill.py --recipes-dir "D:\\...\\free-recipe-database\\families" --dry-run
    python albertsons_restriction_backfill.py --recipes-dir "D:\\...\\free-recipe-database\\families"
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from ingredient_master import IngredientMaster, load_master, master_path_for

ALBERTSONS_SOURCES = {"albertsons_native", "albertsons_native_gap_fill"}
DEFAULT_REPORT_PATH = Path(__file__).with_name("restriction_backfill_report.json")
NON_RECIPE_FILENAMES = {
    "ingredient_index.json", "ingredient_restrictions.json", "ingredient_aliases.json",
    "ingredient_master.json", "ingredient_resolution_report.json", DEFAULT_REPORT_PATH.name,
}


def infer_restrictions(ingredients: list, master: IngredientMaster):
    """Returns (sorted restriction ids, []) when every ingredient is decided, otherwise
    (None, [(ingredient id, name) that blocked the answer])."""
    blockers = []
    violated = set()
    for ing in ingredients or []:
        if not isinstance(ing, dict):
            continue
        ing_id = ing.get("ingredient_id")
        decided = None if ing_id is None else master.violates(ing_id)
        if decided is None:
            blockers.append((ing_id, ing.get("name")))
        else:
            violated |= decided
    if blockers or not ingredients:
        return None, blockers
    return sorted(violated), []


def run(recipes_dir: Path, master_path: Path, report_path: Path, dry_run: bool) -> None:
    master = IngredientMaster(load_master(master_path))
    decided = sum(1 for e in master.entries.values() if e["violates"] is not None)
    print(f"Loaded {len(master.entries)} ingredients from {master_path.name} ({decided} decided, "
          f"{len(master.entries) - decided} with violates: null).")
    outcomes = Counter()
    blocked = {}            # (ingredient_id, name) -> number of recipe files it blocked
    restriction_counts = Counter()
    scanned = 0

    for path in recipes_dir.rglob("*.json"):
        if path.name in NON_RECIPE_FILENAMES:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        scanned += 1
        if not isinstance(record, dict) or record.get("source") not in ALBERTSONS_SOURCES:
            continue
        if record.get("violated_restriction_ids") is not None:
            outcomes["already had data (untouched)"] += 1
            continue
        result, blockers = infer_restrictions(record.get("ingredients"), master)
        if result is None:
            outcomes["left unknown (an ingredient is undecided)"] += 1
            for key in set(blockers):
                blocked[key] = blocked.get(key, 0) + 1
            continue
        outcomes["violates nothing" if not result else "violates >= 1"] += 1
        restriction_counts.update(result)
        record["violated_restriction_ids"] = result
        if not dry_run:
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    verb = "Would set" if dry_run else "Set"
    print(f"\nScanned {scanned} files. Albertsons records:")
    for label, count in outcomes.most_common():
        print(f"  {count:6d}  {label}")
    print(f"\n{verb} violated_restriction_ids on "
          f"{outcomes['violates nothing'] + outcomes['violates >= 1']} file(s). "
          f"Restriction counts among them: {dict(sorted(restriction_counts.items()))}")
    if dry_run:
        print("Dry run: no files were changed.")
    if blocked:
        print(f"{len(blocked)} distinct ingredient(s) blocked a decision -- see the report.")
    report = {
        "outcomes": dict(outcomes),
        "restriction_counts": dict(sorted(restriction_counts.items())),
        "needs_review": [
            {"ingredient_id": ing_id, "name": name, "recipe_files_blocked": count}
            for (ing_id, name), count in sorted(blocked.items(), key=lambda item: -item[1])
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {report_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Infer violated_restriction_ids for Albertsons recipes from ingredient_master.json.")
    parser.add_argument("--recipes-dir", type=Path, required=True)
    parser.add_argument("--master", type=Path, default=None,
                        help="Master ingredient file (default: ingredient_master.json at the root of --recipes-dir)")
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Compute and report, but write no recipe files.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.recipes_dir, args.master or master_path_for(args.recipes_dir), args.report_path, args.dry_run)
