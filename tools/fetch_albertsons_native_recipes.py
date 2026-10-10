"""
Fetches full recipe detail for Albertsons-native recipes (ones with no Mealime
legacy_id match -- e.g. most of "Kid's recipe hub") directly from Albertsons' own
gated recipe API, using headers YOU capture from your own logged-in browser session.

This is intentionally NOT something the assistant runs against the live API itself --
capture the headers yourself (from DevTools) and run this file locally.

How to get the headers:
  1. On any meal-plans-recipes cookbook page, open DevTools -> Network tab.
  2. Click into any recipe so a request to
     .../abs/pub/dirm/menuservice/v2/recipe?id=... fires.
  3. Right-click that request -> Copy -> Copy as cURL (bash).
  4. Run set_headers.py and paste it in. That writes the git-ignored albertsons_headers.json
     this script reads (see albertsons_auth.py) -- the real values never go in any source file.
     This script uses the Authorization and Ocp-Apim-Subscription-Key headers.
  A 401/403 answer stops the run (AuthExpired) instead of failing every remaining id.

OUTPUT LAYOUT: writes into families/<parent_id>/<legacy_id>_<slug>.json under --out-dir
-- the same convention albertsons_library_diff.py and the existing Mealime libraries
use (e.g. free-recipe-database), so --out-dir can safely BE an existing library root to
merge straight in, not just a scratch folder. Skips (doesn't re-fetch) any id that
already has a matching <legacy_id>_*.json file in that family folder, so re-running on
an overlapping ids list -- or fetching straight into a library that already has some of
these -- is a safe, cheap no-op rather than a duplicate/overwrite.

SCHEMA: Albertsons' v2/recipe response is a completely different, incompatible shape
from the rest of this library (legacyId/parentId instead of id/recipe_id, ingredients
nested under bundle.items, instructions with no ingredient-callout breakdown, no
units/serving_count/cookwares at the top level, etc.) -- writing it raw would crash
mealime_generate_manifest.py and recipe_export.py the moment either one reached one of
these files (both do a bare record["recipe_id"], no .get). build_variants() below maps
it onto the exact same shape mealime_json_scrape.py's build_record() produces, so every
existing downstream tool keeps working unmodified. One real, unavoidable data gap:
Albertsons' v2/recipe never returns nutrition data (confirmed empty across every recipe
fetched this way), so normalized nutrition is always {} -- same as how every consumer
already treats missing nutrition fields elsewhere in the library, just total here
instead of partial. The recipe's thumbnail (if any) is downloaded into the family folder
as thumbnail.<ext>, same as mealime_json_scrape.py's save_family_data() -- skipped if a
sibling variant already saved one.

kid_friendly is a NEW field with no Mealime-schema equivalent (genuine Mealime records
never have it, so consumers should use record.get("kid_friendly") not a bare index):
True when the discovery-time sweep found this recipe under Albertsons' "kids-recipe-hub"
cookbook, an Albertsons-side curation flag with no corresponding Mealime variety_tag_id
-- see mealime_id_reference.is_kid_friendly().

cookbook_slugs is likewise new: the raw list of Albertsons cookbook slugs the discovery
sweep found this recipe under ("super-simple", "dessert", ...), stored as-is so the
export step (recipe_export.py) can decide which become Norish tags without another
fetch. Only 10 of them map to Mealime variety_tag_ids and one to kid_friendly; the rest
used to be dropped here. albertsons_cookbook_slugs_sync.py applies the same field to
already-fetched recipes (and to genuine Mealime recipes the sweep also lists) straight
from the sweep file, no live API needed.

UNIT-ONLY EXPANSION (US + Metric, no serving-size synthesis): Albertsons only ever
gives us ONE (serving count, US-labeled) combo per fetch. An earlier version of this
script also synthesized 2/4/6-serving variants by proportionally scaling every
ingredient quantity -- mathematically defensible for continuous quantities (spices,
liquids, weights), but it broke down for discrete/countable ingredients: scaling "4
eggs" to a 2/3 ratio produces "2 2/3 eggs", which nobody can actually measure. Since
there's no way to tell, from the quantity alone, which ingredients are countable versus
continuous, serving-size synthesis was removed entirely -- build_variants() now only
ever reflects the ONE serving count Albertsons actually returned. Unit conversion
(US<->Metric) is kept: it's a straight relabeling of the SAME quantity (not a
proportional guess), verified against a real Mealime US/Metric sibling pair
(families/799, ids 7023-7028), and it structurally can't produce a fractional discrete
count either -- see convert_ingredient_quantity(): cup/tbsp/tsp/bare-count units
(including "4 eggs") are UNIT-SYSTEM-AGNOSTIC and never convert at all; only oz/fl-oz
-based units get converted, using amount.inG/inMl (the metric equivalent Albertsons
already computed for the exact quantity shown, no scaling involved). Ingredient NAMES
and step primary_message text are reused verbatim between the two variants -- neither
depends on unit system. secondary_message (the per-step ingredient-quantity callout) IS
rebuilt per variant from Albertsons' own lineItems when a step has them (matching each
lineItem back to its bundle.items entry by name) -- about 8.5% of recipes have no
lineItems on any step at all, and get secondary_message=None throughout.

PER-STEP QUANTITY SPLITS (preserved, never collapsed to the ingredient's total): an
ingredient can be called out in more than one step at DIFFERENT amounts -- confirmed on
legacyId 1019960 (Slow Cooker Cheesy Beef Lasagna), where salt is 3/4 tsp browning the
beef and 1/4 tsp in the ricotta mixture (summing to the master list's 1 tsp), and same
for black pepper (1/2 tsp + 1/4 tsp = 3/4 tsp). An earlier version of this script
rendered EVERY mention of an ingredient using the master ingredient's own total quantity
-- so both steps above would have shown "1 tsp salt", doubling it in the actual cooked
result if followed literally. build_instructions_for_combo() now reads each lineItem's
OWN leading quantity text (via parse_leading_amount()) and scales/converts THAT number
for the combo, only falling back to the ingredient's full total when there's exactly one
mention of it in the whole recipe (the common case, where the one mention necessarily
IS the total -- no information at risk). If an ingredient has multiple mentions but a
particular one can't be confidently parsed or its unit doesn't match the master
item's own unit, that occurrence is shown with no quantity (bare name) rather than
guessing -- same "don't fabricate" posture as mealime_quantity_backfill.py.

MASTER QUANTITY BACKFILL FROM PER-STEP SUMS: mirrors mealime_quantity_backfill.py's own
job for genuine Mealime data, applied here for the rare case Albertsons' own top-level
bundle.items quantity is itself unparseable (e.g. "to taste"): if every per-step mention
of that ingredient shares the same unit, they're summed and used as the master quantity
instead of falling through to formattedQuantity/label text. In practice Albertsons'
own totals are essentially always present (0 empty across all 1,649 Albertsons-sourced
records already in this library as of this fix), so this mostly guards against future
fetches, not a gap in existing data.

The US variant keeps Albertsons' real legacyId as "id" (needed for already_fetched()'s
dedup check and everything else in the pipeline that keys off it); the Metric variant
gets a deterministic synthetic id (legacy_id * 10 + 1) since Albertsons never issues a
separate id for a combo it didn't actually render -- see synthetic_metric_variant_id().

NON-NATIVE GAP-FILL: when parentId != legacyId (see NotAlbertsonsNative), this recipe
belongs to a real, already-scraped Mealime family rather than being Albertsons-only --
build_variants() is refused for these, since writing synthetic-schema siblings would
duplicate or conflict with the family's real, richly-authored Mealime data. Confirmed
to have happened for 242 recipes across 167 families before this gate existed (see
conversation/session notes for the cleanup). BUT: if this exact legacy_id was never
actually part of that family locally (a genuine missing variant slot, not a duplicate
of one already covered -- albertsons_library_diff.py already only ever hands fetch this
script "new_variant"/"new_family" ids, so this is the common case, not the exception),
there's no real sibling to conflict with. For that case,
build_single_native_gap_variant() writes just the US+Metric pair for the ONE serving
count Albertsons actually returned (same unit-only expansion as build_variants(), no
serving synthesis here either), under its real legacyId, filling that one slot rather
than skipping it outright. It deliberately does NOT overwrite anything by default:
already_fetched()'s existing-file check (using the discovery-time legacy_id, before any
fetch happens at all) is what protects a slot that turns out to already be filled.

--OVERWRITE: re-fetches and replaces ids that already have a file, instead of skipping
them -- for picking up a fix to how this script builds a recipe (e.g. the per-step
quantity fix -- see build_instructions_for_combo()'s docstring) in recipes it already
wrote before that fix existed. Guarded against the one genuinely dangerous case this
enables: an existing file at that legacy_id that ISN'T one of ours (source isn't
"albertsons_native"/"albertsons_native_gap_fill" -- e.g. a genuinely-scraped Mealime
file that happens to collide on id) is refused, never overwritten, exactly the mistake
NotAlbertsonsNative's own gate exists to prevent elsewhere in this file. The old file(s)
for that legacy_id are deleted right before the fresh ones are written (not left
alongside), so a slug change since the original fetch can't leave an orphaned duplicate.

Usage:
    python fetch_albertsons_native_recipes.py --ids-json albertsons_recipe_ids.json --out-dir albertsons_native_recipes
    python fetch_albertsons_native_recipes.py --ids-json albertsons_recipe_ids.json --out-dir "D:\\Google Drive\\GitHub Pages\\nickuhlig.github.io\\projects\\free-recipe-database"
    (optionally add --only-legacy-id-over 100000 to just do the non-Mealime ones)
    (add --overwrite to re-fetch and replace ids you already have, e.g. after a fix)
"""

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from albertsons_auth import check_response, load_headers
from albertsons_library_diff import strip_disambiguating_suffix
from mealime_id_reference import MENU_TYPES, is_kid_friendly, variety_tag_ids_from_cookbook_slugs

RECIPE_URL_TEMPLATE = "https://www.albertsons.com/abs/pub/dirm/menuservice/v2/recipe?id={id}"
FAMILIES_DIRNAME = "families"
UNIT_SYSTEMS = ["US", "Metric"]

# Each full recipe's own "diets" field (e.g. ["classic","flexitarian","pescatarian",
# "vegetarian"]) is a property of the recipe itself -- unlike the account-level diet
# preference, which we confirmed does NOT filter v2/cookbook's category listings at
# all (even with a live, Vegan-set session, the raw data includes non-vegan dishes).
# So instead of an 8-way discovery sweep, we just read this field straight off each
# recipe once we're fetching it anyway, and translate it to the same numeric
# allowed_type_ids mealime_authenticated_sweep.py uses (mealime_id_reference.py's
# MENU_TYPES). Only "classic"/"vegetarian"/"flexitarian"/"pescatarian"/"vegan" have
# been seen in real data so far -- "paleo"/"low carb"/"keto" aliases below are
# best-guess spellings; unrecognized strings are logged (not silently dropped) so the
# mapping can be corrected once we see a real example.
DIET_NAME_TO_ID = {name.lower(): tid for tid, name in MENU_TYPES.items() if tid != -1}
DIET_NAME_TO_ID.update({
    "paleo-friendly": 5,
    "low carb": 7,
    "low-carb": 7,
    "carb-conscious": 7,
    "keto-friendly": 8,
})


def diets_to_allowed_type_ids(diets: list[str]) -> list[int]:
    ids = []
    for name in diets:
        tid = DIET_NAME_TO_ID.get(name.lower())
        if tid is None:
            print(f"    [!] unrecognized diet name {name!r} -- add it to DIET_NAME_TO_ID")
            continue
        ids.append(tid)
    return sorted(set(ids))

# The captured session headers come from the git-ignored albertsons_headers.json (create or
# refresh it with set_headers.py) -- see albertsons_auth.py. Never paste real values in here.
HEADERS = load_headers(("Authorization", "Ocp-Apim-Subscription-Key"))


def load_targets(ids_json: Path, only_legacy_id_over: int | None) -> list[dict]:
    records = json.loads(ids_json.read_text(encoding="utf-8"))
    if only_legacy_id_over is not None:
        records = [r for r in records if r["legacy_id"] > only_legacy_id_over]
    return records


def fetch_one(session: requests.Session, albertsons_id: str) -> dict | None:
    resp = session.get(RECIPE_URL_TEMPLATE.format(id=albertsons_id), timeout=20)
    check_response(resp, f"recipe {albertsons_id}")  # 401/403 -> AuthExpired: stop instead of failing every id
    if resp.status_code != 200:
        print(f"    -> HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


def family_dir(out_dir: Path, parent_id: int) -> Path:
    return out_dir / FAMILIES_DIRNAME / str(parent_id)


# --- quantity parsing/formatting -----------------------------------------------

UNICODE_FRACTION_VALUES = {
    "¼": 0.25, "½": 0.5, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3,
    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}
# Snapping targets for format_quantity_number, widest-tolerance-safe ordering doesn't
# matter here since matches are exact-within-tolerance, not first-match-wins across
# overlapping ranges.
FRACTION_DISPLAY = [
    (0.125, "⅛"), (0.25, "¼"), (1 / 3, "⅓"), (0.375, "⅜"), (0.5, "½"),
    (0.625, "⅝"), (2 / 3, "⅔"), (0.75, "¾"), (0.875, "⅞"),
]


def parse_quantity_number(text: str | None) -> float | None:
    """Best-effort numeric parse of Albertsons' own bundle.items[].quantity field
    (already isolated from the unit, unlike Mealime's combined display strings) --
    handles plain ints/decimals, "a/b" fractions, and a bare unicode fraction, in case
    any of those show up. Returns None for anything unparseable (e.g. "to taste"),
    which callers treat as "can't scale this one, pass the original text through"."""
    text = (text or "").strip()
    if not text:
        return None

    for symbol, value in UNICODE_FRACTION_VALUES.items():
        if text == symbol:
            return value
        if text.endswith(symbol):
            whole_part = text[: -len(symbol)].strip()
            if whole_part.isdigit():
                return int(whole_part) + value

    # mixed or bare ASCII fraction, e.g. "1 1/4" or "1/2"
    fraction_match = re.match(r"^(\d+)?\s*(\d+)/(\d+)$", text)
    if fraction_match:
        whole_str, num_str, denom_str = fraction_match.groups()
        try:
            return (int(whole_str) if whole_str else 0) + float(num_str) / float(denom_str)
        except ZeroDivisionError:
            pass

    try:
        return float(text)
    except ValueError:
        return None


def format_quantity_number(n: float) -> str:
    """Renders a scaled quantity the way real Mealime data does -- whole numbers bare,
    common fractions as a unicode glyph (with a preceding whole-number part separated
    by a space, e.g. "1 ½", matching families/799's real US/Metric sibling data)."""
    whole = int(n)
    frac = n - whole
    if frac < 0.02:
        return str(whole)
    for value, symbol in FRACTION_DISPLAY:
        if abs(frac - value) < 0.02:
            return f"{whole} {symbol}".strip() if whole else symbol
    return f"{round(n, 2):g}"


def format_metric_total(total: float, unit_kind: str) -> str:
    """unit_kind is 'g' or 'ml' -- collapses to kg/L above 1000, the same threshold
    recipe_export.py's Norish exporter uses for the same reason (nicer display)."""
    if unit_kind == "g":
        return f"{round(total / 1000, 2):g} kg" if total >= 1000 else f"{round(total)} g"
    return f"{round(total / 1000, 2):g} L" if total >= 1000 else f"{round(total)} ml"


# --- ingredient scaling + unit conversion ---------------------------------------

MEASURING_TOOL_UNITS = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
}
BARE_WEIGHT_UNITS = {"lb", "lbs", "pound", "pounds", "oz", "ounce", "ounces"}
PACKAGE_OZ_RE = re.compile(r"\(([\d.]+)\s*oz\)", re.IGNORECASE)


def pluralize_measuring_unit(unit: str, amount: float) -> str:
    """Albertsons' own unit field is always singular ("cup"); its formattedQuantity
    pluralizes for display ("1 1/4 cups"), which is lost by building off unit directly.
    Real Mealime data pluralizes cup -> cups above 1 but never pluralizes tbsp/tsp
    (confirmed: "2 tbsp", not "2 tbsps") -- so only cup needs this."""
    if unit.lower() == "cup" and amount > 1.01:
        return "cups"
    return unit


def convert_ingredient_quantity(item: dict, scale_factor: float, target_units: str,
                                 base_num_override: float | None = None) -> str:
    """Scales one bundle.items entry to a target serving ratio and, for Metric, converts
    units per the rule verified against real Mealime US/Metric sibling data -- see the
    module docstring's 6-VARIANT EXPANSION section.

    base_num_override, when given, replaces the item's OWN total quantity as the number
    being scaled/converted -- everywhere that number would otherwise appear (the scaled
    display number, and the amount.inG/inMl figures, which describe the item's full
    total and are ratio'd down to match). Used by build_instructions_for_combo() to
    render one STEP's own portion of an ingredient (e.g. the 3/4 tsp of salt used
    browning the beef, out of 1 tsp total) instead of repeating the ingredient's whole
    total at every step it's mentioned in -- see the module docstring's PER-STEP
    QUANTITY SPLITS section. Works even when the item's OWN total is itself unparseable
    (e.g. "to taste") -- the override is trusted on its own in that case, rather than
    bailing out just because the (irrelevant, here) total couldn't be read; only the
    portion-of-total math (amount.inG/inMl ratio, package-unit per-unit grams) needs
    that total and is skipped -- not faked -- when it's unavailable."""
    unit = (item.get("unit") or "").strip()
    if unit == "-":
        unit = ""  # Albertsons' own placeholder for "no real unit" (e.g. a counted egg)
    unit_lower = unit.lower()
    original_num = parse_quantity_number(item.get("quantity"))

    if original_num is None and base_num_override is None:
        # Can't scale numerically (e.g. "to taste") -- pass the display text through
        # unscaled rather than guessing.
        return item.get("formattedQuantity") or item.get("label") or ""

    base_num = base_num_override if base_num_override is not None else original_num
    portion = (base_num / original_num) if original_num else None  # None = unknowable, not "whole"
    scaled_num_str = format_quantity_number(base_num * scale_factor)

    if not unit:
        return scaled_num_str  # bare count, e.g. "2" bananas -- no unit, no conversion

    package_match = PACKAGE_OZ_RE.search(unit)
    if package_match and target_units == "Metric":
        amount = item.get("amount") or {}
        in_g = amount.get("inG")
        grams_per_unit = (in_g / original_num) if in_g and original_num else float(package_match.group(1)) * 28.3495
        metric_unit = PACKAGE_OZ_RE.sub(f"({grams_per_unit:.0f} g)", unit)
        return f"{scaled_num_str} {metric_unit}".strip()

    if unit_lower in MEASURING_TOOL_UNITS:
        display_unit = pluralize_measuring_unit(unit, base_num * scale_factor)
        return f"{scaled_num_str} {display_unit}".strip()

    if package_match:
        # Package units under US keep their oz label untouched.
        return f"{scaled_num_str} {unit}".strip()

    if target_units == "Metric" and portion is not None:
        amount = item.get("amount") or {}
        in_g, in_ml = amount.get("inG"), amount.get("inMl")
        if unit_lower in BARE_WEIGHT_UNITS and in_g is not None:
            return format_metric_total(in_g * portion * scale_factor, "g")
        if "fl" in unit_lower and "oz" in unit_lower and in_ml is not None:
            return format_metric_total(in_ml * portion * scale_factor, "ml")
        # Anything else (spritz, pinch, clove, slice, etc.) falls through to the
        # conservative fallback below rather than converting to a g/mL figure that
        # happens to exist in amount.* but isn't a real weight/volume unit a cook
        # would recognize.

    # Unrecognized unit, Metric requested with no matching amount.inG/inMl to convert
    # from, or (portion is None) no real total to ratio a weight/volume conversion
    # against -- conservative fallback: keep the unit text, just scale the number.
    return f"{scaled_num_str} {unit}".strip()


def get_valid_bundle_items(data: dict) -> list[dict]:
    """Albertsons' bundle.items occasionally includes a bare placeholder entry with no
    name at all (e.g. {"amount": {}, "shoppable": true}, confirmed on legacyId 1031996)
    -- not a real ingredient, so it's dropped here rather than crashing or showing up
    as a blank ingredient line. Filtered once, in this single place, so
    build_ingredients_for_combo() and build_instructions_for_combo()'s lineItem
    index-matching always iterate the identical list and stay aligned with each other."""
    return [item for item in (data.get("bundle") or {}).get("items", []) if item.get("name")]


def build_ingredients_for_combo(data: dict, scale_factor: float, target_units: str) -> list[dict]:
    return [
        {
            "ingredient_id": None,
            "name": item["name"],
            "quantity": convert_ingredient_quantity(item, scale_factor, target_units),
        }
        for item in get_valid_bundle_items(data)
    ]


def _match_lineitem(formatted_text: str, original_items: list[dict]) -> tuple[int, str] | None:
    """Matches a raw lineItem's 'formatted' text (e.g. '2 tsp butter, unsalted') back to
    its index in bundle.items by name -- longest name match wins, same tie-break
    recipe_export.py's own callout-matching uses for the analogous Mealime case. Also
    returns the LEADING text before the matched name (e.g. '2 tsp'), which is this
    specific step's own stated amount -- see parse_leading_amount() and the module
    docstring's PER-STEP QUANTITY SPLITS section for why that's read instead of always
    substituting the ingredient's full total."""
    core = formatted_text.strip().rstrip(".")
    core_lower = core.lower()
    best_index, best_len = None, -1
    for i, item in enumerate(original_items):
        name_lower = item["name"].strip().lower()
        if core_lower.endswith(name_lower) and len(name_lower) > best_len:
            best_index, best_len = i, len(name_lower)
    if best_index is None:
        return None
    return best_index, core[: len(core) - best_len].strip()


def parse_leading_amount(text: str) -> tuple[float | None, str]:
    """Parses a lineItem's own leading quantity text (e.g. '3/4 tsp' out of '3/4 tsp
    salt', already stripped of the ingredient name by _match_lineitem()) into
    (amount, remaining unit text). Tries the longest leading run of whitespace-separated
    tokens parse_quantity_number() accepts, since a mixed number like '1 1/2' spans two
    tokens. Returns (None, text) if no leading number parses at all (e.g. 'to taste')."""
    tokens = text.split()
    for i in range(len(tokens), 0, -1):
        amount = parse_quantity_number(" ".join(tokens[:i]))
        if amount is not None:
            return amount, " ".join(tokens[i:]).strip()
    return None, text.strip()


def _sum_same_unit_mentions(mentions: list[tuple[float, str]]) -> tuple[float, str] | None:
    """Sums a list of (amount, unit-text) mentions of the SAME ingredient across
    different steps, e.g. [(0.75, 'tsp'), (0.25, 'tsp')] -> (1.0, 'tsp'). Refuses (returns
    None) if the mentions don't all share one unit -- exactly as unresolvable as no
    mention at all, so it's left for the caller to fall back rather than guess at a
    cross-unit conversion. (Every real example seen so far states one ingredient in one
    consistent unit throughout a recipe, unlike genuine Mealime data's tsp/tbsp/cup mix,
    so this doesn't need mealime_quantity_backfill.py's fuller unit-family conversion.)"""
    units = {unit.strip().lower().rstrip(".") for _, unit in mentions}
    if len(units) != 1:
        return None
    return sum(amount for amount, _ in mentions), mentions[0][1]


def build_instructions_for_combo(data: dict, ingredients_for_combo: list[dict],
                                  target_units: str) -> list[dict]:
    """primary_message is reused verbatim (see module docstring for why); secondary_message
    is rebuilt per step from lineItems (when present), reading each STEP'S OWN stated
    quantity rather than always substituting the ingredient's full total -- see the
    module docstring's PER-STEP QUANTITY SPLITS section. Also backfills
    ingredients_for_combo's own quantity (in place) for any ingredient whose Albertsons
    total was itself unparseable, by summing its per-step mentions -- see the module
    docstring's MASTER QUANTITY BACKFILL section.

    scale_factor is always 1.0 here: Albertsons only ever gives one serving count per
    fetch (see the module docstring's UNIT-ONLY EXPANSION section), so there's never a
    serving-size ratio to apply, only the target_units conversion."""
    scale_factor = 1.0
    original_items = get_valid_bundle_items(data)

    # First pass: find every lineItem mention up front, grouped by ingredient index, so
    # we know (a) how many times each ingredient is mentioned at all -- a single mention
    # unambiguously IS the ingredient's full total, no parsing required -- and (b) what
    # to sum for the master-quantity backfill.
    per_step_matches: list[list[tuple[int, str]]] = []
    mentions_by_index: dict[int, list[tuple[float, str]]] = {}
    for step in data.get("instructions", []):
        step_matches = []
        for line_item in step.get("lineItems") or []:
            formatted = line_item.get("formatted")
            if not formatted:
                continue
            match = _match_lineitem(formatted, original_items)
            if match is None:
                continue
            idx, leading = match
            step_matches.append((idx, leading))
            amount, unit_text = parse_leading_amount(leading)
            if amount is not None:
                mentions_by_index.setdefault(idx, []).append((amount, unit_text))
        per_step_matches.append(step_matches)

    mention_counts: dict[int, int] = {}
    for step_matches in per_step_matches:
        for idx, _ in step_matches:
            mention_counts[idx] = mention_counts.get(idx, 0) + 1

    # Master quantity backfill: only for ingredients Albertsons itself left unparseable.
    for idx, item in enumerate(original_items):
        if parse_quantity_number(item.get("quantity")) is not None:
            continue  # Albertsons already gave a usable total -- nothing to backfill.
        summed = _sum_same_unit_mentions(mentions_by_index.get(idx, []))
        if summed is None:
            continue
        summed_amount, summed_unit = summed
        backfill_item = {**item, "quantity": format_quantity_number(summed_amount), "unit": summed_unit}
        ingredients_for_combo[idx]["quantity"] = convert_ingredient_quantity(
            backfill_item, scale_factor, target_units)

    instructions = []
    for step, step_matches in zip(data.get("instructions", []), per_step_matches):
        secondary_message = None
        for idx, leading in step_matches:
            ing = ingredients_for_combo[idx]
            original_item = original_items[idx]
            original_unit = (original_item.get("unit") or "").strip().lower().rstrip(".")

            line_quantity = ing["quantity"]  # default: only mention -- IS the full total
            if mention_counts[idx] > 1:
                amount, unit_text = parse_leading_amount(leading)
                if (amount is not None
                        and unit_text.strip().lower().rstrip(".") == original_unit):
                    line_quantity = convert_ingredient_quantity(
                        original_item, scale_factor, target_units, base_num_override=amount)
                else:
                    # Multiple mentions but THIS one can't be confidently attributed a
                    # specific amount -- show the name only rather than repeating the
                    # ingredient's full total at every mention (the bug this replaces).
                    line_quantity = ""

            line = f"{line_quantity} {ing['name']}".strip() if line_quantity else ing["name"]
            secondary_message = (secondary_message + "\n" + line) if secondary_message else line
        instructions.append({"primary_message": step["message"], "secondary_message": secondary_message})
    return instructions


# Mealime's own variant ids top out around 41,000, so legacy_id * 10 + 1 can only land on a
# genuine Mealime id below this ceiling -- i.e. only for gap-fill recipes, whose legacy ids are
# Mealime-range numbers (an Albertsons-only 7-digit legacy id gives an 8-digit id, far above).
MEALIME_ID_CEILING = 100_000
# Added to a colliding id: puts it above every other id in the library (albertsons-native
# Metric ids peak near 10.4 million) so it can never collide again.
COLLISION_ID_OFFSET = 20_000_000

_library_root: Path | None = None
_id_families: dict[int, set[str]] | None = None


def set_library_root(out_dir: Path) -> None:
    """Tells synthetic_metric_variant_id() where the library is, so it can avoid ids that a
    recipe in ANOTHER family already uses. run() calls this once; the id -> family index is
    built lazily (from file names only, no file reads) the first time it's needed."""
    global _library_root, _id_families
    _library_root = out_dir
    _id_families = None


def _ids_by_family() -> dict[int, set[str]]:
    global _id_families
    if _id_families is None:
        _id_families = {}
        families_root = _library_root / FAMILIES_DIRNAME
        if families_root.is_dir():
            for fdir in families_root.iterdir():
                if not fdir.is_dir():
                    continue
                for path in fdir.glob("*_*.json"):
                    prefix = path.name.split("_", 1)[0]
                    if prefix.isdigit():
                        _id_families.setdefault(int(prefix), set()).add(fdir.name)
    return _id_families


def synthetic_metric_variant_id(legacy_id: int, parent_id: int | None = None) -> int:
    """Deterministic id for the Metric sibling Albertsons doesn't itself provide --
    unlike Mealime's own scheme, where every combo has its own independently issued id,
    Albertsons only ever gives us ONE real legacyId per fetch. legacy_id * 10 + 1 keeps
    this stable across re-runs (so already_fetched()-style re-runs stay idempotent) and
    outside the real id range (Albertsons' own legacyIds don't carry a synthetic marker
    digit like this), without needing a separate id registry.

    One exception: for a gap-fill recipe (Mealime-range legacy id) legacy_id * 10 + 1 can land
    on a genuine Mealime variant id in a DIFFERENT family (e.g. 1071 -> 10711, which Mealime
    already uses for another recipe). Two files with one id break anything keyed by id --
    exports, the cookbook-slug sync -- so in that case COLLISION_ID_OFFSET is added. A recipe's
    own existing Metric file is in its own family, so it never counts as a collision and
    re-fetching a non-colliding recipe keeps its id."""
    candidate = legacy_id * 10 + 1
    if candidate >= MEALIME_ID_CEILING or _library_root is None:
        return candidate
    families = _ids_by_family()
    while families.get(candidate, set()) - {str(parent_id)}:
        candidate += COLLISION_ID_OFFSET
    return candidate


def mealime_entry_for(fdir: Path, variant_id: int, name: str) -> dict | None:
    """Mealime's own record of this exact variant, from the family's alt_variants.json (written by
    mealime_json_scrape.py for families scraped through the marketing site -- the authenticated-backfill
    path has no such file). A gap-fill recipe is a real Mealime variant the public catalog didn't include,
    and its legacy id IS that variant's Mealime id, so the entry is Mealime's authoritative
    variety_tag_ids / allowed_type_ids / violated_restriction_ids for it. Returns None when there's no file,
    no entry with this id, or the entry's name doesn't match (then it isn't the same recipe)."""
    path = fdir / "alt_variants.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(entries, list):
        return None
    entry = next((e for e in entries if isinstance(e, dict) and e.get("id") == variant_id), None)
    if entry is None or (entry.get("name") or "").strip().lower() != (name or "").strip().lower():
        return None
    return entry


def mealime_twin_entry(fdir: Path, entry: dict) -> dict | None:
    """Mealime's Metric entry (unit_family_id 1) for the same dish -- group_label + name -- and serving
    size as this US entry: the real Mealime variant behind a gap-fill recipe's Metric twin, which the
    fetch otherwise gives a made-up id. Its `id` is the twin's real Mealime id and its `position` its
    real position. None if there's no alt_variants.json or no such entry."""
    try:
        entries = json.loads((fdir / "alt_variants.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = (entry.get("name") or "").strip().lower()
    return next((e for e in entries if isinstance(e, dict) and e.get("unit_family_id") == 1
                 and e.get("group_label") == entry.get("group_label")
                 and (e.get("name") or "").strip().lower() == name
                 and e.get("serving_count") == entry.get("serving_count")), None)


def mealime_position_for(fdir: Path, entry: dict, units: str) -> int | None:
    """Mealime's own `position` for the variant of this dish at these units. `position` orders a
    family's variants in blocks (all of the first dish's variants, then the next dish's, ...), so the
    dish that comes first is the family's original -- which is what the --menu/--avoid export rule ranks
    on. The US variant is the entry itself; the Metric twin's comes from mealime_twin_entry(). None if
    that entry isn't there."""
    if units == "US":
        return entry.get("position")
    twin = mealime_twin_entry(fdir, entry)
    return twin.get("position") if twin else None


def merge_mealime_entry(variant: dict, entry: dict) -> tuple[list[int], bool]:
    """Applies Mealime's entry to a gap-fill variant record, in place: variety_tag_ids gains any id the
    entry has and the record lacks (existing ids are never removed or reordered; a sorted list stays
    sorted), and violated_restriction_ids is REPLACED by Mealime's list when it differs as a set --
    Mealime's own classification of this exact variant beats anything inferred here (from the sweep or
    from ingredients). Returns (variety ids added, whether the restrictions changed)."""
    existing = list(variant.get("variety_tag_ids") or [])
    added = [i for i in (entry.get("variety_tag_ids") or []) if i not in existing]
    if added:
        merged = existing + added
        variant["variety_tag_ids"] = sorted(merged) if existing == sorted(existing) else merged
    truth = entry.get("violated_restriction_ids")
    current = variant.get("violated_restriction_ids")   # None = unknown
    restrictions_changed = truth is not None and (current is None or sorted(truth) != sorted(current))
    if restrictions_changed:
        variant["violated_restriction_ids"] = list(truth)
    return added, restrictions_changed


class NotAlbertsonsNative(Exception):
    """Raised by build_variants() when a raw fetch's parentId != legacyId -- meaning
    it's NOT actually an Albertsons-native recipe, but a normal Mealime-catalog recipe
    that happens to share a family (parentId) with real, already-scraped Mealime data.
    This project established early on that Albertsons reuses Mealime's own numbering
    scheme for non-native recipes -- self-referencing (parentId == legacyId) is what
    makes a recipe genuinely Albertsons-only. Writing variants for a non-native id would
    create synthetic, nutrition-less, degraded-schema siblings alongside (or conflicting
    with) the real Mealime-authored family -- confirmed to have already happened for 242
    recipes across 167 families before this gate existed (cleaned up via a one-time
    deletion pass; see conversation/session notes). These ids should be fetched via
    mealime_json_scrape.py's own API instead, which returns the real, authored recipe
    data rather than this module's approximation."""


def build_variants(data: dict, record: dict, parent_id: int) -> list[dict]:
    """Expands one Albertsons v2/recipe fetch into its US and Metric forms, at the ONE
    serving count Albertsons actually returned -- see the module docstring's UNIT-ONLY
    EXPANSION section for why serving-size synthesis was removed. Raises
    NotAlbertsonsNative if this recipe isn't actually Albertsons-native (see that
    exception's docstring)."""
    legacy_id = data["legacyId"]
    if data.get("parentId") != legacy_id:
        raise NotAlbertsonsNative(
            f"legacyId {legacy_id} has parentId {data.get('parentId')!r} -- not "
            f"self-referencing, so this belongs to a real Mealime family, not an "
            f"Albertsons-native one. Fetch it via mealime_json_scrape.py instead."
        )

    servings = (data.get("yield") or {}).get("servings") or 4
    allowed_type_ids = diets_to_allowed_type_ids(data.get("diets") or [])
    violated_restriction_ids = record.get("violated_restriction_ids")  # None = unknown, not fabricated
    rating_count = data.get("hyvorCount")
    cooking_minutes = data.get("cookingMinutes")
    # Albertsons' own v2/recipe response never includes variety_tag_ids at all (unlike
    # genuine Mealime data) -- backfilled from which cookbook(s) the sweep found this
    # recipe under instead. See mealime_id_reference.VARIETY_TAGS for what's verified.
    cookbook_slugs = record.get("cookbook_slugs") or []
    variety_tag_ids = variety_tag_ids_from_cookbook_slugs(cookbook_slugs)
    kid_friendly = is_kid_friendly(cookbook_slugs)

    variants = []
    for position, units in enumerate(UNIT_SYSTEMS):
        variant_id = legacy_id if units == "US" else synthetic_metric_variant_id(legacy_id, parent_id)

        ingredients = build_ingredients_for_combo(data, 1.0, units)
        instructions = build_instructions_for_combo(data, ingredients, units)

        variants.append({
            "id": variant_id,
            "recipe_id": parent_id,
            "name": data["name"],
            "slug": data["slug"],
            "source_url": None,
            "canonical_variant_url": None,
            "serving_count": servings,
            "cooking_minutes": cooking_minutes,
            "units": units,
            "group_label": "Default",
            "position": position,
            "unit_family_id": 2 if units == "US" else 1,
            "is_pro": None,
            "average_rating": None,
            "rating_count": rating_count,
            "allowed_type_ids": allowed_type_ids,
            "violated_restriction_ids": violated_restriction_ids,
            "variety_tag_ids": variety_tag_ids,
            "cuisine_tag_ids": [],
            "recipe_category_id": None,
            "kid_friendly": kid_friendly,
            "cookbook_slugs": sorted(cookbook_slugs),
            "cookwares": [],
            "ingredients": ingredients,
            "instructions": instructions,
            "nutrition": {},
            "schema_metadata": None,
            "sibling_variant_ids": [],
            "family_dir": f"{FAMILIES_DIRNAME}/{parent_id}/",
            "source": "albertsons_native",
            "backfill_bonus_fields": None,
        })

    all_ids = [v["id"] for v in variants]
    for v in variants:
        v["sibling_variant_ids"] = list(all_ids)  # matches real data: includes self too

    return variants


def existing_sibling_ids(fdir: Path) -> list[int]:
    """Numeric ids of every recipe file already sitting in a family folder (genuine
    Mealime siblings, prior albertsons_native variants, etc.) -- used to give a
    gap-fill variant an accurate sibling_variant_ids without touching the other files
    themselves (editing already-correct real Mealime data is more invasive than it's
    worth for one extra cross-reference)."""
    ids = []
    if not fdir.is_dir():
        return ids
    for path in fdir.glob("*.json"):
        if path.name == "alt_variants.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), int):
            ids.append(record["id"])
    return ids


def existing_group_labels(fdir: Path) -> set[str]:
    """group_label values already used in a family folder -- a family can contain more
    than one genuinely distinct dish (confirmed on family 103, "Better Than Takeout
    Chicken Fried Rice": group_label "Classic" vs "Classic Gluten-Free", different
    ingredients, different violated_restriction_ids), and group_label -- not
    recipe_id/family alone -- is what distinguishes them. A gap-fill variant must not
    collide with an existing group here, or it would get silently merged into the wrong
    dish by anything (like library_diet_restriction_stats.py) that dedupes on
    (recipe_id, group_label)."""
    labels = set()
    if not fdir.is_dir():
        return labels
    for path in fdir.glob("*.json"):
        if path.name == "alt_variants.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(record, dict) and record.get("group_label"):
            labels.add(record["group_label"])
    return labels


def existing_true_sibling(fdir: Path, name: str) -> dict | None:
    """A genuinely Mealime-scraped sibling already in this family (source is None -- i.e. NOT written
    by this script) whose name matches (case-insensitively) the recipe about to be gap-filled: proof
    this exact dish is already known here, just under a different id/servings/units combo. When one
    exists, its own group_label and violated_restriction_ids are Mealime's real, authoritative data for
    this dish -- reused in build_single_native_gap_variant() instead of this script's own guesses
    (the "Albertsons: <slug>" placeholder label, and whatever the discovery sweep's restriction-toggling
    heuristic attributed to the specific legacy_id being fetched). That heuristic is confirmed unreliable
    here: on family 3777 ("Thai Cucumber Noodle Salad"), the 2/4/6-serving gap-fill siblings of ONE dish
    each got a DIFFERENT violated_restriction_ids from the sweep, and none of the three matched the real
    Mealime sibling already on file for the same dish -- diet/restriction status is a property of the
    dish, not the serving count, so they can't all be right, but the already-scraped sibling can. Returns
    None if there's no such sibling, or if more than one disagrees with another on these fields (don't
    guess which is right)."""
    if not fdir.is_dir():
        return None
    matches = []
    for path in fdir.glob("*.json"):
        if path.name == "alt_variants.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(record, dict) or record.get("source") is not None:
            continue  # only trust genuinely-scraped Mealime data, not another script-written guess
        if (record.get("name") or "").strip().lower() == name.strip().lower():
            matches.append(record)
    if not matches:
        return None
    first = matches[0]
    for other in matches[1:]:
        if (other.get("group_label") != first.get("group_label")
                or sorted(other.get("violated_restriction_ids") or [])
                != sorted(first.get("violated_restriction_ids") or [])):
            return None
    return first


def build_single_native_gap_variant(data: dict, record: dict, parent_id: int, fdir: Path,
                                     taken_group_labels: set[str] | None = None) -> list[dict]:
    """For a non-native recipe (NotAlbertsonsNative) that turns out to be a genuinely
    missing variant slot in an existing real Mealime family -- see the module
    docstring's NON-NATIVE GAP-FILL section -- builds the US+Metric pair for the ONE
    serving count Albertsons actually returned (same unit-only expansion build_variants()
    does; no serving-size synthesis here either, and never was -- this function's
    single-serving-count behavior is what build_variants() was changed to match, not
    the other way around). Reuses the exact same ingredient/instruction/diet-mapping
    logic build_variants() uses. Returns a list of 2 variant dicts (US, then Metric).

    group_label is, in order of preference: (1) an existing genuine Mealime sibling's own group_label,
    when this exact dish (by name) is already known in the family -- see existing_true_sibling(); (2)
    derived from Albertsons' own slug, with its per-request disambiguating numeric suffix stripped (see
    albertsons_library_diff.strip_disambiguating_suffix() -- Mealime's own slugs never carry one, and
    leaving it in would give each serving-count sibling of the SAME dish a different group_label here).
    Case (2) falls back to appending the legacyId itself if the slug-derived label is somehow still
    taken (see existing_group_labels()). violated_restriction_ids is similarly preferred from that same
    true sibling over the discovery sweep's own per-legacy_id guess -- see existing_true_sibling()'s
    docstring for why that guess is confirmed unreliable here."""
    legacy_id = data["legacyId"]
    clean_slug = strip_disambiguating_suffix(data["slug"])
    servings = (data.get("yield") or {}).get("servings")
    cooking_minutes = data.get("cookingMinutes")
    allowed_type_ids = diets_to_allowed_type_ids(data.get("diets") or [])
    rating_count = data.get("hyvorCount")
    cookbook_slugs = record.get("cookbook_slugs") or []
    variety_tag_ids = variety_tag_ids_from_cookbook_slugs(cookbook_slugs)
    kid_friendly = is_kid_friendly(cookbook_slugs)

    true_sibling = existing_true_sibling(fdir, data["name"])
    if true_sibling is not None:
        group_label = true_sibling.get("group_label")
        violated_restriction_ids = true_sibling.get("violated_restriction_ids")
    else:
        violated_restriction_ids = record.get("violated_restriction_ids")
        group_label = f"Albertsons: {clean_slug}"
        if taken_group_labels and group_label in taken_group_labels:
            group_label = f"Albertsons: {clean_slug} ({legacy_id})"

    variants = []
    for position, units in enumerate(UNIT_SYSTEMS):
        variant_id = legacy_id if units == "US" else synthetic_metric_variant_id(legacy_id, parent_id)
        ingredients = build_ingredients_for_combo(data, 1.0, units)
        instructions = build_instructions_for_combo(data, ingredients, units)

        variants.append({
            "id": variant_id,
            "recipe_id": parent_id,
            "name": data["name"],
            "slug": clean_slug,
            "source_url": None,
            "canonical_variant_url": None,
            "serving_count": servings,
            "cooking_minutes": cooking_minutes,
            "units": units,
            "group_label": group_label,
            "position": position,
            "unit_family_id": 2 if units == "US" else 1,
            "is_pro": None,
            "average_rating": None,
            "rating_count": rating_count,
            "allowed_type_ids": allowed_type_ids,
            "violated_restriction_ids": violated_restriction_ids,
            "variety_tag_ids": variety_tag_ids,
            "cuisine_tag_ids": [],
            "recipe_category_id": None,
            "kid_friendly": kid_friendly,
            "cookbook_slugs": sorted(cookbook_slugs),
            "cookwares": [],
            "ingredients": ingredients,
            "instructions": instructions,
            "nutrition": {},
            "schema_metadata": None,
            "sibling_variant_ids": [],  # filled in by the caller, which can see the family folder
            "family_dir": f"{FAMILIES_DIRNAME}/{parent_id}/",
            "source": "albertsons_native_gap_fill",
            "backfill_bonus_fields": None,
        })

    return variants


def save_thumbnail(session: requests.Session, fdir: Path, data: dict) -> None:
    """Mirrors mealime_json_scrape.py's save_family_data() thumbnail handling: one
    thumbnail.<ext> per family folder, skipped if a sibling variant already saved one
    (this run or a previous one)."""
    images = (data.get("media") or {}).get("images") or []
    image_url = images[0].get("thumbnail") if images else None
    if not image_url:
        return
    ext = Path(urlparse(image_url).path).suffix or ".jpg"
    image_path = fdir / f"thumbnail{ext}"
    if image_path.exists():
        return
    try:
        resp = session.get(image_url, timeout=20)
        resp.raise_for_status()
        image_path.write_bytes(resp.content)
    except requests.RequestException:
        pass  # images are a nice-to-have; don't fail the whole recipe over one bad download


ALBERTSONS_OWN_SOURCES = {"albertsons_native", "albertsons_native_gap_fill"}


def existing_files_for(out_dir: Path, parent_id: int, legacy_id: int) -> list[Path]:
    """Matches the library layout's <variant_id>_<slug>.json naming (same convention
    albertsons_library_diff.py reads), returned as paths (not just a bool) so
    --overwrite can inspect and replace them."""
    fdir = family_dir(out_dir, parent_id)
    return sorted(fdir.glob(f"{legacy_id}_*.json"))


def already_fetched(out_dir: Path, parent_id: int, legacy_id: int) -> bool:
    """So writing straight into an existing library -- e.g. free-recipe-database --
    and re-running on the same ids list is a safe no-op, the same way
    mealime_json_scrape.py already skips ids it's already saved."""
    return bool(existing_files_for(out_dir, parent_id, legacy_id))


def file_source(path: Path) -> str | None:
    """The "source" field of an already-saved recipe file, or None if it's missing or
    unreadable -- used by --overwrite to confirm a file it's about to replace is one
    THIS script wrote, never a genuinely-scraped Mealime file that happens to collide
    on id (an unreadable file is treated the same as a foreign source: refuse, don't
    guess)."""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("source")
    except (json.JSONDecodeError, OSError):
        return None


def run(ids_json: Path, out_dir: Path, delay: float, only_legacy_id_over: int | None,
        overwrite: bool = False) -> None:
    if not any(HEADERS.values()):
        raise SystemExit("No session headers -- albertsons_headers.json is missing or empty. Capture "
                          "them from DevTools and load them with set_headers.py (see albertsons_auth.py).")

    set_library_root(out_dir)
    targets = load_targets(ids_json, only_legacy_id_over)
    print(f"Fetching {len(targets)} recipes...")

    session = requests.Session()
    session.headers.update(HEADERS)

    saved, skipped, failed, gap_filled = 0, 0, 0, 0
    for i, record in enumerate(targets, start=1):
        existing = existing_files_for(out_dir, record["parent_id"], record["legacy_id"])
        if existing and not overwrite:
            print(f"[{i}/{len(targets)}] {record['name']} ({record['id']}) -> already have it, skipping")
            skipped += 1
            continue

        if existing:
            foreign = [f for f in existing if file_source(f) not in ALBERTSONS_OWN_SOURCES]
            if foreign:
                print(f"[{i}/{len(targets)}] {record['name']} ({record['id']}) -> refusing to overwrite: "
                      f"{[f.name for f in foreign]} not written by this script (different/missing source) "
                      f"-- fix manually if this is really meant to be replaced")
                failed += 1
                continue
            print(f"[{i}/{len(targets)}] {record['name']} ({record['id']}) -> already have it, re-fetching to overwrite")
        else:
            print(f"[{i}/{len(targets)}] {record['name']} ({record['id']})")

        try:
            data = fetch_one(session, record["id"])
        except requests.RequestException as exc:
            print(f"    -> error: {exc}")
            failed += 1
            continue

        if data is None:
            failed += 1
            continue

        # Trust the fetched detail's own parentId over the discovery-time thumb's --
        # some thumbs (e.g. single-variant sponsored recipes) omit parentId entirely,
        # which would otherwise land the file under a literal "None" folder.
        parent_id = data.get("parentId", record["parent_id"])
        fdir = family_dir(out_dir, parent_id)
        fdir.mkdir(parents=True, exist_ok=True)

        if existing:
            # Delete before rebuilding (not after writing the new file(s) alongside) --
            # both so a slug change can't leave an orphaned duplicate, and so
            # existing_group_labels()/existing_sibling_ids() below don't see this
            # recipe's own OLD entry and mistake it for a real collision.
            for f in existing:
                f.unlink()

        try:
            variants = build_variants(data, record, parent_id)
        except NotAlbertsonsNative as exc:
            print(f"    -> {exc}")
            print(f"    -> filling just this one missing variant slot instead (real legacyId {record['legacy_id']})")
            gap_variants = build_single_native_gap_variant(data, record, parent_id, fdir, existing_group_labels(fdir))
            entry = mealime_entry_for(fdir, record["legacy_id"], data["name"])
            if entry is not None:
                true_label = (entry.get("group_label") or "").strip()
                for gap_variant in gap_variants:
                    added, restrictions_changed = merge_mealime_entry(gap_variant, entry)
                    if true_label:
                        # Mealime's own label for this variant, so serving sizes of one dish share one
                        # dish instead of each carrying its own "Albertsons: <slug>" placeholder.
                        gap_variant["group_label"] = true_label
                    real_position = mealime_position_for(fdir, entry, gap_variant["units"])
                    if real_position is not None:
                        gap_variant["position"] = real_position   # was the US/Metric index 0/1
                    if gap_variant["units"] != "US":
                        # The Metric twin's REAL Mealime id, not the made-up legacy_id * 10 + 1. Only a file that
                        # ISN'T ours (a genuine Mealime file with that id) blocks it; a gap-fill file with the
                        # id is this same twin from an earlier fetch and simply gets replaced.
                        twin = mealime_twin_entry(fdir, entry)
                        if twin is not None and twin.get("id") is not None and not any(
                                file_source(f) not in ALBERTSONS_OWN_SOURCES for f in fdir.glob(f"{twin['id']}_*.json")):
                            gap_variant["id"] = twin["id"]
                print(f"    -> Mealime's own entry found in this family's alt_variants.json: variety tags "
                      f"+{added or 'none'}, restrictions {'replaced' if restrictions_changed else 'already match'}")
            sibling_ids = sorted(set(existing_sibling_ids(fdir)) | {v["id"] for v in gap_variants})
            for v in gap_variants:
                v["sibling_variant_ids"] = sibling_ids
            save_thumbnail(session, fdir, data)
            for v in gap_variants:
                out_path = fdir / f"{v['id']}_{v['slug']}.json"
                out_path.write_text(json.dumps(v, indent=2, ensure_ascii=False), encoding="utf-8")
            gap_filled += 1
            time.sleep(delay)
            continue

        save_thumbnail(session, fdir, data)
        for variant in variants:
            out_path = fdir / f"{variant['id']}_{variant['slug']}.json"
            out_path.write_text(json.dumps(variant, indent=2, ensure_ascii=False), encoding="utf-8")
        saved += 1
        time.sleep(delay)

    print(f"\nDone. Saved {saved} native recipe(s) (US+Metric each), filled {gap_filled} missing "
          f"variant slot(s) in existing families (US+Metric each), skipped (already had) {skipped}, "
          f"failed {failed}.")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids-json", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--only-legacy-id-over", type=int, default=None,
                        help="Only fetch records whose legacy_id is above this threshold "
                             "(e.g. 100000, to target just the Albertsons-native ones).")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-fetch and replace ids you already have instead of skipping them "
                             "(e.g. to pick up a fix to this script in recipes fetched before it "
                             "existed). Refuses to touch any existing file that wasn't itself "
                             "written by this script -- see the module docstring's --OVERWRITE "
                             "section.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.ids_json, args.out_dir, args.delay, args.only_legacy_id_over, args.overwrite)
