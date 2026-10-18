"""
Step 1 of per-ingredient nutrition: find FDC (SR Legacy) candidates for master ingredients.

    python fdc_ingredient_match.py candidates --out <file.json> [--top 500] [--usage <file.json>]

Ranks master ingredients by how many NON-Mealime recipe lines use them (Mealime recipes keep
Mealime's own nutrition, so only Albertsons native, gap-fill and imported-site recipes count),
then runs one FDC search per ingredient and writes the top hits for review. Counting usage reads
every recipe file (slow on Google Drive), so --usage caches the counts: read if it exists,
written otherwise.

Query rules agreed with the user:
  - "X or Y" ingredients search for X (the first option).
  - Parenthetical text is dropped from the query ("green onions (scallions)" -> "green onions").
  - SR Legacy only: Foundation foods carry too few nutrients to meet the 66-field standard;
    an ingredient with no good SR Legacy match is flagged for the user, never auto-filled.

Picking the match is a reviewed, human step -- FDC's search ranking is poor ("salmon fillet"
ranks emu fillet first), so the top hit is never taken automatically.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import fdc_client
from ingredient_master import load_master, master_path_for
from library_paths import DEFAULT_LIBRARY_DIR

MEALIME_SOURCES = {None, "authenticated_backfill"}


def non_mealime_usage(library_dir: Path) -> Counter:
    """ingredient id -> number of ingredient lines in non-Mealime recipes."""
    usage = Counter()
    for path in (library_dir / "families").glob("*/*.json"):
        recipe = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(recipe, dict) or recipe.get("source") in MEALIME_SOURCES:
            continue
        for line in recipe.get("ingredients", []):
            if line.get("ingredient_id") is not None:
                usage[str(line["ingredient_id"])] += 1
    return usage


def search_query(name: str) -> str:
    query = re.sub(r"\([^)]*\)", " ", name)
    query = re.split(r"\s+or\s+", query, maxsplit=1)[0]
    # FDC parses the query as Lucene syntax: "/" and friends make it answer 400
    query = re.sub(r"[/\\:\"!^~*?\[\]{}+&|]", " ", query)
    return re.sub(r"\s+", " ", query).strip(" ,-")


def cmd_candidates(args) -> None:
    library_dir = Path(args.recipes_dir)
    master = load_master(master_path_for(library_dir))["ingredients"]

    usage_file = Path(args.usage) if args.usage else None
    if usage_file and usage_file.exists():
        usage = Counter(json.loads(usage_file.read_text(encoding="utf-8")))
    else:
        print("Counting non-Mealime ingredient usage (reads every recipe)...", file=sys.stderr)
        usage = non_mealime_usage(library_dir)
        if usage_file:
            usage_file.write_text(json.dumps(usage), encoding="utf-8")

    out_path = Path(args.out)
    results = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    ranked = [i for i, _ in usage.most_common() if i in master][:args.top]
    for n, ing_id in enumerate(ranked, 1):
        if ing_id in results:
            continue
        name = master[ing_id]["name"]
        query = search_query(name)
        results[ing_id] = {"name": name, "uses": usage[ing_id], "query": query,
                           "hits": fdc_client.search(query, data_types=("SR Legacy",), page_size=args.hits)}
        if n % 25 == 0:
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {n}/{len(ranked)}", file=sys.stderr)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(results)} ingredients with candidates -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recipes-dir", default=str(DEFAULT_LIBRARY_DIR))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("candidates", help="search FDC for the most-used ingredients")
    p.add_argument("--out", required=True)
    p.add_argument("--top", type=int, default=500)
    p.add_argument("--hits", type=int, default=8)
    p.add_argument("--usage")
    args = parser.parse_args()
    {"candidates": cmd_candidates}[args.cmd](args)


if __name__ == "__main__":
    main()
