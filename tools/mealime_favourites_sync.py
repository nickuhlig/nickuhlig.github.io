"""
Syncs your Mealime account's live "Favourites" list onto the already-scraped library,
stamping a favourite: true/false field onto every genuine Mealime-sourced recipe file
(everything EXCEPT source in {"albertsons_native", "albertsons_native_gap_fill"} -- see
ALBERTSONS_SOURCES below for why this is checked by exclusion, not by matching
"marketing_site"/"authenticated_backfill" directly).

WHERE THIS DATA COMES FROM: get_builder_data's response (the same endpoint
mealime_authenticated_sweep.py already uses) carries a top-level "favourites" array
that the public marketing-site scrape path never sees -- each entry looks like
{"id": <favourite-record-id>, "recipe_variant_id": <the recipe id you already know>,
"name": ..., "image_url": ...}. Discovered by patching window.fetch/XMLHttpRequest in a
logged-in browser session and inspecting the real response; there's no public
documentation for it. "recipe_variant_id" is the exact same numeric id this library
already keys every genuine Mealime record on, so matching is direct -- no name-matching
or heuristics needed, unlike kid_friendly's cookbook-slug inference for Albertsons data.

WHY EXACT VARIANT-ID MATCHING (no propagation to sibling serving-size/unit variants):
the API itself differentiates by recipe_variant_id, not by recipe family, so a sibling
id that ISN'T itself in the favourites list is recorded as favourite: false rather than
guessed true from a same-family match. If you favourited a recipe at one serving size,
only that exact variant id will show favourite: true here.

THIS IS LIVE, RE-RUNNABLE DATA, unlike the one-time kid_friendly backfill: your
favourites list changes as you favourite/unfavourite recipes in the app, so re-run this
script whenever you want the library's favourite fields refreshed. Every genuine Mealime
record gets a definite True/False each run (not left unset) since get_builder_data's
favourites list is authoritative and complete over your account's full catalog view --
there's no "unknown" case here the way there is for e.g. cuisine_tag_ids.

SPELLING: this project intentionally uses "favourite" (not "favorite") in its own
fields/docs per user preference. Norish's own archive schema uses the field name
"favorite" (American spelling, verified against its actual source -- see
recipe_export.py's build_norish_recipe()) -- that one spelling is NOT changed, since it
has to match Norish's own Zod schema byte-for-byte to import correctly.

Usage:
    python mealime_favourites_sync.py --recipes-dir "D:\\path\\to\\free-recipe-database"

Setup (one-time):
    pip install requests
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

API_ROOT = "https://api.mealime.com/api/v2/"
FAMILIES_DIRNAME = "families"
# "genuine Mealime" is identified by EXCLUSION, not by matching "marketing_site" /
# "authenticated_backfill" directly: every one of the 25,200 genuine records currently
# in the live library has source=None, because the now-deleted
# mealime_strip_source_fields.py nulled "source" project-wide for years before it was
# narrowed to stop doing that. Only Albertsons-sourced records (all added after that
# fix) still carry a real source value, so those are what's actually excluded here.
ALBERTSONS_SOURCES = {"albertsons_native", "albertsons_native_gap_fill"}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def make_session(auth_token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Authorization": f"Token token={auth_token}",
    })
    return session


def fetch_favourite_ids(session: requests.Session) -> set[int]:
    resp = session.post(API_ROOT + "get_builder_data", json={}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {f["recipe_variant_id"] for f in data.get("favourites") or []}


def sync_library(recipes_dir: Path, favourite_ids: set[int]) -> dict:
    stats = {"checked": 0, "marked_true": 0, "marked_false": 0, "unchanged": 0, "updated_files": 0}
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
            if not isinstance(record, dict) or record.get("source") in ALBERTSONS_SOURCES:
                continue

            stats["checked"] += 1
            is_favourite = record.get("id") in favourite_ids
            if is_favourite:
                stats["marked_true"] += 1
            else:
                stats["marked_false"] += 1

            if record.get("favourite") == is_favourite:
                stats["unchanged"] += 1
                continue

            record["favourite"] = is_favourite
            fpath.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            stats["updated_files"] += 1

    return stats


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync your Mealime account's live Favourites list onto the scraped library."
    )
    parser.add_argument("--recipes-dir", type=Path, required=True,
                         help="Library folder containing families/ (e.g. free-recipe-database)")
    parser.add_argument("--auth-token", default=os.environ.get("MEALIME_AUTH_TOKEN"),
                         help="Your my.mealime.com account's auth token (from its localStorage key "
                              "'mealimeAuthToken'). Defaults to the MEALIME_AUTH_TOKEN environment "
                              "variable so you don't have to pass a secret on the command line. Not "
                              "needed if --favourite-ids-json is given instead.")
    parser.add_argument("--favourite-ids-json", type=Path, default=None,
                         help="Alternative to --auth-token: a JSON file containing a plain list of "
                              "already-fetched recipe_variant_id integers (e.g. saved from a manual "
                              "get_builder_data call), for syncing without handling the auth token "
                              "in this script directly.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.favourite_ids_json:
        favourite_ids = set(json.loads(args.favourite_ids_json.read_text(encoding="utf-8")))
        print(f"Loaded {len(favourite_ids)} favourited recipe id(s) from {args.favourite_ids_json}.")
    elif args.auth_token:
        session = make_session(args.auth_token)
        favourite_ids = fetch_favourite_ids(session)
        print(f"Fetched {len(favourite_ids)} favourited recipe id(s) from your account.")
    else:
        print("Error: either --auth-token (or MEALIME_AUTH_TOKEN) or --favourite-ids-json is required.",
              file=sys.stderr)
        sys.exit(1)

    stats = sync_library(args.recipes_dir, favourite_ids)

    print(f"\nGenuine Mealime records checked: {stats['checked']}")
    print(f"  favourite=True:  {stats['marked_true']}")
    print(f"  favourite=False: {stats['marked_false']}")
    print(f"Files updated: {stats['updated_files']} (unchanged from previous sync: {stats['unchanged']})")


if __name__ == "__main__":
    main()
