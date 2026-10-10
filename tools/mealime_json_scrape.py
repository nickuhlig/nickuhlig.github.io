"""
Scrape Mealime recipes from the www.mealime.com JSON data API (not the old PDF
print-page pipeline in mealime_scrape.py).

For each candidate recipe-variant id:
  1. GET https://r.mealime.com/<id> (no redirect-following) to resolve its slug
     from the Location header. A non-redirect response means the id doesn't exist.
  2. GET the Next.js data endpoint for that slug+id, which returns the recipe
     fully resolved: ingredients (name + quantity), instructions, nutrition,
     cookware -- plus the recipe's full sibling list (altVariants: every other
     serving-count/unit-system/diet-variant of the same dish), each carrying its
     own menu-type and dietary-restriction flags straight from Mealime itself.

This ID space is shared with the print-page domain used by mealime_scrape.py
(the same numeric id resolves on both), and is NOT limited to the ~3,291-recipe
public browse catalog -- pro-tier and unpromoted recipe families exist outside
that catalog but are still fully fetchable here, with no login required.

Every recipe's own JSON is saved inside its family's shared folder,
families/<recipe_id>/<id>_<slug>.json, alongside that family's alt_variants.json
and thumbnail.<ext> -- both of which are written ONCE per recipe_id (every
sibling variant carries an identical copy of that array and photo URL) rather
than duplicated per saved recipe.

OPTIONAL AUTHENTICATED BACKFILL (--auth-token):
The public www.mealime.com JSON API above 404s for some real ids -- notably
"dead sibling" variants (a diet/serving variant Mealime lists but never
actually published) and recipes too new for the public catalog to have caught
up to yet. Both are still fetchable if you have a my.mealime.com login:

  1. POST https://api.mealime.com/api/v2/get_builder_data (with your account's
     auth token, extracted from my.mealime.com's own localStorage) returns
     "variant_meta": full metadata -- including published_recipe_uuid -- for
     every recipe your account's meal planner considers usable. This set
     overlaps with, but is NEITHER a subset NOR superset of, the public
     catalog: it includes both entirely-unlisted older families and brand new
     recipes the public site hasn't caught up to, but it's bounded by some
     "feasible for meal planning" filter we haven't fully characterized, so it
     isn't exhaustive either.
  2. GET https://cdn-recipes.mealime.com/<published_recipe_uuid>.json is a
     PUBLIC, unauthenticated CDN that serves the fully-resolved recipe (same
     shape as publishedRecipe above) for any uuid you already know -- so once
     step 1 gives you a uuid, fetching the actual content needs no auth at all.

When --auth-token is given, get_builder_data is called once at startup and
used as a fallback whenever the normal path 404s for an id it happens to know
about. By default these backfilled records have allowed_type_ids/
violated_restriction_ids/alt_variants left null -- that data only exists in
the public site's response, not in a single get_builder_data call -- but a
few bonus fields (ratings, macros, calories, sodium, popularity) that the
public site never exposes at all are always included.

To fill in allowed_type_ids/violated_restriction_ids for backfilled records
too, first run mealime_authenticated_sweep.py against the same --out-dir --
it sweeps your account's Eating Preferences (menu type x dietary restriction)
to derive real tags for every recipe it can see, saving them to
authenticated_index.json. If that file is present, this script uses it
automatically (both for backfill tagging and, with --from-builder-data, as a
bigger id list than a single get_builder_data call would give).

Usage:
    python mealime_json_scrape.py --start 1 --end 100
    python mealime_json_scrape.py --start 1 --end 40000 --delay 0.5
    python mealime_json_scrape.py --auth-token "$MEALIME_AUTH_TOKEN" --start 1 --end 40000
    python mealime_json_scrape.py --auth-token "$MEALIME_AUTH_TOKEN" --from-builder-data
    python mealime_authenticated_sweep.py --out-dir <same --out-dir as above>  # run this first for full tags

Setup (one-time):
    pip install requests
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

RECIPES_PAGE_URL = "https://www.mealime.com/recipes"
REDIRECT_URL_TEMPLATE = "https://r.mealime.com/{}"
JSON_URL_TEMPLATE = "https://www.mealime.com/_next/data/{build_id}/recipes/{slug}/{id}.json?slug={slug}&variantId={id}"
BUILDER_DATA_URL = "https://api.mealime.com/api/v2/get_builder_data"
CDN_RECIPE_URL_TEMPLATE = "https://cdn-recipes.mealime.com/{}.json"

DEFAULT_OUT_DIR = Path.home() / "Downloads" / "mealime_recipes_json"
LOG_FILENAME = "scrape_log.csv"
FAMILIES_DIRNAME = "families"
ALT_VARIANTS_FILENAME = "alt_variants.json"
AUTH_INDEX_FILENAME = "authenticated_index.json"  # produced by mealime_authenticated_sweep.py

BUILD_ID_RE = re.compile(r'"buildId":"([^"]+)"')
LOCATION_SLUG_RE = re.compile(r"/recipes/([^/]+)/(\d+)")

MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2  # seconds -- doubles each attempt: 2s, 4s, 8s, 16s

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session

def request_with_retry(method, url, **kwargs):
    """Calls session.get/session.post (pass the bound method as `method`) with retry-
    with-backoff on timeouts, connection errors, and 5xx server errors. Any other
    response (200, 404, 403, etc.) is returned as-is on the first attempt without
    retrying -- callers that inspect specific status codes (fetch_recipe_json,
    fetch_cdn_recipe, resolve_slug) keep working exactly as before."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = method(url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
        else:
            if resp.status_code < 500:
                return resp
            last_exc = requests.exceptions.HTTPError(f"{resp.status_code} server error", response=resp)

        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            print(f"    [retry] {url} failed ({last_exc}); retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(wait)

    raise last_exc


def discover_build_id(session: requests.Session) -> str:
    resp = request_with_retry(session.get, RECIPES_PAGE_URL, timeout=20)
    resp.raise_for_status()
    match = BUILD_ID_RE.search(resp.text)
    if not match:
        raise RuntimeError("Could not find buildId on the /recipes page -- site markup may have changed.")
    return match.group(1)




def resolve_slug(session: requests.Session, recipe_id: int) -> str | None:
    resp = request_with_retry(session.get, REDIRECT_URL_TEMPLATE.format(recipe_id), allow_redirects=False, timeout=15)
    location = resp.headers.get("Location", "")
    match = LOCATION_SLUG_RE.search(location)
    return match.group(1) if match else None


def fetch_recipe_json(session: requests.Session, build_id: str, slug: str, recipe_id: int) -> dict | None:
    url = JSON_URL_TEMPLATE.format(build_id=build_id, slug=slug, id=recipe_id)
    resp = request_with_retry(session.get, url, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def fetch_builder_data(session: requests.Session, auth_token: str) -> dict:
    resp = request_with_retry(
        session.post,
        BUILDER_DATA_URL,
        headers={"Content-Type": "application/json", "Authorization": f"Token token={auth_token}"},
        json={},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def build_builder_index(builder_data: dict) -> dict:
    """Maps numeric recipe-variant id -> its variant_meta entry (which carries
    published_recipe_uuid, the key needed to backfill via the public CDN). This
    entry has no allowed_type_ids/violated_restriction_ids -- a single
    get_builder_data call can't derive those (see mealime_authenticated_sweep.py,
    which can and populates load_authenticated_index instead)."""
    return {entry["id"]: entry for entry in builder_data["variant_meta"]}


def load_authenticated_index(out_dir: Path) -> dict | None:
    """Loads the id -> {recipe_id, published_recipe_uuid, allowed_type_ids,
    violated_restriction_ids} index produced by mealime_authenticated_sweep.py,
    if it's been run against this out_dir. Returns None if it hasn't."""
    path = out_dir / AUTH_INDEX_FILENAME
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {int(k): v for k, v in raw.items()}


def fetch_cdn_recipe(session: requests.Session, published_recipe_uuid: str) -> dict | None:
    resp = request_with_retry(session.get, CDN_RECIPE_URL_TEMPLATE.format(published_recipe_uuid), timeout=20)
    if resp.status_code in (403, 404):
        return None
    resp.raise_for_status()
    return resp.json()


def load_family_alt_variant_entry(out_dir: Path, recipe_family_id: int, variant_id: int) -> dict | None:
    """If this family's alt_variants.json already exists on disk (written the first
    time any sibling was reached via the marketing site -- possibly earlier in this
    same run, if a live sibling has a lower id than a dead one), it carries every
    sibling's own individually-tagged entry straight from Mealime, including dead
    siblings that 404 on the direct path. That's better than anything a single
    get_builder_data snapshot or even the authenticated sweep can offer for this
    specific id, since it's keyed per-id rather than derived by elimination."""
    alt_path = family_dir(out_dir, recipe_family_id) / ALT_VARIANTS_FILENAME
    if not alt_path.exists():
        return None
    try:
        alt_variants = json.loads(alt_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return next((v for v in alt_variants if v["id"] == variant_id), None)


def build_backfill_record(pr: dict, meta: dict, alt_entry: dict | None = None) -> dict:
    """Same output shape as build_record, but sourced from the authenticated
    get_builder_data + public CDN path instead of the marketing site. Fields
    that only the marketing site's response carries (alt_variants/sibling data,
    canonical_variant_url, schema_metadata, group_label/position/unit_family_id)
    are left null/empty -- there's no equivalent data available on this path.
    In exchange this path exposes a few fields the marketing site never does:
    rating, macros, calories, sodium_mg, popularity, price_per_serving.

    allowed_type_ids/violated_restriction_ids come from alt_entry (this exact id's
    own entry in the family's already-saved alt_variants.json) when available --
    that's the most specific, direct source there is. Failing that, they come from
    meta if it's a load_authenticated_index() entry (mealime_authenticated_sweep.py's
    output); they're empty if meta is just a plain get_builder_data variant_meta
    entry, which doesn't carry that data at all."""
    ingredients = [{"ingredient_id": None, "name": li["ingredient_name"], "quantity": li["quantity"]}
                   for li in pr["line_items"]]

    if alt_entry is not None:
        allowed_type_ids = alt_entry.get("allowed_type_ids") or []
        raw_restr = alt_entry.get("violated_restriction_ids")
        violated_restriction_ids = [] if raw_restr is None else raw_restr
    else:
        allowed_type_ids = meta.get("allowed_type_ids") or []
        violated_restriction_ids = meta.get("violated_restriction_ids") or []

    return {
        "id": pr["id"],
        "recipe_id": pr["recipe_id"],
        "name": pr["name"],
        "slug": pr["slug"],
        "source_url": f"https://www.mealime.com/recipes/{pr['slug']}/{pr['id']}",
        "canonical_variant_url": None,
        "serving_count": pr["serving_count"],
        "cooking_minutes": pr["cooking_minutes"],
        "units": pr["units"],
        "group_label": None,
        "position": None,
        "unit_family_id": None,
        "is_pro": meta.get("is_pro"),
        "average_rating": meta.get("rating"),
        "rating_count": meta.get("rating_count"),
        "allowed_type_ids": allowed_type_ids,
        "violated_restriction_ids": violated_restriction_ids,
        "variety_tag_ids": meta.get("variety_tag_ids", []),
        "cuisine_tag_ids": [],
        "recipe_category_id": None,
        "cookwares": pr["cookwares"],
        "ingredients": ingredients,
        "instructions": pr["instructions"],
        "nutrition": pr["nutrition"],
        "schema_metadata": None,
        "sibling_variant_ids": [],
        "family_dir": f"{FAMILIES_DIRNAME}/{pr['recipe_id']}/",
        "source": "authenticated_backfill",
        "backfill_bonus_fields": {
            "macros": meta.get("macros"),
            "calories": meta.get("calories"),
            "sodium_mg": meta.get("sodium_mg"),
            "popularity": meta.get("popularity"),
            "price_per_serving": meta.get("price_per_serving"),
            "ruleset": meta.get("ruleset"),
            "first_published_at": meta.get("first_published_at"),
        },
    }


def family_dir(out_dir: Path, recipe_family_id: int) -> Path:
    return out_dir / FAMILIES_DIRNAME / str(recipe_family_id)


def save_family_data(session: requests.Session, out_dir: Path, recipe_family_id: int,
                      alt_variants: list | None, pr: dict) -> None:
    """Writes the shared alt_variants array and the recipe's thumbnail image once
    per recipe family, since every sibling variant references the identical data --
    skips the work entirely if another sibling already saved it this run (or a
    previous run). alt_variants is None on the authenticated-backfill path, which
    has no equivalent data -- the family folder just won't get that file."""
    fdir = family_dir(out_dir, recipe_family_id)
    fdir.mkdir(parents=True, exist_ok=True)

    alt_variants_path = fdir / ALT_VARIANTS_FILENAME
    if alt_variants is not None and not alt_variants_path.exists():
        alt_variants_path.write_text(json.dumps(alt_variants, indent=2, ensure_ascii=False), encoding="utf-8")

    for field, base_name in (("thumbnail_image_url", "thumbnail"),):
        image_url = pr.get(field)
        if not image_url:
            continue
        ext = Path(urlparse(image_url).path).suffix or ".jpg"
        image_path = fdir / f"{base_name}{ext}"
        if image_path.exists():
            continue
        try:
            resp = session.get(image_url, timeout=20)
            resp.raise_for_status()
            image_path.write_bytes(resp.content)
        except requests.RequestException:
            pass  # images are a nice-to-have; don't fail the whole recipe over one bad download


def build_record(data: dict, recipe_id: int, slug: str) -> dict:
    pr = data["pageProps"]["publishedRecipe"]
    alt_variants = data["pageProps"]["altVariants"]
    self_variant = next((v for v in alt_variants if v["id"] == recipe_id), {})

    ingredients = pr["line_items"]
    ingredient_ids = self_variant.get("ingredient_ids") or []
    if len(ingredient_ids) == len(ingredients):
        ingredients = [
            {"ingredient_id": ing_id, "name": li["ingredient_name"], "quantity": li["quantity"]}
            for ing_id, li in zip(ingredient_ids, pr["line_items"])
        ]

    return {
        "id": pr["id"],
        "recipe_id": pr["recipe_id"],
        "name": pr["name"],
        "slug": pr["slug"],
        "source_url": f"https://www.mealime.com/recipes/{slug}/{recipe_id}",
        "canonical_variant_url": data["pageProps"].get("canonicalVariantUrl"),
        "serving_count": pr["serving_count"],
        "cooking_minutes": pr["cooking_minutes"],
        "units": pr["units"],
        "group_label": self_variant.get("group_label"),
        "position": self_variant.get("position"),
        "unit_family_id": self_variant.get("unit_family_id"),
        "is_pro": self_variant.get("is_pro"),
        "average_rating": self_variant.get("average_rating"),
        "rating_count": self_variant.get("rating_count"),
        "allowed_type_ids": self_variant.get("allowed_type_ids", []),
        "violated_restriction_ids": self_variant.get("violated_restriction_ids", []),
        "variety_tag_ids": self_variant.get("variety_tag_ids", []),
        "cuisine_tag_ids": self_variant.get("cuisine_tag_ids", []),
        "recipe_category_id": self_variant.get("recipe_category_id"),
        "cookwares": pr["cookwares"],
        "ingredients": ingredients,
        "instructions": pr["instructions"],
        "nutrition": pr["nutrition"],
        "schema_metadata": data["pageProps"].get("schemaMetadata"),
        "sibling_variant_ids": [v["id"] for v in alt_variants],
        "family_dir": f"{FAMILIES_DIRNAME}/{pr['recipe_id']}/",
        "source": "marketing_site",
        "backfill_bonus_fields": None,
    }


def existing_json_for_id(out_dir: Path, recipe_id: int) -> Path | None:
    matches = list(out_dir.rglob(f"{recipe_id}_*.json"))
    return matches[0] if matches else None


def log_row(log_path: Path, recipe_id: int, status: str, title: str, detail: str) -> None:
    is_new = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "id", "status", "title", "detail"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), recipe_id, status, title, detail])


def save_record_file(out_dir: Path, record: dict) -> str:
    filename = f"{record['id']}_{record['slug']}.json"
    filepath = family_dir(out_dir, record["recipe_id"]) / filename
    filepath.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"{record['family_dir']}{filename}"


def backfill_one(session: requests.Session, recipe_id: int, out_dir: Path, log_path: Path,
                  builder_index: dict) -> str | None:
    """Attempts the authenticated-builder-data + public-CDN path for an id the
    normal marketing-site path couldn't reach. Returns 'saved_backfill'/'not_found'/
    'error', or None if this id isn't in builder_index at all (meaning: not
    backfillable, caller should report the original not_found instead)."""
    meta = builder_index.get(recipe_id)
    if meta is None:
        return None

    uuid = meta.get("published_recipe_uuid")
    try:
        pr = fetch_cdn_recipe(session, uuid) if uuid else None
    except requests.RequestException as exc:
        log_row(log_path, recipe_id, "error", "", f"backfill CDN fetch failed: {exc}")
        return "error"

    if pr is None:
        log_row(log_path, recipe_id, "not_found", "", f"in builder data but CDN 404/403'd for uuid {uuid}")
        return "not_found"

    try:
        alt_entry = load_family_alt_variant_entry(out_dir, pr["recipe_id"], recipe_id)
        record = build_backfill_record(pr, meta, alt_entry)
    except (KeyError, TypeError) as exc:
        log_row(log_path, recipe_id, "error", "", f"unexpected CDN json shape: {exc}")
        return "error"

    save_family_data(session, out_dir, record["recipe_id"], None, pr)
    detail = save_record_file(out_dir, record)
    log_row(log_path, recipe_id, "saved_backfill", record["name"], detail)
    return "saved_backfill"


def scrape_one(session: requests.Session, build_id: str, recipe_id: int, out_dir: Path, log_path: Path,
                builder_index: dict | None = None) -> str:
    """Returns one of: 'saved', 'duplicate', 'not_found', 'error'."""
    if existing_json_for_id(out_dir, recipe_id):
        return "duplicate"

    try:
        slug = resolve_slug(session, recipe_id)
    except requests.RequestException as exc:
        log_row(log_path, recipe_id, "error", "", f"redirect lookup failed: {exc}")
        return "error"

    if slug is None:
        if builder_index:
            backfilled = backfill_one(session, recipe_id, out_dir, log_path, builder_index)
            if backfilled is not None:
                return backfilled
        log_row(log_path, recipe_id, "not_found", "", "no redirect (id does not exist)")
        return "not_found"

    try:
        data = fetch_recipe_json(session, build_id, slug, recipe_id)
    except requests.RequestException as exc:
        log_row(log_path, recipe_id, "error", "", f"json fetch failed: {exc}")
        return "error"

    if data is None:
        if builder_index:
            backfilled = backfill_one(session, recipe_id, out_dir, log_path, builder_index)
            if backfilled is not None:
                return backfilled
        log_row(log_path, recipe_id, "not_found", "", f"slug resolved ({slug}) but json 404'd")
        return "not_found"

    try:
        record = build_record(data, recipe_id, slug)
    except (KeyError, TypeError) as exc:
        log_row(log_path, recipe_id, "error", "", f"unexpected json shape: {exc}")
        return "error"

    save_family_data(session, out_dir, record["recipe_id"], data["pageProps"]["altVariants"],
                      data["pageProps"]["publishedRecipe"])
    detail = save_record_file(out_dir, record)

    log_row(log_path, recipe_id, "saved", record["name"], detail)
    return "saved"


def run(ids, out_dir: Path, delay: float, max_consecutive_misses: int, auth_token: str | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / LOG_FILENAME
    session = make_session()

    print("Discovering current buildId...")
    build_id = discover_build_id(session)
    print(f"buildId: {build_id}")

    builder_index = load_authenticated_index(out_dir)
    if builder_index is not None:
        print(f"Loaded authenticated sweep index: {len(builder_index)} recipes available for backfill, "
              f"with allowed_type_ids/violated_restriction_ids filled in "
              f"(from mealime_authenticated_sweep.py)")
    elif auth_token:
        print("Fetching authenticated builder data (for backfilling ids the public site can't reach)...")
        builder_data = fetch_builder_data(session, auth_token)
        builder_index = build_builder_index(builder_data)
        print(f"builder data: {len(builder_index)} recipes available for backfill "
              f"(no allowed_type_ids/violated_restriction_ids -- run mealime_authenticated_sweep.py for those)")

    counts = {"saved": 0, "saved_backfill": 0, "duplicate": 0, "not_found": 0, "error": 0}
    total = len(ids)
    consecutive_misses = 0

    try:
        for i, recipe_id in enumerate(ids, start=1):
            status = scrape_one(session, build_id, recipe_id, out_dir, log_path, builder_index)
            counts[status] += 1
            print(
                f"[{i}/{total}] id={recipe_id} -> {status}  "
                f"(saved={counts['saved']} saved_backfill={counts['saved_backfill']} "
                f"duplicate={counts['duplicate']} not_found={counts['not_found']} error={counts['error']})"
            )

            if status in ("not_found", "error"):
                consecutive_misses += 1
                if consecutive_misses >= max_consecutive_misses:
                    print(
                        f"\nStopping early: {consecutive_misses} consecutive not_found/error "
                        f"results. This usually means you've run past the end of the valid id "
                        f"range, or requests are being blocked. Re-run with --start {recipe_id + 1} "
                        f"to continue past this point once you've confirmed why."
                    )
                    break
            else:
                consecutive_misses = 0

            if status not in ("duplicate",):
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\nInterrupted by user. Progress is saved in the log; re-run the same "
              "range later and already-downloaded recipes will be skipped.")

    print("\nDone.")
    print(f"Saved: {counts['saved']}  Saved via backfill: {counts['saved_backfill']}  "
          f"Duplicate (already had): {counts['duplicate']}  "
          f"Not found: {counts['not_found']}  Errors: {counts['error']}")
    print(f"Log: {log_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape Mealime recipes via the site's JSON data API.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", type=int, help="First recipe id (inclusive). Use with --end.")
    group.add_argument("--ids-file", type=Path, help="Text file with one recipe id per line.")
    group.add_argument("--from-builder-data", action="store_true",
                        help="Scrape every id in your account's authenticated builder data instead of "
                             "a numeric range -- requires --auth-token (or MEALIME_AUTH_TOKEN).")
    parser.add_argument("--end", type=int, help="Last recipe id (inclusive). Required with --start.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help=f"Where to save recipe JSON files (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--delay", type=float, default=0.5,
                         help="Seconds to wait between requests (default: 0.5)")
    parser.add_argument("--max-consecutive-misses", type=int, default=300,
                         help="Auto-stop after this many consecutive not_found/error results in "
                              "a row, as a safety net against runaway ranges (default: 300)")
    parser.add_argument("--auth-token", default=os.environ.get("MEALIME_AUTH_TOKEN"),
                         help="Your my.mealime.com account's auth token (from its localStorage key "
                              "'mealimeAuthToken'), used to backfill ids the public site 404s on. "
                              "Optional -- omit to scrape the public site only. Defaults to the "
                              "MEALIME_AUTH_TOKEN environment variable so you don't have to pass a "
                              "secret on the command line.")
    args = parser.parse_args()

    if args.start is not None and args.end is None:
        parser.error("--end is required when using --start")
    if args.from_builder_data and not args.auth_token:
        parser.error("--from-builder-data requires --auth-token (or MEALIME_AUTH_TOKEN)")

    return args


def main():
    args = parse_args()

    if args.from_builder_data:
        auth_index = load_authenticated_index(args.out_dir)
        if auth_index is not None:
            ids = sorted(auth_index.keys())
        else:
            session = make_session()
            builder_data = fetch_builder_data(session, args.auth_token)
            ids = sorted(build_builder_index(builder_data).keys())
    elif args.ids_file:
        ids = [int(line.strip()) for line in args.ids_file.read_text().splitlines() if line.strip()]
    else:
        if args.start < 1 or args.end < args.start:
            print("Invalid range.", file=sys.stderr)
            sys.exit(1)
        ids = list(range(args.start, args.end + 1))

    run(ids, out_dir=args.out_dir, delay=args.delay, max_consecutive_misses=args.max_consecutive_misses,
        auth_token=args.auth_token)


if __name__ == "__main__":
    main()
