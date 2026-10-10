"""
Generates manifest.json: a single small file mapping every recipe id in the library
to its family/units/servings/name, for the GitHub Pages export tool (index.html in
this same project) to use.

WHY THIS EXISTS: the GitHub Pages tool needs to know which recipe files match a
units/servings filter *before* fetching any file content, since GitHub's API can list
a repo's files in one call but can't tell you a file's units/servings without reading
it -- and reading all ~25,000 recipe files individually just to filter them would be
slow and could approach GitHub's 5,000-requests/hour API rate limit. This manifest
answers "which files match?" in a single fetch; only the matching subset then needs
its actual content fetched.

Run this locally against your scraped library, then commit+push manifest.json to the
same GitHub repo that already holds the families/ folder (see this project's
recipe_export.py sibling scripts for the rest of the pipeline).

Usage:
    python mealime_generate_manifest.py --recipes-dir "C:\\path\\to\\free_recipes_json" --out-file "C:\\path\\to\\repo_clone\\manifest.json"
"""

import argparse
import json
from pathlib import Path

from library_paths import DEFAULT_LIBRARY_DIR as DEFAULT_RECIPES_DIR
NON_RECIPE_FILENAMES = {"alt_variants.json", "authenticated_index.json", "ingredient_index.json",
                        "ingredient_master.json", "manifest.json"}


def find_recipe_files(recipes_dir: Path):
    for path in recipes_dir.rglob("*.json"):
        if path.name in NON_RECIPE_FILENAMES:
            continue
        yield path


def build_manifest(recipes_dir: Path) -> dict:
    entries = {}
    families_with_thumbnail = set()

    # Sorted everywhere below: the output must be byte-identical wherever and whenever it is built
    # (filesystem order differs between Windows and Linux, and a set has no order), or every
    # regeneration -- e.g. the weekly one on the Raspberry Pi -- shows up as a spurious git change.
    for path in sorted(find_recipe_files(recipes_dir)):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(record, dict) or "id" not in record:
            continue

        entries[str(record["id"])] = {
            "recipe_id": record["recipe_id"],
            # Lets the site tell which variants belong to one dish ((recipe_id, group_label, name) --
            # see recipe_export.py's dish_key()) -- needed for Norish's "keep a dish whose only serving
            # size isn't selected" fallback, and for the --menu/--avoid dish-collapsing logic.
            "group_label": record.get("group_label"),
            # Mealime's own ordering of a family's dishes (lowest first = added earliest, ~always the
            # true base recipe) -- the ranking signal export-logic.js's dish-collapsing logic uses.
            "position": record.get("position"),
            "slug": record["slug"],
            "name": record["name"],
            "units": record.get("units"),
            "serving_count": record.get("serving_count"),
            "allowed_type_ids": record.get("allowed_type_ids") or [],
            # null (unknown) is kept as null, NOT turned into [] -- [] means "verified to violate
            # nothing". The site's restriction filter excludes unknown recipes when avoiding anything.
            "violated_restriction_ids": record.get("violated_restriction_ids"),
            # The file's real location, NOT rebuilt from id + slug: a record's slug can carry a
            # "-1"/"-2"... suffix its filename lacks, and a rebuilt path then 404s on the site.
            "path": path.relative_to(recipes_dir).as_posix(),
        }

    for family_dir in recipes_dir.glob("families/*"):
        if not family_dir.is_dir():
            continue
        for thumb in sorted(family_dir.glob("thumbnail.*")):
            families_with_thumbnail.add((int(family_dir.name), thumb.name))
            break

    return {
        "entries": {key: entries[key] for key in sorted(entries, key=int)},
        "family_thumbnails": {str(rid): name for rid, name in sorted(families_with_thumbnail)},
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build manifest.json (id -> family/units/servings/path) for the "
                    "GitHub Pages export tool."
    )
    parser.add_argument("--recipes-dir", type=Path, default=DEFAULT_RECIPES_DIR,
                        help=f"Source library folder (default: {DEFAULT_RECIPES_DIR})")
    parser.add_argument("--out-file", type=Path, required=True,
                        help="Where to write manifest.json (e.g. inside your local clone "
                             "of the GitHub repo, so it's ready to commit)")
    return parser.parse_args()


def main():
    args = parse_args()
    manifest = build_manifest(args.recipes_dir)
    args.out_file.parent.mkdir(parents=True, exist_ok=True)
    args.out_file.write_text(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(manifest['entries'])} recipe entries, "
          f"{len(manifest['family_thumbnails'])} family thumbnails")
    print(f"Saved: {args.out_file}")
    print(f"Size: {args.out_file.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
