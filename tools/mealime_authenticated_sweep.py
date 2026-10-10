"""
One-off sweep of your Mealime account's authenticated builder-data API, producing a
combined id -> {recipe_id, published_recipe_uuid, allowed_type_ids, violated_restriction_ids}
index that's far more complete than a single get_builder_data call -- and, unlike the
public marketing site, reaches recipes it doesn't list at all.

WHY THIS WORKS: your account's Eating Preferences (Menu Type, Meal Size, Units, Allergies
and Restrictions, Ingredients You Dislike) all narrow what get_builder_data returns. This
script temporarily changes those settings via the same set_profile API the site itself
uses, calling get_builder_data after each change, and restores your original settings when
it's done (even if it's interrupted partway -- the restore runs in a finally block):

  1. For each of the 8 menu types (Classic, Vegetarian, Flexitarian, Pescatarian, Paleo,
     Vegan, Low Carb, Keto), with restrictions/dislikes cleared: call get_builder_data and
     record every id that appears. A recipe's allowed_type_ids = every menu type under
     which it showed up. ("Classic" turns out to be a strict superset of the other 7 --
     "no holds barred" really does mean everything qualifies -- so this reaches every
     recipe your account can see in one pass, while still correctly tagging which of the
     other 7 menus each one belongs to.)
  2. Starting from the Classic/no-restrictions baseline, toggle each of the 12 dietary
     restrictions on one at a time and re-call get_builder_data. Any recipe that
     disappears compared to the baseline violates that restriction.
  3. Each variant_meta entry is for ONE specific serving count -- a recipe family has a
     separate id for its 2/4/6-serving variants, and only whichever size your account is
     currently set to shows up in steps 1-2. So this also calls get_builder_data (still
     under Classic, no restrictions) once per serving size your original setting DIDN'T
     already cover, purely to discover those sibling ids. Their tags aren't re-derived
     from scratch (that would mean tripling the whole sweep); instead, since diet
     tags are a property of the DISH, not its serving scale, every newly-found id
     inherits allowed_type_ids/violated_restriction_ids from any already-tagged sibling
     that shares its recipe_id.

Validated against known data: for the Chicken Parmesan Meatballs recipe (id 24647), this
produces allowed_type_ids=[1,3,7] and violated_restriction_ids=[1,2,10,11] -- an exact
match (different order) with what the public marketing site's own data says.

IMPORTANT: this genuinely changes your account's Eating Preferences repeatedly while it
runs (~22 changes), restoring your original settings at the very end. If the script
crashes in a way that skips the finally block (e.g. you kill the process), your account
could be left on whatever menu/restriction/serving size it was mid-sweep -- just reopen
Settings > Eating Preferences and set it back manually if that happens.

Usage:
    python mealime_authenticated_sweep.py --out-dir "C:\\path\\to\\mealime_recipes_json"

Setup (one-time):
    pip install requests
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

API_ROOT = "https://api.mealime.com/api/v2/"
DEFAULT_OUT_DIR = Path.home() / "Downloads" / "mealime_recipes_json"
INDEX_FILENAME = "authenticated_index.json"

MENU_TYPE_IDS = [1, 2, 3, 4, 5, 6, 7, 8]
RESTRICTION_IDS = [1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14]
SERVING_COUNTS = [2, 4, 6]
CLASSIC_MENU_TYPE_ID = 1

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def make_session(auth_token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Authorization": f"Token token={auth_token}",
    })
    return session


MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2  # seconds -- doubles each attempt: 2s, 4s, 8s, 16s


def api_post(session: requests.Session, action: str, body: dict) -> dict:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(API_ROOT + action, json=body, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as exc:
            if resp.status_code < 500:
                raise  # client error (bad token, bad request, etc.) -- retrying won't help
            last_exc = exc
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc

        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            print(f"    [retry] {action} failed ({last_exc}); retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(wait)

    raise last_exc


def get_user(session: requests.Session) -> dict:
    return api_post(session, "get_user", {})


def set_profile(session: requests.Session, profile: dict) -> None:
    api_post(session, "set_profile", {"profile": profile})


def get_builder_data(session: requests.Session) -> dict:
    return api_post(session, "get_builder_data", {})


def run_sweep(session: requests.Session) -> tuple[dict, str | None]:
    """Returns (combined_index, error_message). combined_index is keyed by numeric id."""
    original_profile = get_user(session)["profile"]
    combined = {}
    error_msg = None

    try:
        for i, type_id in enumerate(MENU_TYPE_IDS, start=1):
            print(f"[menu {i}/{len(MENU_TYPE_IDS)}] switching to recipe_type_id={type_id}...")
            set_profile(session, {
                "recipe_type_id": type_id,
                "unit_family_id": original_profile["unit_family_id"],
                "recipe_restriction_ids": [],
                "dislike_ids": [],
                "serving_count": original_profile["serving_count"],
            })
            data = get_builder_data(session)
            for v in data["variant_meta"]:
                entry = combined.setdefault(v["id"], {
                    "recipe_id": v["recipe_id"],
                    "published_recipe_uuid": v["published_recipe_uuid"],
                    "allowed_type_ids": [],
                    "violated_restriction_ids": None,
                })
                entry["allowed_type_ids"].append(type_id)
            print(f"    variant_meta: {len(data['variant_meta'])} recipes (running total: {len(combined)})")

        classic_baseline_ids = [rid for rid, e in combined.items() if CLASSIC_MENU_TYPE_ID in e["allowed_type_ids"]]
        for rid in classic_baseline_ids:
            combined[rid]["violated_restriction_ids"] = []

        for i, restriction_id in enumerate(RESTRICTION_IDS, start=1):
            print(f"[restriction {i}/{len(RESTRICTION_IDS)}] checking restriction_id={restriction_id} under Classic...")
            set_profile(session, {
                "recipe_type_id": CLASSIC_MENU_TYPE_ID,
                "unit_family_id": original_profile["unit_family_id"],
                "recipe_restriction_ids": [restriction_id],
                "dislike_ids": [],
                "serving_count": original_profile["serving_count"],
            })
            data = get_builder_data(session)
            present_ids = {v["id"] for v in data["variant_meta"]}
            for rid in classic_baseline_ids:
                if rid not in present_ids:
                    combined[rid]["violated_restriction_ids"].append(restriction_id)

        other_serving_counts = [s for s in SERVING_COUNTS if s != original_profile["serving_count"]]
        for i, serving_count in enumerate(other_serving_counts, start=1):
            print(f"[serving {i}/{len(other_serving_counts)}] checking serving_count={serving_count} under Classic...")
            set_profile(session, {
                "recipe_type_id": CLASSIC_MENU_TYPE_ID,
                "unit_family_id": original_profile["unit_family_id"],
                "recipe_restriction_ids": [],
                "dislike_ids": [],
                "serving_count": serving_count,
            })
            data = get_builder_data(session)
            new_count = 0
            for v in data["variant_meta"]:
                if v["id"] not in combined:
                    combined[v["id"]] = {
                        "recipe_id": v["recipe_id"],
                        "published_recipe_uuid": v["published_recipe_uuid"],
                        "allowed_type_ids": [],
                        "violated_restriction_ids": None,
                    }
                    new_count += 1
            print(f"    variant_meta: {len(data['variant_meta'])} recipes ({new_count} new ids, running total: {len(combined)})")

        print("Propagating tags across same-recipe serving-size siblings...")
        by_recipe_id = {}
        for rid, entry in combined.items():
            by_recipe_id.setdefault(entry["recipe_id"], []).append(entry)
        propagated = 0
        for entries in by_recipe_id.values():
            tagged = next((e for e in entries if e["allowed_type_ids"] or e["violated_restriction_ids"]), None)
            if tagged is None:
                continue
            for e in entries:
                if not e["allowed_type_ids"] and e["violated_restriction_ids"] is None:
                    e["allowed_type_ids"] = list(tagged["allowed_type_ids"])
                    e["violated_restriction_ids"] = (
                        list(tagged["violated_restriction_ids"]) if tagged["violated_restriction_ids"] is not None else None
                    )
                    propagated += 1
        print(f"    tagged {propagated} additional serving-size sibling(s) from an already-tagged sibling")
    except requests.RequestException as exc:
        error_msg = str(exc)
    finally:
        print("Restoring your original Eating Preferences...")
        set_profile(session, original_profile)

    return combined, error_msg


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sweep your Mealime account's authenticated builder data to build a "
                    "combined id -> tags index (allowed_type_ids, violated_restriction_ids)."
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help=f"Where to write {INDEX_FILENAME} (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--auth-token", default=os.environ.get("MEALIME_AUTH_TOKEN"),
                         help="Your my.mealime.com account's auth token (from its localStorage key "
                              "'mealimeAuthToken'). Defaults to the MEALIME_AUTH_TOKEN environment "
                              "variable so you don't have to pass a secret on the command line.")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.auth_token:
        print("Error: --auth-token (or MEALIME_AUTH_TOKEN) is required.", file=sys.stderr)
        sys.exit(1)

    session = make_session(args.auth_token)
    combined, error_msg = run_sweep(session)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / INDEX_FILENAME
    out_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")

    tagged_count = sum(1 for e in combined.values() if e["violated_restriction_ids"] is not None)
    print("\nDone." if error_msg is None else f"\nStopped early due to an error: {error_msg}")
    print(f"Total recipes found: {len(combined)}  (with restriction tags: {tagged_count})")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
