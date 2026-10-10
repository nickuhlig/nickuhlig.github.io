"""
Harvest recipe ids from Albertsons' "Meal Plans & Recipes" feature
(www.albertsons.com/meal-plans-recipes) -- ID DISCOVERY ONLY, no full recipe detail.

BACKGROUND:
Albertsons' meal-planning feature is built on Mealime's recipe library (Albertsons
acquired Mealime in 2019): every recipe thumbnail is served from
cdn-uploads.mealime.com, and every recipe record Albertsons returns carries a
`legacyId` (Mealime's own numeric recipe-variant id) and `parentId` (Mealime's numeric
recipe-family id) -- the exact same ids mealime_json_scrape.py already knows how to
fetch full detail for. So the plan is two scripts, not one:
  1. THIS script harvests the Albertsons-side id -> legacyId/parentId mapping for
     recipes across Albertsons' category ("cookbook") listings, including ones outside
     Mealime's own public browse catalog.
  2. Feed the resulting legacy ids into mealime_json_scrape.py (--ids-file) to pull
     full ingredients/instructions/nutrition from Mealime's own public JSON API (with
     --auth-token backfill for any that 404 there, since Albertsons-exclusive recipes
     are exactly the kind of "unpromoted family" that path is meant to catch).

WHY THIS APPROACH, AND ITS LIMITS:
Albertsons exposes recipe listings two ways:
  a) A public, unauthenticated Next.js data route:
       GET /meal-plans-recipes/_next/data/<buildId>/cookbooks/<slug>.json
     Same kind of server-rendered data route mealime_json_scrape.py already uses
     against Mealime itself -- no login, no cookies, no API key required. This is what
     this script uses. Its limit: every category response is capped at ~20 recipes
     with no working pagination parameter found (page/offset/blockRef were all tried
     and ignored), so this will NOT surface a category's full contents.
  b) An authenticated client API, /abs/pub/dirm/menuservice/v2/cookbook?slug=<slug>,
     which returns a category's FULL contents (e.g. all 78 recipes in a "favorites"
     list, vs. 20 from route (a)) -- but it requires both your logged-in session
     cookie AND an Azure API Management subscription-key header that ships baked into
     Albertsons' minified JS bundle. Deliberately NOT implemented here: it would mean
     hardcoding a lifted internal credential into a script that replays it outside the
     browser entirely, which is a materially different (and worse) thing than reusing
     a public data route the way mealime_json_scrape.py already does. If you want that
     fuller haul, the more defensible route is capturing the responses your own
     logged-in browser already receives as you click through categories (which is what
     was done interactively to discover the shape of this data), not scripting around
     the key.

So: expect a solid multi-hundred-recipe batch across every homepage category, not
Albertsons' full catalog.

Usage:
    python new_site_json_scrape.py --out-dir <dir>
    python new_site_json_scrape.py --out-dir <dir> --slugs curries,bowls,dessert

Setup (one-time):
    pip install requests
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

HOME_URL = "https://www.albertsons.com/meal-plans-recipes"
DATA_URL_TEMPLATE = "https://www.albertsons.com/meal-plans-recipes/_next/data/{build_id}/cookbooks/{slug}.json"

# Personalized/uiComponent sections that don't appear as "cookbook" blocks in the
# homepage layout but map to a real /cookbooks/<slug> route anyway -- worth trying,
# but not guaranteed to resolve (hence they're attempted separately and tolerated if
# they 404).
EXTRA_CANDIDATE_SLUGS = ["favorites", "cook-it-again"]

DEFAULT_OUT_DIR = Path.home() / "Downloads" / "albertsons_recipe_ids"
IDS_JSON_FILENAME = "albertsons_recipe_ids.json"
LEGACY_IDS_FILENAME = "legacy_ids.txt"

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)

MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2  # seconds -- doubles each attempt: 2s, 4s, 8s, 16s

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def make_session(cookie: str | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if cookie:
        session.headers.update({"Cookie": cookie})
    return session


def request_with_retry(method, url, **kwargs):
    """Calls session.get with retry-with-backoff on timeouts, connection errors, and
    5xx server errors. Any other response (200, 404, etc.) is returned as-is on the
    first attempt without retrying."""
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


def discover_build_id_and_slugs(session: requests.Session) -> tuple[str, list[dict]]:
    """Fetches the meal-plans-recipes homepage and pulls the embedded __NEXT_DATA__
    blob (the same server-rendered JSON Next.js hydrates the page from) to get the
    current buildId plus every "cookbook" block's slug/name -- this is what the
    homepage actually shows, so it's a live, self-updating source of category slugs
    rather than a hardcoded list that will drift as Albertsons changes its layout."""
    resp = request_with_retry(session.get, HOME_URL, timeout=20)
    resp.raise_for_status()
    match = NEXT_DATA_RE.search(resp.text)
    if not match:
        raise RuntimeError("Could not find __NEXT_DATA__ on the meal-plans-recipes homepage "
                            "-- site markup may have changed.")
    data = json.loads(match.group(1))
    build_id = data["buildId"]
    layout = data["props"]["pageProps"]["layout"]
    cookbooks = [
        {"slug": block["slug"], "name": block.get("cookbook", {}).get("name", block["slug"])}
        for block in layout
        if block.get("blockType") == "cookbook"
    ]
    return build_id, cookbooks


def fetch_cookbook_thumbs(session: requests.Session, build_id: str, slug: str) -> list[dict] | None:
    """Returns the (up to ~20) recipe thumbs for one category via the public Next.js
    data route, or None if that slug doesn't resolve to a real cookbook page."""
    url = DATA_URL_TEMPLATE.format(build_id=build_id, slug=slug)
    resp = request_with_retry(session.get, url, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return [thumb for block in data["pageProps"]["blocks"] for thumb in block["thumbs"]]


def normalize_thumb(thumb: dict, slug: str) -> dict:
    """`thumb` is assumed to already be a Mealime-backed recipe (has a legacyId) --
    see the `legacyId not in thumb` filter in run(), which skips plain grocery-product
    thumbs (e.g. the "Prepared Meals"/ready-meals category mixes in frozen-dinner SKUs
    that carry a product id instead of a Mealime legacyId)."""
    return {
        "id": thumb["id"],
        "legacy_id": thumb["legacyId"],
        "parent_id": thumb.get("parentId"),
        "slug": thumb["slug"],
        "name": thumb["name"],
        "calories": thumb.get("calories"),
        "cooking_minutes": thumb.get("cookingMinutes"),
        "serving_count": thumb.get("servingCount"),
        "thumbnail_image_url": thumb.get("thumbnailImageUrl"),
        "cookbook_slugs": [slug],
    }


def run(out_dir: Path, extra_slugs: list[str], only_slugs: list[str] | None,
        delay: float, cookie: str | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    session = make_session(cookie)

    print("Discovering current buildId and homepage category slugs...")
    build_id, cookbooks = discover_build_id_and_slugs(session)
    print(f"buildId: {build_id}")

    if only_slugs:
        slugs_to_try = [{"slug": s, "name": s} for s in only_slugs]
    else:
        slugs_to_try = cookbooks + [{"slug": s, "name": s} for s in extra_slugs]

    print(f"Trying {len(slugs_to_try)} category slugs: {', '.join(c['slug'] for c in slugs_to_try)}")

    recipes: dict[str, dict] = {}
    for i, cookbook in enumerate(slugs_to_try, start=1):
        slug = cookbook["slug"]
        try:
            thumbs = fetch_cookbook_thumbs(session, build_id, slug)
        except requests.RequestException as exc:
            print(f"[{i}/{len(slugs_to_try)}] {slug} -> error ({exc})")
            continue

        if thumbs is None:
            print(f"[{i}/{len(slugs_to_try)}] {slug} -> not found (no such cookbook route)")
            continue

        new_count = 0
        skipped_products = 0
        for thumb in thumbs:
            if "legacyId" not in thumb or thumb["legacyId"] is None:
                skipped_products += 1  # e.g. ready-meals mixes in grocery-product SKUs
                continue
            normalized = normalize_thumb(thumb, slug)
            existing = recipes.get(normalized["id"])
            if existing is None:
                recipes[normalized["id"]] = normalized
                new_count += 1
            elif slug not in existing["cookbook_slugs"]:
                existing["cookbook_slugs"].append(slug)

        skipped_note = f", {skipped_products} skipped (non-recipe products)" if skipped_products else ""
        print(f"[{i}/{len(slugs_to_try)}] {slug} ({cookbook['name']}) -> "
              f"{len(thumbs)} recipes, {new_count} new{skipped_note} (total unique so far: {len(recipes)})")

        if i < len(slugs_to_try):
            time.sleep(delay)

    ids_path = out_dir / IDS_JSON_FILENAME
    result_list = sorted(recipes.values(), key=lambda r: r["legacy_id"])
    ids_path.write_text(json.dumps(result_list, indent=2, ensure_ascii=False), encoding="utf-8")

    legacy_ids_path = out_dir / LEGACY_IDS_FILENAME
    legacy_ids_path.write_text(
        "\n".join(str(r["legacy_id"]) for r in result_list) + "\n", encoding="utf-8"
    )

    print(f"\nDone. {len(result_list)} unique recipes found across {len(slugs_to_try)} categories.")
    print(f"Full records: {ids_path}")
    print(f"Legacy ids only (feed this into mealime_json_scrape.py --ids-file): {legacy_ids_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Harvest Albertsons meal-plans-recipes recipe ids (id discovery only -- "
                    "see module docstring for why full detail isn't fetched here)."
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help=f"Where to save the id list (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--slugs", default=None,
                         help="Comma-separated list of cookbook slugs to use INSTEAD of "
                              "auto-discovering them from the homepage (e.g. "
                              "'curries,bowls,dessert'). Useful for retrying a few slugs.")
    parser.add_argument("--extra-slugs", default=",".join(EXTRA_CANDIDATE_SLUGS),
                         help="Comma-separated list of extra candidate slugs to try alongside "
                              f"the auto-discovered ones (default: {','.join(EXTRA_CANDIDATE_SLUGS)}). "
                              "Ignored if --slugs is given.")
    parser.add_argument("--delay", type=float, default=0.5,
                         help="Seconds to wait between category requests (default: 0.5)")
    parser.add_argument("--cookie", default=None,
                         help="Optional raw Cookie header value from your logged-in browser "
                              "session (DevTools -> Network -> any albertsons.com request -> "
                              "Cookie request header), in case the public data route ever "
                              "starts requiring a session for personalized defaults. Not needed "
                              "in testing so far.")
    return parser.parse_args()


def main():
    args = parse_args()
    only_slugs = [s.strip() for s in args.slugs.split(",") if s.strip()] if args.slugs else None
    extra_slugs = [s.strip() for s in args.extra_slugs.split(",") if s.strip()] if args.extra_slugs else []

    if args.out_dir.exists() and not args.out_dir.is_dir():
        print(f"{args.out_dir} exists and is not a directory.", file=sys.stderr)
        sys.exit(1)

    run(args.out_dir, extra_slugs=extra_slugs, only_slugs=only_slugs,
        delay=args.delay, cookie=args.cookie)


if __name__ == "__main__":
    main()
