"""
Authenticated sweep of Albertsons' meal-plans-recipes catalog -- toggles serving size
(the axis that actually changes WHICH recipes a category shows) across every category,
using headers YOU capture from your own logged-in browser session.

This is intentionally NOT something the assistant runs against the live API itself --
capture the headers yourself (same as fetch_albertsons_native_recipes.py) with
set_headers.py, which writes the git-ignored albertsons_headers.json (see albertsons_auth.py),
and run this file locally. A 401/403 answer stops the sweep (AuthExpired) rather than
skipping request after request, and the sweep file is only replaced atomically and only if
the new run isn't suspiciously smaller than the old one (MIN_SWEEP_FRACTION, --allow-shrink).

WHY SERVINGS ONLY, NOT DIET/RESTRICTIONS/DISLIKES, FOR DISCOVERY:
Confirmed by hand: changing your account's serving-size preference (POST
.../v1/profile with a different servingCount) makes a category page show a
noticeably different set of recipes -- e.g. "Curries" at 2 servings included 8 dishes
that never appeared in our earlier default-preference (4 servings) harvest at all.
This is genuine and verified working headless -- it's what produced the
1,495-recipe / 395-new-family result from the first full run of this script.

Diet type does NOT do the same thing, despite first appearances. The live page really
does show diet-appropriate substitutes (e.g. "Steaks" under a Vegan account preference
shows tofu/tempeh dishes instead), but that's a client-side rendering behavior, not
something the v2/cookbook API itself does: calling v2/cookbook?slug=steaks directly --
even from a real, cookie-equipped, actively-Vegan browser session -- returns the exact
same unfiltered ~100-119 recipes (first one being an actual ribeye steak) regardless of
diet. So there is no "add the right header" fix here; toggling diet_id server-side via
v1/profile does nothing to what this endpoint returns, confirmed both without and with
a full session Cookie header. This isn't implemented as a discovery axis at all anymore
for that reason.

Instead, since each full recipe record carries its own "diets" field (e.g.
["classic","flexitarian","pescatarian","vegetarian"]) as an intrinsic property of the
recipe, diet tagging is done in fetch_albertsons_native_recipes.py once you fetch full
detail for a recipe anyway -- see diets_to_allowed_type_ids() there. No sweep needed.

Restrictions and dislikes are exclusion filters against the TRUE pool (turning one on
can only remove recipes from that pool, never add ones that weren't already reachable
under some serving count). But confirmed by hand, restriction toggling is still a real
extra discovery axis in practice: the SSR page route (/meal-plans-recipes/cookbooks/
<slug>, and the public data route behind it -- see fetch_cookbook_via_data_route()
below) doesn't show a fixed "first N of the master list" -- which specific items fill
that batch gets recomputed against whatever the current restriction leaves. Confirmed
on "steaks": excluding Gluten made 2 previously-unseen dishes appear that weren't in
the no-restrictions baseline at all (their spots were taken before by now-excluded
gluten dishes). So sweeping single restrictions through the SSR data route -- NOT
v2/cookbook, since that endpoint ignores diet server-side and is suspected to ignore
restrictions too, untested further given the SSR route already works -- is a legitimate
way to surface additional recipes. See run_restriction_discovery()/
--restriction-discovery below.

RESTRICTION TAGGING (violated_restriction_ids), same pass, no extra requests: same
technique mealime_authenticated_sweep.py uses against Mealime's own authenticated API --
record the no-restrictions baseline id set (per serving count x category, since that's
also run_restriction_discovery()'s first pass anyway), then for each subsequent
restriction pass, any baseline id that's now MISSING violates that restriction. This
replaces the old --tag-restrictions flag/run_restriction_tagging(), which used
v2/cookbook -- the same endpoint confirmed to ignore diet server-side, so its restriction
tags were never trustworthy to begin with (it self-checked and warned about this, but
had no better endpoint to fall back to at the time). Recipes found ONLY via
restriction-discovery (never seen in a baseline pass) get violated_restriction_ids left
as None -- we only know what made them newly visible, not what they'd violate, since we
never observed their baseline appearance to diff against.

KNOWN LIMITATION, confirmed permanent: v2/cookbook caps out at 100 thumbs per response
even when "total" is higher (confirmed: "steaks" has total=119, only 100 returned).
This isn't a missing pagination parameter -- watched the site's own "Load More" button
in DevTools and it fires NO new network request at all when clicked. It's purely
client-side pagination over the single batch already fetched on page load, revealing a
few more already-downloaded items each click. So the extra items past 100 aren't
reachable through Albertsons' own website either, by any means -- there's nothing to
discover here, this is a real ceiling on the endpoint itself.

PAIR-RESTRICTION DISCOVERY (--pair-restriction-discovery, a second, optional tier on
top of --restriction-discovery): sweeps every PAIR of the 12 restrictions together
(C(12,2) = 66 combos) instead of just one at a time. Rationale: excluding two
restrictions at once shrinks the competing pool further than either alone, so it can
push even lower-ranked recipes into the visible batch that no single restriction's
exclusion ever freed a slot for. Going further -- all 4096 possible subsets of the 12
restrictions -- was considered and rejected: each additional order of combination is
chasing an ever-shrinking remainder from the same fixed-size, ~100-120-item-per-category
pool, so returns diminish fast while the pass count explodes combinatorially (4095
combos x 3 servings x 21 categories =~258,000 requests, an estimated 30-hour run,
against pairs' ~4,158 requests) -- a poor trade given this project has already hit
bot-protection timeouts and 5xx retries at far lower request volumes.

Pairs ALSO now attribute real violations, at zero extra request cost, for recipes never
seen in a true baseline pass (previously the majority of Albertsons-native recipes --
confirmed: roughly half of a real sweep's recipes). The key insight: going straight from
the TRUE baseline to a PAIR is ambiguous (which of the two excluded restrictions caused
a disappearance?), which is why the original version of this file never attempted it.
But going from a SINGLE-restriction pass i to a PAIR (i, j) that adds exactly one more
excluded restriction j is NOT ambiguous -- i's exclusion is held constant across both
observations, so a recipe present under i alone but missing once j joins it can only be
attributed to j. Since the full C(12,2)=66 pairs already cover every (i, j) combination,
any recipe found under single-restriction pass i gets a definitive verdict (violates /
doesn't violate) for the OTHER 11 restrictions too, by diffing pass i against each of
the 11 pairs that include i -- full 12-restriction coverage, the same completeness a true
baseline id gets, just anchored on i instead of "no restrictions" as the reference point.
See finalize_restriction_tags()'s docstring for the completeness check this enables.

Usage:
    python albertsons_full_sweep.py --out-dir albertsons_sweep_results
    python albertsons_full_sweep.py --out-dir albertsons_sweep_results --restriction-discovery
    python albertsons_full_sweep.py --out-dir albertsons_sweep_results --restriction-discovery --pair-restriction-discovery
    python albertsons_full_sweep.py --out-dir albertsons_sweep_results --servings 2,4
"""

import argparse
import json
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import requests

import new_site_json_scrape as discover
from albertsons_auth import check_response, load_headers
from mealime_id_reference import RESTRICTIONS

PROFILE_URL = "https://www.albertsons.com/abs/pub/dirm/menuservice/v1/profile"
COOKBOOK_URL_TEMPLATE = "https://www.albertsons.com/abs/pub/dirm/menuservice/v2/cookbook?slug={slug}"
# Same underlying data as the SSR page's embedded __NEXT_DATA__, just as plain JSON --
# this is the route new_site_json_scrape.py already uses unauthenticated. Unlike
# v2/cookbook, this one genuinely respects the account's current diet/restrictions
# (confirmed by hand) since it's what actually renders the page you see.
DATA_ROUTE_TEMPLATE = "https://www.albertsons.com/meal-plans-recipes/_next/data/{build_id}/cookbooks/{slug}.json"

RESTRICTION_IDS = list(RESTRICTIONS.keys())  # [1,2,3,4,5,6,9,10,11,12,13,14]
SERVING_COUNTS = [2, 4, 6]
CLASSIC_DIET_ID = 1

# The captured session headers come from the git-ignored albertsons_headers.json (create or
# refresh it with set_headers.py) -- see albertsons_auth.py. Never paste real values in here.
HEADERS = load_headers(("Authorization", "Ocp-Apim-Subscription-Key", "Cookie"))

MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2
# A full sweep finding fewer than this fraction of the previous file's recipes is treated as a bad
# run and not saved over it. Needs like-for-like runs: without the restriction passes a sweep finds
# only ~1/3 of the catalog (the rest is only reachable through them), so a plain sweep over a
# restriction-discovery file trips this on purpose.
MIN_SWEEP_FRACTION = 0.9


def request_with_retry(method, url, **kwargs):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = method(url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
        else:
            if resp.status_code < 500:
                check_response(resp, url)  # 401/403 -> AuthExpired: stops the whole sweep, not just this request
                return resp
            last_exc = requests.exceptions.HTTPError(f"{resp.status_code} server error", response=resp)

        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            print(f"    [retry] {url} failed ({last_exc}); retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(wait)
    raise last_exc


def set_profile(session: requests.Session, diet_id: int, serving_count: int,
                 restriction_ids: list[int] | None = None, dislike_ids: list[int] | None = None) -> None:
    body = {
        "dislikeIds": dislike_ids or [],
        "recipeRestrictionIds": restriction_ids or [],
        "recipeTypeId": diet_id,
        "servingCount": serving_count,
    }
    resp = request_with_retry(session.post, PROFILE_URL, json=body, timeout=20)
    resp.raise_for_status()


def fetch_cookbook_full(session: requests.Session, slug: str) -> list[dict] | None:
    """Note: capped at 100 thumbs per response even when "total" is higher -- see
    module docstring's KNOWN LIMITATION section. Returns whatever the cap allows."""
    resp = request_with_retry(session.get, COOKBOOK_URL_TEMPLATE.format(slug=slug), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("thumbs", [])


def discover_build_id_and_slugs() -> tuple[str, list[str]]:
    """Neither the buildId nor category slugs need auth to discover -- same public
    homepage route new_site_json_scrape.py uses."""
    session = discover.make_session()
    build_id, cookbooks = discover.discover_build_id_and_slugs(session)
    return build_id, [c["slug"] for c in cookbooks]


def fetch_cookbook_via_data_route(session: requests.Session, build_id: str, slug: str) -> list[dict] | None:
    """Unlike fetch_cookbook_full() (v2/cookbook), this route respects the account's
    current diet/restrictions -- see module docstring. No 100-item cap observed here
    either, though categories are usually much smaller through this route (it's what
    the real page renders, not a raw catalog dump)."""
    resp = request_with_retry(session.get, DATA_ROUTE_TEMPLATE.format(build_id=build_id, slug=slug), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    blocks = resp.json().get("pageProps", {}).get("blocks", [])
    return [thumb for block in blocks for thumb in block.get("thumbs", [])]


def normalize_thumb(thumb: dict, slug: str) -> dict:
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
    }


def run_discovery(session: requests.Session, slugs: list[str], serving_counts: list[int],
                   delay: float) -> dict[str, dict]:
    combined: dict[str, dict] = {}

    for combo_i, serving_count in enumerate(serving_counts, start=1):
        print(f"\n[combo {combo_i}/{len(serving_counts)}] servings={serving_count}")
        set_profile(session, CLASSIC_DIET_ID, serving_count, [], [])

        for slug in slugs:
            try:
                thumbs = fetch_cookbook_full(session, slug)
            except requests.exceptions.RequestException as exc:
                print(f"    [skip] {slug}: giving up after retries ({exc})")
                continue
            if thumbs is None:
                print(f"    {slug}: not found")
                continue

            new_count = 0
            for thumb in thumbs:
                if "legacyId" not in thumb or thumb["legacyId"] is None:
                    continue  # non-Mealime product (e.g. ready-meals SKU)
                record = combined.get(thumb["id"])
                if record is None:
                    record = normalize_thumb(thumb, slug)
                    record["cookbook_slugs"] = set()
                    combined[thumb["id"]] = record
                    new_count += 1
                record["cookbook_slugs"].add(slug)

            print(f"    {slug}: {len(thumbs)} recipes, {new_count} new (total unique so far: {len(combined)})")
            time.sleep(delay)

    return combined


def run_restriction_discovery(session: requests.Session, slugs: list[str], build_id: str,
                               combined: dict[str, dict], serving_counts: list[int], delay: float,
                               violated: dict[str, set], confirmed_safe: dict[str, set],
                               single_pass_ids: dict[tuple[int, str, int], set[str]]) -> None:
    """Mutates `combined`, `violated`, `confirmed_safe`, and `single_pass_ids` in place
    (the latter three are shared with run_pair_restriction_discovery() and finalized
    together afterward by finalize_restriction_tags() -- see run()). single_pass_ids
    records this pass's own observed present_ids, keyed by (restriction_id, slug,
    serving_count) -- run_pair_restriction_discovery() reads it back as the reference
    point for attributing pair-pass disappearances to a specific restriction (see its
    own docstring). Does three things in the same pass, at no extra request cost:

    1. DISCOVERY (as before): adds any recipe found via the SSR data route
       (fetch_cookbook_via_data_route) under a no-restrictions baseline plus each of
       the 12 restrictions individually -- crossed with every serving count too
       (hardwired, not an option: a restriction can reveal different substitute dishes
       at 2 servings than at 4 or 6, same as the main discovery sweep found for serving
       count alone) -- that wasn't already found by run_discovery's v2/cookbook sweep.

    2. TAGGING (violated_restriction_ids): ONLY for ids seen in a no-restrictions
       baseline pass (per serving_count x slug, the first pass below) -- same technique
       mealime_authenticated_sweep.py uses: any baseline id now missing under a given
       restriction pass violates that restriction. Since diet/restriction status is a
       property of the DISH, not serving size (confirmed elsewhere in this project --
       allowed_type_ids is identical across every real sibling), this accumulates
       across ALL serving_counts for the same id, not just one.

       `confirmed_safe` also records, for ANY id found present while a restriction is
       active (baseline-seen or not), that its non-appearance-if-violating is a real,
       structural guarantee it does NOT violate that restriction: restrictions exclude
       from the TRUE pool before a batch is ever selected, so a recipe that violated the
       active restriction could not have appeared in that pass at all. This is real
       partial information, but it does NOT get written to violated_restriction_ids as
       [] here -- see finalize_restriction_tags()'s docstring for why conflating
       "confirmed safe from the 1-2 restrictions actually checked" with "checked
       against all 12, violates none" was a real, confirmed bug.

    3. RETENTION (single_pass_ids): every restriction pass's own present_ids is kept,
       not just baseline's -- this is what lets run_pair_restriction_discovery() later
       attribute a pair-pass disappearance to a specific restriction (see its docstring
       and the module docstring's PAIR-RESTRICTION DISCOVERY section) instead of only
       ever recording confirmed_safe evidence.

    Ids never seen in a baseline pass, and never resolvable via run_pair_restriction_
    discovery()'s single-vs-pair diff either, keep violated_restriction_ids as None --
    genuinely unknown, not fabricated as either "violates nothing" or "confirmed safe
    but incomplete."

    Restores Classic/no-restrictions/4-servings before returning. See module docstring
    for why this route (not v2/cookbook) is used for both, and why restriction toggling
    can surface recipes despite being an exclusion filter against the true pool."""
    passes = [(None, "no restrictions (baseline)")] + [(rid, RESTRICTIONS[rid]) for rid in RESTRICTION_IDS]
    total = len(passes) * len(serving_counts)
    combo_i = 0

    for serving_count in serving_counts:
        baseline_ids_by_slug: dict[str, set[str]] = {}

        for restriction_id, label in passes:
            combo_i += 1
            print(f"\n[restriction-discovery {combo_i}/{total}] {label} (servings={serving_count})")
            set_profile(session, CLASSIC_DIET_ID, serving_count,
                        [restriction_id] if restriction_id is not None else [], [])

            for slug in slugs:
                try:
                    thumbs = fetch_cookbook_via_data_route(session, build_id, slug)
                except requests.exceptions.RequestException as exc:
                    print(f"    [skip] {slug}: giving up after retries ({exc})")
                    continue
                if thumbs is None:
                    continue

                present_ids = set()
                new_count = 0
                for thumb in thumbs:
                    if "legacyId" not in thumb or thumb["legacyId"] is None:
                        continue
                    present_ids.add(thumb["id"])
                    if thumb["id"] not in combined:
                        record = normalize_thumb(thumb, slug)
                        record["cookbook_slugs"] = {slug}
                        record["found_via_restriction"] = f"{label} @ {serving_count} servings"
                        combined[thumb["id"]] = record
                        new_count += 1

                if restriction_id is None:
                    baseline_ids_by_slug[slug] = present_ids
                else:
                    single_pass_ids[(restriction_id, slug, serving_count)] = present_ids
                    for present_id in present_ids:
                        confirmed_safe[present_id].add(restriction_id)

                    missing = baseline_ids_by_slug.get(slug, set()) - present_ids
                    for missing_id in missing:
                        violated[missing_id].add(restriction_id)

                if new_count:
                    print(f"    {slug}: {new_count} new (total unique so far: {len(combined)})")
                time.sleep(delay)

    set_profile(session, CLASSIC_DIET_ID, 4, [], [])  # restore before returning


def run_pair_restriction_discovery(session: requests.Session, slugs: list[str], build_id: str,
                                    combined: dict[str, dict], serving_counts: list[int], delay: float,
                                    confirmed_safe: dict[str, set], violated: dict[str, set],
                                    single_pass_ids: dict[tuple[int, str, int], set[str]]) -> None:
    """Second-tier discovery on top of run_restriction_discovery(): sweeps every PAIR of
    the 12 restrictions together (C(12,2) = 66 combos, crossed with every serving count)
    instead of one at a time. See module docstring's PAIR-RESTRICTION DISCOVERY section
    for why pairs (not the full 4096-subset powerset), and for the single-vs-pair
    attribution technique this function's violated[] detection relies on.

    Contributes to `combined` and `confirmed_safe` exactly like run_restriction_discovery()
    does (mutated in place, same shared dicts -- finalized together by
    finalize_restriction_tags() in run()). Presence under a pair exclusion is airtight
    proof of non-violation for BOTH restrictions in that pair -- same structural
    guarantee as the single-restriction case, regardless of how many restrictions are
    combined -- so confirmed_safe gets both added, not just one.

    violated[] detection: for pair (i, j), a recipe present in single_pass_ids[(i, slug,
    serving_count)] (run_restriction_discovery()'s own observation of THAT slug/serving
    combo with only i excluded) but missing from this pair's own observed present_ids
    is attributed to violating j -- i's exclusion is identical in both observations, so
    j is the only thing that changed. Symmetrically, missing from single_pass_ids[(j,
    ...)] attributes to violating i. This is NOT the same ambiguous move as diffing
    straight from the TRUE baseline to a pair (which can't tell which of the two excluded
    restrictions caused a disappearance) -- exactly one restriction differs between each
    of these two observations, so the attribution is as clean as the single-restriction
    pass's own baseline diff. single_pass_ids has no entry for a given (i, slug,
    serving_count) if run_restriction_discovery() wasn't run first (or that particular
    slug/serving combo's fetch failed) -- silently skipped, not guessed at.

    Restores Classic/no-restrictions/4-servings before returning."""
    pairs = list(combinations(RESTRICTION_IDS, 2))
    total = len(pairs) * len(serving_counts)
    combo_i = 0

    for serving_count in serving_counts:
        for pair in pairs:
            combo_i += 1
            label = " + ".join(RESTRICTIONS[r] for r in pair)
            print(f"\n[pair-restriction-discovery {combo_i}/{total}] {label} (servings={serving_count})")
            set_profile(session, CLASSIC_DIET_ID, serving_count, list(pair), [])

            for slug in slugs:
                try:
                    thumbs = fetch_cookbook_via_data_route(session, build_id, slug)
                except requests.exceptions.RequestException as exc:
                    print(f"    [skip] {slug}: giving up after retries ({exc})")
                    continue
                if thumbs is None:
                    continue

                present_ids = set()
                new_count = 0
                for thumb in thumbs:
                    if "legacyId" not in thumb or thumb["legacyId"] is None:
                        continue
                    present_ids.add(thumb["id"])
                    for restriction_id in pair:
                        confirmed_safe[thumb["id"]].add(restriction_id)
                    if thumb["id"] not in combined:
                        record = normalize_thumb(thumb, slug)
                        record["cookbook_slugs"] = {slug}
                        record["found_via_restriction"] = f"{label} @ {serving_count} servings"
                        combined[thumb["id"]] = record
                        new_count += 1

                i, j = pair
                reference_i = single_pass_ids.get((i, slug, serving_count))
                if reference_i is not None:
                    for missing_id in reference_i - present_ids:
                        violated[missing_id].add(j)
                reference_j = single_pass_ids.get((j, slug, serving_count))
                if reference_j is not None:
                    for missing_id in reference_j - present_ids:
                        violated[missing_id].add(i)

                if new_count:
                    print(f"    {slug}: {new_count} new (total unique so far: {len(combined)})")
                time.sleep(delay)

    set_profile(session, CLASSIC_DIET_ID, 4, [], [])  # restore before returning


def finalize_restriction_tags(combined: dict[str, dict], violated: dict[str, set],
                               confirmed_safe: dict[str, set]) -> None:
    """Applies violated_restriction_ids to `combined` from whatever combination of
    run_restriction_discovery() and run_pair_restriction_discovery() ran -- see their
    docstrings for what `violated`/`confirmed_safe` mean.

    COMPLETENESS CHECK: a recipe gets a real violated_restriction_ids list only once
    every one of the 12 restrictions has a definite verdict for it -- confirmed safe
    (in confirmed_safe) or confirmed violated (in violated), the two sets are disjoint
    for a well-behaved id, so their union reaching all 12 means nothing is left unknown.
    This is deliberately NOT restricted to "seen in a true baseline pass" -- that was
    the original, narrower bar, but it's just one way to reach full coverage; a recipe
    resolved entirely through run_pair_restriction_discovery()'s single-vs-pair
    attribution (see its docstring) reaches the exact same 12-of-12 completeness by a
    different route, anchored on a single-restriction pass instead of "no restrictions"
    -- and deserves the same trust. A recipe short of all 12 keeps violated_restriction_ids
    unset (None via run()'s setdefault) -- genuinely unknown, not fabricated as either
    "violates nothing" or "confirmed safe from the few restrictions actually checked" --
    that conflation (writing [] the moment ANY one restriction was confirmed safe,
    regardless of the other 11) was a real, confirmed bug: roughly half of all []
    entries in a real sweep run were this partial case, not genuine full compliance."""
    for recipe_id in combined:
        known = confirmed_safe.get(recipe_id, set()) | violated.get(recipe_id, set())
        if len(known) >= len(RESTRICTION_IDS):
            combined[recipe_id]["violated_restriction_ids"] = sorted(violated.get(recipe_id, set()))


def run(out_dir: Path, serving_counts: list[int], slugs: list[str] | None, delay: float,
        restriction_discovery: bool, pair_restriction_discovery: bool, allow_shrink: bool = False) -> None:
    if not any(HEADERS.values()):
        raise SystemExit("No session headers -- albertsons_headers.json is missing or empty. Capture "
                          "them from DevTools and load them with set_headers.py (see albertsons_auth.py).")

    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    print("Discovering current buildId and category slugs from the homepage...")
    build_id, discovered_slugs = discover_build_id_and_slugs()
    full_sweep = slugs is None  # explicit --slugs runs are partial by design, so never size-checked
    if slugs is None:
        slugs = discovered_slugs
    print(f"buildId: {build_id}")
    print(f"Found {len(slugs)} categories: {', '.join(slugs)}")

    combined = run_discovery(session, slugs, serving_counts, delay)

    violated: dict[str, set] = defaultdict(set)
    confirmed_safe: dict[str, set] = defaultdict(set)
    single_pass_ids: dict[tuple[int, str, int], set[str]] = {}

    if pair_restriction_discovery and not restriction_discovery:
        print("\nWARNING: --pair-restriction-discovery without --restriction-discovery has no "
              "single-restriction pass data to attribute violations against (see "
              "run_pair_restriction_discovery()'s docstring) -- it'll still contribute discovery "
              "and confirmed_safe evidence, just no violated_restriction_ids from this run.")

    if restriction_discovery:
        print(f"\nRunning restriction-discovery pass via the SSR data route "
              f"({len(RESTRICTION_IDS) + 1} passes x {len(serving_counts)} serving size(s) "
              f"x {len(slugs)} categories)...")
        run_restriction_discovery(session, slugs, build_id, combined, serving_counts, delay,
                                   violated, confirmed_safe, single_pass_ids)

    if pair_restriction_discovery:
        pair_count = len(list(combinations(RESTRICTION_IDS, 2)))
        print(f"\nRunning pair-restriction-discovery pass via the SSR data route "
              f"({pair_count} pairs x {len(serving_counts)} serving size(s) "
              f"x {len(slugs)} categories)...")
        run_pair_restriction_discovery(session, slugs, build_id, combined, serving_counts, delay,
                                        confirmed_safe, violated, single_pass_ids)

    if restriction_discovery or pair_restriction_discovery:
        finalize_restriction_tags(combined, violated, confirmed_safe)

    result_list = []
    for record in combined.values():
        record["cookbook_slugs"] = sorted(record["cookbook_slugs"])
        record.setdefault("violated_restriction_ids", None)
        record.setdefault("found_via_restriction", None)
        result_list.append(record)
    result_list.sort(key=lambda r: r["legacy_id"])

    out_path = out_dir / "albertsons_full_sweep.json"
    if full_sweep and not allow_shrink and out_path.exists():
        try:
            previous_count = len(json.loads(out_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            previous_count = 0
        if previous_count and len(result_list) < MIN_SWEEP_FRACTION * previous_count:
            rejected = out_path.with_name("albertsons_full_sweep.rejected.json")
            rejected.write_text(json.dumps(result_list, indent=2, ensure_ascii=False), encoding="utf-8")
            raise SystemExit(f"Sweep found only {len(result_list)} recipes vs {previous_count} in the existing "
                              f"file (< {MIN_SWEEP_FRACTION:.0%}); keeping the existing sweep. The new result "
                              f"was saved to {rejected.name}. Use --allow-shrink if that is really expected.")
    temp_path = out_path.with_name(out_path.name + ".tmp")
    temp_path.write_text(json.dumps(result_list, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(out_path)  # atomic: a crash mid-write can't leave a truncated sweep behind

    print(f"\nDone. {len(result_list)} unique recipes found across "
          f"{len(serving_counts)} serving size(s) x {len(slugs)} categor{'y' if len(slugs) == 1 else 'ies'}.")
    if restriction_discovery or pair_restriction_discovery:
        found = sum(1 for r in result_list if r["found_via_restriction"] is not None)
        print(f"Of those, {found} were found only via restriction-discovery "
              f"(single and/or pair passes, not by the serving-count sweep).")
        tagged = sum(1 for r in result_list if r["violated_restriction_ids"] is not None)
        print(f"Restriction tags recorded for {tagged} recipe(s) with a definite verdict "
              f"(safe or violates) for all 12 restrictions -- via a true baseline pass, "
              f"via run_pair_restriction_discovery()'s single-vs-pair attribution, or both "
              f"-- see finalize_restriction_tags()'s docstring.")
    print(f"Saved: {out_path}")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--servings", default=",".join(str(s) for s in SERVING_COUNTS),
                        help=f"Comma-separated serving counts to sweep (default: {SERVING_COUNTS})")
    parser.add_argument("--slugs", default=None,
                        help="Comma-separated cookbook slugs to use instead of auto-discovering "
                             "them from the homepage")
    parser.add_argument("--delay", type=float, default=0.4,
                        help="Seconds between requests (default: 0.4)")
    parser.add_argument("--restriction-discovery", action="store_true",
                        help="Also sweep all 12 restrictions (+ a no-restrictions baseline) crossed with "
                             "every --servings value (hardwired together, not separately configurable -- "
                             "see module docstring) through the SSR data route, adding any recipe this "
                             "reveals that the serving-count sweep didn't already find, AND tagging "
                             "violated_restriction_ids for every recipe seen in a baseline pass (adds "
                             "13 x len(servings) x len(slugs) requests).")
    parser.add_argument("--pair-restriction-discovery", action="store_true",
                        help="Also sweep every PAIR of the 12 restrictions together (C(12,2) = 66 combos, "
                             "crossed with every --servings value) via the SSR data route -- a deeper, "
                             "optional second tier on top of --restriction-discovery. STRONGLY recommended "
                             "to pass --restriction-discovery too (not required, but this flag's "
                             "violation-tagging is only possible when it has that pass's single-restriction "
                             "data to attribute against -- see run_pair_restriction_discovery()'s docstring; "
                             "without it, this still contributes discovery + confirmed_safe evidence, same "
                             "as before). See module docstring's PAIR-RESTRICTION DISCOVERY section for why "
                             "pairs and not the full 4096-subset powerset (adds "
                             "66 x len(servings) x len(slugs) requests).")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="Save the sweep even if it found far fewer recipes than the existing file "
                             "(normally refused as a probable bad run -- see MIN_SWEEP_FRACTION).")
    return parser.parse_args()


def main():
    args = parse_args()
    serving_counts = [int(s.strip()) for s in args.servings.split(",") if s.strip()]
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()] if args.slugs else None

    run(args.out_dir, serving_counts, slugs, args.delay,
        args.restriction_discovery, args.pair_restriction_discovery, args.allow_shrink)


if __name__ == "__main__":
    main()
