"""
Compares the Albertsons recipe ids harvested by new_site_json_scrape.py against an
existing Mealime recipe library (default: D:\\Google Drive\\GitHub Pages\\
free-recipe-database, the canonical published database -- pass --library-dir to point
at a different one instead, e.g. a local scratch copy) to see which of them are
already saved locally and which are new.

The library's families/<parent_id>/ folders hold one file per saved recipe variant,
named "<variant_id>_<slug>.<ext>" (mealime_json_scrape.py writes .json; some libraries
-- e.g. ones re-exported for Paprika -- use .paprikarecipe instead, so both are
recognized). <parent_id> is Mealime's numeric recipe-family id and <variant_id> is
Mealime's numeric per-serving/unit-system variant id -- the same `parent_id`/`legacy_id`
pair new_site_json_scrape.py records for each Albertsons recipe.

Since Albertsons' own slug carries a disambiguating numeric suffix Mealime's own slug
doesn't (e.g. "...-crusty-bread-10" vs. "...-crusty-bread"), slugs are compared with
that suffix stripped rather than exact-matched.

For each Albertsons recipe this reports one of:
  - "variant_exists"   -- this exact variant id is already saved in that family folder
  - "new_variant"       -- the family folder exists, but not this specific variant
  - "new_family"        -- no family folder exists locally at all

Writes two files:
  - albertsons_library_diff.json  -- every checked record, tagged with its status
  - albertsons_new_recipes.json   -- just the non-"variant_exists" records (full record,
    not just the id), ready to feed straight into
    fetch_albertsons_native_recipes.py --ids-json (which does its own dedup on top of
    this, so re-running fetch against this file is always safe).

Usage:
    python albertsons_library_diff.py --albertsons-ids albertsons_recipe_ids.json
    python albertsons_library_diff.py --albertsons-ids albertsons_recipe_ids.json --library-dir "D:\\Google Drive\\Recipes"
"""

import argparse
import json
import re
from pathlib import Path

from library_paths import DEFAULT_LIBRARY_DIR
FAMILIES_DIRNAME = "families"
VARIANT_FILENAME_RE = re.compile(r"^(\d+)_(.+)\.(?:json|paprikarecipe)$")
TRAILING_NUMERIC_SUFFIX_RE = re.compile(r"-\d+$")


def strip_disambiguating_suffix(slug: str) -> str:
    return TRAILING_NUMERIC_SUFFIX_RE.sub("", slug)


def scan_library(library_dir: Path) -> dict[str, dict]:
    """Returns {family_id_str: {"variant_ids": {int, ...}, "slugs": {str, ...}}}."""
    families_dir = library_dir / FAMILIES_DIRNAME
    if not families_dir.is_dir():
        raise SystemExit(f"{families_dir} not found -- is --library-dir correct?")

    library: dict[str, dict] = {}
    for family_dir in families_dir.iterdir():
        if not family_dir.is_dir():
            continue
        variant_ids = set()
        slugs = set()
        for path in family_dir.iterdir():
            match = VARIANT_FILENAME_RE.match(path.name)
            if not match:
                continue
            variant_ids.add(int(match.group(1)))
            slugs.add(match.group(2))
        library[family_dir.name] = {"variant_ids": variant_ids, "slugs": slugs}
    return library


def diff(albertsons_recipes: list[dict], library: dict[str, dict]) -> list[dict]:
    results = []
    for recipe in albertsons_recipes:
        family_id = str(recipe["parent_id"])
        family = library.get(family_id)
        stripped_slug = strip_disambiguating_suffix(recipe["slug"])

        if family is None:
            status = "new_family"
        elif recipe["legacy_id"] in family["variant_ids"]:
            status = "variant_exists"
        elif stripped_slug in family["slugs"]:
            # Same dish, different variant id than any saved so far (e.g. a serving
            # size/unit combo not yet fetched) -- still worth fetching.
            status = "new_variant"
        else:
            status = "new_variant"

        results.append({**recipe, "status": status})
    return results


def run(albertsons_ids: Path, library_dir: Path = DEFAULT_LIBRARY_DIR, out_dir: Path | None = None) -> Path:
    """Callable entry point (what main() below just wires up to argparse) -- also used directly by
    albertsons_pipeline.py so the orchestrator doesn't have to reimplement this logic. Returns the
    path to albertsons_new_recipes.json (fetch_albertsons_native_recipes.py's --ids-json input)."""
    albertsons_recipes = json.loads(albertsons_ids.read_text(encoding="utf-8"))
    library = scan_library(library_dir)
    results = diff(albertsons_recipes, library)

    out_dir = out_dir or albertsons_ids.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    counts = {"variant_exists": 0, "new_variant": 0, "new_family": 0}
    for r in results:
        counts[r["status"]] += 1

    report_path = out_dir / "albertsons_library_diff.json"
    report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    new_recipes_path = out_dir / "albertsons_new_recipes.json"
    new_recipes = [r for r in results if r["status"] != "variant_exists"]
    new_recipes_path.write_text(json.dumps(new_recipes, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Library: {len(library)} families scanned from {library_dir}")
    print(f"Albertsons recipes checked: {len(results)}")
    print(f"  already have (variant_exists): {counts['variant_exists']}")
    print(f"  new variant of an existing family (new_variant): {counts['new_variant']}")
    print(f"  entirely new family (new_family): {counts['new_family']}")
    print(f"\nFull diff: {report_path}")
    print(f"New recipes (feed into fetch_albertsons_native_recipes.py --ids-json): {new_recipes_path}")
    return new_recipes_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--albertsons-ids", type=Path, required=True,
                        help="albertsons_recipe_ids.json produced by new_site_json_scrape.py")
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR,
                        help="Root of the existing Mealime library (parent of its families/ folder) "
                             f"(default: {DEFAULT_LIBRARY_DIR})")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Where to write the diff report and new-ids list "
                             "(default: alongside --albertsons-ids)")
    args = parser.parse_args()
    run(args.albertsons_ids, args.library_dir, args.out_dir)


if __name__ == "__main__":
    main()
