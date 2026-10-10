"""
Runs the whole Albertsons refresh cycle in one command, instead of remembering to run
albertsons_full_sweep.py, albertsons_library_diff.py, fetch_albertsons_native_recipes.py,
mealime_ingredient_index.py, albertsons_restriction_backfill.py and
albertsons_cookbook_slugs_sync.py yourself in the right order, each one's output file fed
into the next by hand.

STAGES, in order (each is just a call into that script's own run()/sync() function -- this
file adds no new logic of its own beyond threading the file paths between them):

  1. sweep         albertsons_full_sweep.py            NETWORK -- needs HEADERS filled in
                    that file (a captured, logged-in Albertsons session). Writes
                    <sweep-dir>/albertsons_full_sweep.json. This is the slow stage (can be
                    ~40 minutes with --restriction-discovery/--pair-restriction-discovery),
                    so it's opt-in: pass --sweep to run it. Without --sweep, the existing
                    sweep file in --sweep-dir is reused as-is.
  2. diff           albertsons_library_diff.py          LOCAL. Compares the sweep against
                    --recipes-dir, writes <sweep-dir>/albertsons_new_recipes.json (and the
                    full albertsons_library_diff.json report alongside it).
  3. fetch          fetch_albertsons_native_recipes.py  NETWORK -- needs HEADERS filled in
                    that file too (the SAME captured session as the sweep works for both --
                    see that file's own docstring). Fetches every id the diff found new.
  4. ingredients    mealime_ingredient_index.py         LOCAL. Fills ingredient_id nulls from
                    ingredient_master.json; any ingredient name never seen before becomes a
                    new master entry with violates: null, for you to fill in afterwards.
  5. restrictions   albertsons_restriction_backfill.py  LOCAL. Fills violated_restriction_ids
                    for Albertsons recipes from the (now updated) master -- run AFTER
                    ingredients, since it needs the ids that stage just resolved.
  6. cookbooks      albertsons_cookbook_slugs_sync.py   LOCAL. Stamps cookbook_slugs and the
                    variety_tag_ids they imply, from the sweep file.

Skip any stage with --skip-<stage> (sweep is already opt-in via --sweep instead -- there's no
--skip-sweep). A skipped stage that a later one depends on (diff needs a sweep file to exist;
fetch needs a diff output to exist) is a hard error, not a silent no-op, so a typo'd flag
can't quietly export against stale or missing data.

--dry-run only affects the LOCAL stages that support one (ingredients/restrictions/
cookbooks) -- it does NOT preview or skip the network stages (sweep/fetch); use --skip-fetch
for that (there's no way to "preview" the sweep without actually running it).

Defaults match this project's usual layout:
    --recipes-dir   <repo>/projects/free-recipe-database  (found from where tools/ sits; see library_paths.py)
    --sweep-dir     <repo>/tools/albertsons_sweep_results

Usage:
    # everyday refresh: reuse the last sweep, diff + fetch + all three local cleanup steps
    python albertsons_pipeline.py

    # also redo the sweep first (needs HEADERS filled in albertsons_full_sweep.py)
    python albertsons_pipeline.py --sweep
    python albertsons_pipeline.py --sweep --restriction-discovery --pair-restriction-discovery

    # just redo the local processing steps (e.g. after editing ingredient_master.json by hand)
    python albertsons_pipeline.py --skip-diff --skip-fetch

    # preview what the local steps would change, without writing anything
    python albertsons_pipeline.py --skip-diff --skip-fetch --dry-run
"""

import argparse
from pathlib import Path

import albertsons_cookbook_slugs_sync
import albertsons_full_sweep
import albertsons_library_diff
import albertsons_restriction_backfill
import fetch_albertsons_native_recipes
import mealime_ingredient_index
from ingredient_master import master_path_for
from library_paths import DEFAULT_LIBRARY_DIR, DEFAULT_SWEEP_DIR as DEFAULT_SWEEP_DIR_PATH

DEFAULT_RECIPES_DIR = DEFAULT_LIBRARY_DIR
DEFAULT_SWEEP_DIR = DEFAULT_SWEEP_DIR_PATH
SWEEP_FILENAME = "albertsons_full_sweep.json"


def banner(step: str, title: str) -> None:
    print(f"\n{'=' * 12} {step}  {title}  {'=' * 12}")


def run(recipes_dir: Path, sweep_dir: Path, *,
        do_sweep: bool, skip_diff: bool, skip_fetch: bool, skip_ingredients: bool,
        skip_restrictions: bool, skip_cookbooks: bool,
        restriction_discovery: bool, pair_restriction_discovery: bool,
        overwrite_fetch: bool, dry_run: bool) -> None:
    sweep_json = sweep_dir / SWEEP_FILENAME

    if do_sweep:
        banner("1/6", "sweep")
        albertsons_full_sweep.run(sweep_dir, albertsons_full_sweep.SERVING_COUNTS, None, 0.4,
                                   restriction_discovery, pair_restriction_discovery)
    elif not sweep_json.exists():
        raise SystemExit(f"{sweep_json} doesn't exist yet -- pass --sweep to run one (needs HEADERS "
                          f"filled in albertsons_full_sweep.py), or point --sweep-dir at a folder that "
                          f"already has one.")
    else:
        print(f"\nReusing existing sweep (pass --sweep to refresh it): {sweep_json}")

    new_recipes_path = sweep_dir / "albertsons_new_recipes.json"
    if not skip_diff:
        banner("2/6", "diff")
        new_recipes_path = albertsons_library_diff.run(sweep_json, recipes_dir, sweep_dir)
    elif not skip_fetch and not new_recipes_path.exists():
        # only the fetch stage reads it, so a local-only run (diff and fetch both skipped) doesn't need it
        raise SystemExit(f"{new_recipes_path} doesn't exist yet -- drop --skip-diff at least once to "
                          f"create it.")
    else:
        print(f"\nSkipping diff; reusing existing output: {new_recipes_path}")

    if not skip_fetch:
        banner("3/6", "fetch")
        fetch_albertsons_native_recipes.run(new_recipes_path, recipes_dir, 0.5, None, overwrite=overwrite_fetch)
    else:
        print("\nSkipping fetch.")

    if not skip_ingredients:
        banner("4/6", "ingredients")
        mealime_ingredient_index.run(recipes_dir, dry_run=dry_run)
    else:
        print("\nSkipping ingredient index.")

    if not skip_restrictions:
        banner("5/6", "restrictions")
        albertsons_restriction_backfill.run(recipes_dir, master_path_for(recipes_dir),
                                             albertsons_restriction_backfill.DEFAULT_REPORT_PATH, dry_run)
    else:
        print("\nSkipping restriction backfill.")

    if not skip_cookbooks:
        banner("6/6", "cookbooks")
        albertsons_cookbook_slugs_sync.sync(recipes_dir, sweep_json, dry_run)
    else:
        print("\nSkipping cookbook/variety sync.")

    print("\nPipeline done.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the full Albertsons sweep -> diff -> fetch -> ingredients -> restrictions -> "
                    "cookbooks refresh cycle in one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--recipes-dir", type=Path, default=DEFAULT_RECIPES_DIR,
                        help=f"The library (default: {DEFAULT_RECIPES_DIR})")
    parser.add_argument("--sweep-dir", type=Path, default=DEFAULT_SWEEP_DIR,
                        help=f"Where the sweep/diff files live (default: {DEFAULT_SWEEP_DIR})")
    parser.add_argument("--sweep", action="store_true",
                        help="Re-run the sweep (slow, network) instead of reusing the existing one")
    parser.add_argument("--restriction-discovery", action="store_true",
                        help="Passed through to albertsons_full_sweep.py when --sweep is given")
    parser.add_argument("--pair-restriction-discovery", action="store_true",
                        help="Passed through to albertsons_full_sweep.py when --sweep is given")
    parser.add_argument("--skip-diff", action="store_true")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-ingredients", action="store_true")
    parser.add_argument("--skip-restrictions", action="store_true")
    parser.add_argument("--skip-cookbooks", action="store_true")
    parser.add_argument("--overwrite-fetch", action="store_true",
                        help="Passed through to fetch_albertsons_native_recipes.py's --overwrite")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the ingredients/restrictions/cookbooks stages only -- does not "
                             "affect sweep/fetch")
    return parser.parse_args()


def main():
    args = parse_args()
    run(args.recipes_dir, args.sweep_dir,
        do_sweep=args.sweep, skip_diff=args.skip_diff, skip_fetch=args.skip_fetch,
        skip_ingredients=args.skip_ingredients, skip_restrictions=args.skip_restrictions,
        skip_cookbooks=args.skip_cookbooks,
        restriction_discovery=args.restriction_discovery,
        pair_restriction_discovery=args.pair_restriction_discovery,
        overwrite_fetch=args.overwrite_fetch, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
