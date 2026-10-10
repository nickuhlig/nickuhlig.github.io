"""
Copies a filtered subset of an already-scraped Mealime recipe library (produced by
mealime_json_scrape.py) into a new folder, keeping the families/<recipe_id>/ folder
structure intact.

Each recipe file records its own "units" ("Metric" or "US") and "serving_count" (2,
4, or 6) -- every combination of the two is normally saved as a separate sibling
file within the same family folder. This lets you pull out just the combination you
actually want (e.g. US customary, 4 servings) without carrying the other 5
variants of every recipe along with it.

For each family folder that has at least one recipe matching your filters, this
copies:
  - every matching recipe json file (there's usually just one per family per
    units/servings combo, but occasionally more if a family has multiple distinct
    dishes/variants sharing a recipe_id)
  - alt_variants.json and thumbnail.<ext>, if present -- shared per-family files,
    copied once regardless of how many recipes in that family matched

Families with no matching recipe are skipped entirely (not created empty).

Usage:
    python mealime_library_subset.py --units US --servings 4 --out-dir "C:\\path\\to\\subset"
    python mealime_library_subset.py --recipes-dir "C:\\path\\to\\source" --units metric --servings 2 --out-dir "C:\\path\\to\\subset"
"""

import argparse
import json
import shutil
from pathlib import Path

DEFAULT_RECIPES_DIR = Path.home() / "Downloads" / "free_recipes_json"
FAMILIES_DIRNAME = "families"
ALT_VARIANTS_FILENAME = "alt_variants.json"
SHARED_FAMILY_FILENAMES = {ALT_VARIANTS_FILENAME}  # + thumbnail.* matched separately
NON_RECIPE_FILENAMES = {ALT_VARIANTS_FILENAME, "authenticated_index.json", "ingredient_index.json",
                        "ingredient_master.json"}

UNITS_ALIASES = {"metric": "Metric", "us": "US", "us_customary": "US"}


def normalize_units(value: str) -> str:
    key = value.strip().lower()
    if key not in UNITS_ALIASES:
        raise argparse.ArgumentTypeError(f"--units must be one of metric/US (got {value!r})")
    return UNITS_ALIASES[key]


def copy_subset(recipes_dir: Path, out_dir: Path, units: str, servings: int) -> None:
    families_src = recipes_dir / FAMILIES_DIRNAME
    if not families_src.is_dir():
        raise SystemExit(f"{families_src} not found -- is --recipes-dir correct?")

    families_out = out_dir / FAMILIES_DIRNAME
    families_out.mkdir(parents=True, exist_ok=True)

    families_scanned = 0
    families_matched = 0
    recipes_copied = 0

    for family_src in sorted(families_src.iterdir()):
        if not family_src.is_dir():
            continue
        families_scanned += 1

        matches = []
        for path in family_src.glob("*.json"):
            if path.name in NON_RECIPE_FILENAMES:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if record.get("units") == units and record.get("serving_count") == servings:
                matches.append(path)

        if not matches:
            continue

        families_matched += 1
        family_out = families_out / family_src.name
        family_out.mkdir(parents=True, exist_ok=True)

        for path in matches:
            shutil.copy2(path, family_out / path.name)
            recipes_copied += 1

        alt_path = family_src / ALT_VARIANTS_FILENAME
        if alt_path.exists():
            shutil.copy2(alt_path, family_out / ALT_VARIANTS_FILENAME)

        for thumb in family_src.glob("thumbnail.*"):
            shutil.copy2(thumb, family_out / thumb.name)

    print(f"Scanned {families_scanned} families.")
    print(f"Matched {families_matched} families ({units}, {servings} servings), "
          f"copied {recipes_copied} recipe file(s).")
    print(f"Saved to: {out_dir}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy a units/servings subset of a scraped Mealime recipe library to a new "
                    "folder, keeping the families/<recipe_id>/ structure."
    )
    parser.add_argument("--recipes-dir", type=Path, default=DEFAULT_RECIPES_DIR,
                        help=f"Source library folder (default: {DEFAULT_RECIPES_DIR})")
    parser.add_argument("--out-dir", type=Path, required=True,
                        help="Destination folder for the filtered subset (created if needed)")
    parser.add_argument("--units", type=normalize_units, required=True,
                        help="metric or US")
    parser.add_argument("--servings", type=int, required=True, choices=(2, 4, 6),
                        help="Serving size to keep: 2, 4, or 6")
    return parser.parse_args()


def main():
    args = parse_args()
    copy_subset(args.recipes_dir, args.out_dir, args.units, args.servings)


if __name__ == "__main__":
    main()
