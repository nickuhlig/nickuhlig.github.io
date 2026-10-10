"""
Converts scraped recipe JSON (the scraper's output) into formats usable by
third-party recipe software: an HTML page with embedded schema.org Recipe JSON-LD,
Open Recipe Format (ORF) YAML, Paprika's own .paprikarecipe(s) format, and PDF
(rendered from the same HTML via a headless browser). Keeps the families/<id>/
folder structure, same as the library-subset filter script.

FORMAT NOTES (what will actually import where):
  json    -- the original scraped recipe JSON, copied byte-for-byte (not
             re-serialized) into the same families/<id>/ output structure as every
             other format. For archival, or feeding straight back into your own
             scripts -- no recipe software imports this shape directly.
  html    -- a human-readable recipe page with a <script type="application/ld+json">
             schema.org Recipe object embedded. This is the format most likely to
             actually import cleanly wherever you're headed -- Paprika, Mealie,
             Tandoor, Nextcloud Cookbook, Plan to Eat, etc. all have an "import from
             URL/webpage" clipper that looks for exactly this markup.
  orf     -- Open Recipe Format YAML (spec: open-recipe-format.readthedocs.io). Built
             to be spec-compliant, but as of 2026 this format has essentially no
             adoption in mainstream recipe software (a single hobbyist project) --
             treat it as a well-structured, human-readable/scriptable archive format,
             not a reliable import path.
  paprika -- Paprika's own .paprikarecipe (gzip-compressed JSON) / .paprikarecipes
             (a zip of .paprikarecipe entries, stored uncompressed since the entries
             are already gzipped) format. Tandoor and several other apps import this
             directly. Both an individual file per recipe (alongside the other
             formats in its family folder) and one combined library.paprikarecipes
             at the top of --out-dir (for a single one-shot import) are written.
             The family thumbnail (if any) is embedded as base64, matching a real
             Paprika export's photo/photo_large/photo_hash/photos[] fields -- see
             build_paprika's docstring for exactly what was reverse-engineered vs.
             substituted.
  pdf     -- same layout as the HTML page, printed to PDF via a headless browser
             (Playwright, already a dependency of this project's other scrapers).
  norish  -- Norish's Recipe Archive format: a loose recipe.json per recipe (in its
             family folder, alongside the other formats -- for inspection, or
             feeding your own tooling) AND one combined
             norish-recipes-<date>.norishrecipes zip at the top of --out-dir (a
             root manifest.json plus one <id>/recipe.json folder per recipe, with
             its thumbnail packed into <id>/images/ -- the shape Norish's own
             importer expects, reverse-engineered from norish-recipes/norish's
             archive writer and test fixtures). Reconstructs step-ingredient chips
             from each step's secondary_message, matching Mealime's callout lines
             back to ingredients by name and computing each chip's share of that
             ingredient's total quantity (98%+ match rate confirmed across this
             library). NOTE: Norish scales servings and unit systems live from one
             stored recipe -- unlike every other format here, bundling more than
             one serving/unit variant of the same family creates near-duplicate
             recipes in Norish rather than one that scales. Use --units/--servings
             to pick a single variant per family when norish is one of --formats;
             run() warns if it detects otherwise.

Ingredient quantities are free text from the source data (e.g. "6 fl oz", "\u00bd (15 oz) can")
and aren't split into numeric amount + unit anywhere in the source data. Paprika and
the HTML/PDF page just use the quantity text as-is (a display string is all they
need). ORF requires a numeric amount + a unit per ingredient, so this parses a
leading number (including unicode fraction characters like \u00bd) off the front of
each quantity string on a best-effort basis; anything that doesn't start with a
recognizable number is stored as amount 1 with the full original text as the unit,
rather than silently dropped.

Images: the locally-saved thumbnail (if the family has one) is copied alongside the
recipe and referenced by relative path in the HTML/PDF. Paprika embeds it directly as
a base64 blob (its own external image_url field is left null -- the source's external
thumbnail URL isn't retained anywhere in the scraped JSON, only the downloaded file
is). ORF has no image field at all.

--units/--servings filter to just one units system (metric/US) and/or one serving
size (2/4/6) before converting -- the same filter the library-subset script applies,
so you can convert straight from the full library without running that script first.
Omit either one to convert every units/servings variant found.

--servings FALLBACK for single-option recipes: many Albertsons-native recipes only ever
have ONE serving count at all (Albertsons gives us whatever it rendered, no serving-size
synthesis -- see fetch_albertsons_native_recipes.py's module docstring), so a strict
--servings filter would silently drop them entirely rather than export the one option
they actually have. Done inside select_menu_winners() (see its docstring), scoped per
(recipe_id, units) -- every serving count a family offers under that units system,
together -- rather than per exact dish name: an exact match ANYWHERE in that group always
wins outright, and the fallback (one answer regardless of serving count) only fires when
the family truly has nothing at the requested count at all. Scoping it any narrower (e.g.
by group_label+name, as an earlier version of this fallback did) lets one single-option
dish's fallback fire even when a DIFFERENT, equally-valid dish in the same family already
satisfies the request -- confirmed happening on family 100, which has two real "Classic"
recipes (one with kale, one without) each locked to a different single serving count.
--units filtering is unaffected by this (Albertsons recipes always come as a US+Metric
pair per serving count, so there's no equivalent "only one units option" case to fall
back for).

--menu selects the ONE recipe per family that best matches, for every (units, serving_count)
combination that family offers -- never several diet-swap siblings at once (Classic, Vegan,
and a gluten-free version of the same dish all going out together). --menu is NOT optional:
Classic (the default) is the floor -- there is no "export every variant, uncollapsed" mode any
more. Someone who wants several menus exports them in separate runs.

Confirmed in this library: group_label alone cannot be trusted as a dish identity key --
plenty of families reuse "Default" (or no label at all) across genuinely DIFFERENT recipes.
The clearest case found: family 1184's "Default" label covers both a vegan almond-sauce
burrito bowl AND a real-cheddar version -- same name, same label, different ingredients.
A library-wide check found 315 such collisions (6.2% of all dishes, spanning 238 families,
2,494 files) -- so selection below never groups by group_label+name first; it only ever
uses group_label as a tie-break PREFERENCE (step 3), never as a hard grouping key.

SELECTION, per (recipe_id, units, serving_count) slot found anywhere in the library:
  1. RESTRICTIONS -- drop any candidate that violates one of --avoid's restrictions, or whose
     restrictions are unknown (conservative -- allergy safety; nothing in the library is
     unknown as of this writing).
  2. MENU TYPE -- keep only candidates whose allowed_type_ids includes --menu's type id
     (MENU_NAME_TO_TYPE_ID -- Classic requires type id 1 here too, the same as every other
     menu; there's no special-cased "no filter" case any more). A slot with no candidate
     satisfying this is simply absent from the result -- nothing exports for it.
  3. LABEL MATCH -- if more than one candidate remains, prefer one whose group_label
     keyword-matches --menu's name (e.g. prefer a "Vegan"-labelled file over a "Default" one
     when --menu vegan). Backed by a confirmed 100% correlation in this library: every file
     whose group_label names a diet has that diet correctly reflected in its own
     allowed_type_ids (340 checked, 0 mismatches) -- so trusting an explicit label here is
     sound whenever one exists to trust.
  4. POSITION -- if still tied, prefer the lowest `position` (Mealime's own catalog-add
     order -- ~99% reliable at identifying the true/closest-to-original recipe when several
     genuinely different, well-labeled siblings remain tied on type and label).
  5. ID -- final tiebreak, lowest variant id (always unique, so this always resolves to
     exactly one file). This is what actually resolves the 315 collisions above: with no
     distinguishing label to prefer in step 3, id is what decides -- not a judgment about
     which copy's content is "right".

--units narrows WHICH units-system slots get exported, applied as a simple independent filter
after selection (no fallback needed -- see --units/--servings above). --servings is different:
it's passed straight into select_menu_winners() (the FALLBACK section above), since deciding
whether the requested count is genuinely unavailable needs the same per-family view selection
itself already has -- doing it as a separate post-filter step is exactly what caused the family
100 bug this design fixes. Use --dry-run to see the resulting counts (including how many
families end up with no qualifying recipe) before writing anything.

Usage:
    python recipe_export.py --out-dir "C:\\path\\to\\export"
    python recipe_export.py --out-dir "C:\\path\\to\\export" --formats html orf
    python recipe_export.py --out-dir "C:\\path\\to\\export" --units US --servings 4
    python recipe_export.py --recipes-dir "C:\\path\\to\\a_subset" --out-dir "C:\\path\\to\\export" --formats paprika

    (--menu defaults to classic; add --avoid gluten dairy --dry-run to preview a filtered export first)

Setup (one-time):
    pip install pyyaml
    pip install playwright   (only needed for the pdf format)
    playwright install chromium
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

import yaml

from mealime_id_reference import MENU_TYPES, RESTRICTIONS, VARIETY_TAGS, decode_restrictions, decode_types
from mealime_library_subset import normalize_units

from library_paths import DEFAULT_LIBRARY_DIR as DEFAULT_RECIPES_DIR
FAMILIES_DIRNAME = "families"
NON_RECIPE_FILENAMES = {"alt_variants.json", "authenticated_index.json", "ingredient_index.json",
                        "ingredient_master.json", "manifest.json"}
PAPRIKA_BUNDLE_FILENAME = "library.paprikarecipes"
ALL_FORMATS = ("json", "html", "orf", "paprika", "pdf", "norish")

# Internal pipeline provenance -- which scraper/fetch path produced this file, its
# original site URL, etc. -- that the live library keeps (useful for debugging, e.g.
# it's what made the Albertsons-contamination cleanup possible) but an exported copy
# doesn't need: whoever receives the export doesn't care how it got here, and it isn't
# meaningful outside this project anyway. Every other export format (html/orf/paprika/
# pdf/norish) already naturally excludes these -- they're built from an explicit field
# list that never included them -- only the "json" format did a raw byte-for-byte copy,
# so this only needs to apply there. Values are nulled rather than the keys deleted, to
# keep the exported shape consistent with what unset fields already look like elsewhere.
EXPORT_CLEARED_FIELDS = {
    "source": None,
    "source_url": None,
    "canonical_variant_url": None,
    "schema_metadata": None,
}

# Norish tags beyond the variety_tag_ids ones (VARIETY_TAGS) -- see build_norish_recipe(). Norish cookbooks can't be created by an archive (the format
# carries no cookbook data), so these become tags, and cookbooks are then bulk-filled in
# Norish by filtering on them.
#
# Mealime menu types (allowed_type_ids -> MENU_TYPES) a recipe satisfies. Classic is
# every recipe and Flexitarian nearly so -- neither says anything, so both are left out.
MENU_TYPE_TAG_IDS = (2, 4, 5, 6, 7, 8)  # Vegetarian, Pescatarian, Paleo, Vegan, Low Carb, Keto

# Albertsons cookbooks are NOT handled here any more: albertsons_cookbook_slugs_sync.py turns each
# cookbook into a variety_tag_id (Mealime's own where one exists, our ids 35-43 for dessert,
# breakfast, snacks, ...), so they export through VARIETY_TAGS like every other variety tag. Run that
# sync before exporting -- a recipe whose cookbook_slugs haven't been synced won't get those tags.


def restriction_free_tag_names(record: dict) -> list[str]:
    """"<Restriction>-Free" tag names for every restriction id NOT in violated_restriction_ids --
    e.g. a recipe with violated_restriction_ids [1, 11] gets "Dairy-Free", "Fish-Free", ... for the
    other ten restrictions, but never "Gluten-Free" or "Egg-Free". None (unknown -- restrictions
    never evaluated for this recipe) yields NO tags at all: asserting "-Free" on a recipe whose
    restrictions were never checked would be a false claim, not an absence of the allergen -- same
    reasoning as build_suitable_for_diet's GlutenFreeDiet handling above."""
    violated = record.get("violated_restriction_ids")
    if not isinstance(violated, list):
        return []
    violated_ids = set(violated)
    return [f"{name}-Free" for restriction_id, name in RESTRICTIONS.items() if restriction_id not in violated_ids]


def export_tag_names(record: dict) -> list[str]:
    """The tag/category set shared by the Norish (tags) and Paprika (categories) exporters, so both
    formats offer the end user the same filtering: variety_tag_ids (VARIETY_TAGS -- Mealime's own
    dish-type tags plus our Albertsons-only additions 35-43), the menu types worth tagging
    (MENU_TYPE_TAG_IDS), and a "<Restriction>-Free" tag for every restriction the recipe doesn't
    violate (restriction_free_tag_names). Deduplicated, order preserved."""
    names = [VARIETY_TAGS[v] for v in (record.get("variety_tag_ids") or []) if v in VARIETY_TAGS]
    names += [MENU_TYPES[t] for t in MENU_TYPE_TAG_IDS if t in (record.get("allowed_type_ids") or [])]
    names += restriction_free_tag_names(record)
    return list(dict.fromkeys(names))

# --menu name -> allowed_type_ids id, for select_menu_winners(). Flexitarian is deliberately absent
# as a --menu choice -- it's present on nearly every dish, so it wouldn't discriminate anything (same
# reasoning as its exclusion from MENU_TYPE_TAG_IDS). Classic requires type id 1 like every other
# choice here -- no special-cased "no filter" case; ~88% of dishes carry it, and the position/id
# tie-break steps still resolve the rest, same as any other menu with a thin candidate pool.
MENU_NAME_TO_TYPE_ID = {
    "classic": 1, "vegetarian": 2, "pescatarian": 4, "paleo": 5, "vegan": 6, "low-carb": 7, "keto": 8,
}
MENU_CHOICES = tuple(MENU_NAME_TO_TYPE_ID)

# --avoid name -> violated_restriction_ids id, for select_menu_winners(). Matches
# mealime_id_reference.RESTRICTIONS' 12 ids exactly, spelled as CLI-friendly kebab-case.
AVOID_NAME_TO_RESTRICTION_ID = {
    "gluten": 1, "dairy": 2, "fish": 3, "shellfish": 4, "peanut": 5, "tree-nut": 6,
    "soy": 9, "nightshade": 10, "egg": 11, "sesame": 12, "mustard": 13, "sulfite": 14,
}
AVOID_CHOICES = tuple(AVOID_NAME_TO_RESTRICTION_ID)

# A fixed, dedicated namespace for this project's own deterministic uid generation
# (RFC 4122 -- a fresh random UUID minted once and hardcoded, rather than reusing a
# standard namespace like NAMESPACE_URL, to avoid colliding with some other tool's
# uuid5 output for the same string). Re-exporting the same recipe id always yields
# the same Paprika uid, so re-importing an updated export overwrites rather than
# duplicates in apps that dedupe on it.
PAPRIKA_UUID_NAMESPACE = uuid.UUID("d38f3b0a-6e0a-4b0d-9c8f-9e6f1e6e6c9a")

NORISH_SYSTEM_USED = {"US": "us", "Metric": "metric"}
NORISH_ARCHIVE_FORMAT = "norish-recipes"
NORISH_ARCHIVE_FORMAT_VERSION = 1
NORISH_DEFAULT_EXPORTER_ORIGIN = "https://recipe-export.invalid"

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

FRACTION_MAP = {
    "\u00bc": 0.25, "\u00bd": 0.5, "\u00be": 0.75,
    "\u2153": 1 / 3, "\u2154": 2 / 3,
    "\u2155": 0.2, "\u2156": 0.4, "\u2157": 0.6, "\u2158": 0.8,
    "\u2159": 1 / 6, "\u215a": 5 / 6,
    "\u215b": 0.125, "\u215c": 0.375, "\u215d": 0.625, "\u215e": 0.875,
}
LEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(.*)$", re.DOTALL)


def sanitize_filename(name: str) -> str:
    name = INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name or "untitled")[:150]


def find_recipe_files(recipes_dir: Path):
    for path in recipes_dir.rglob("*.json"):
        if path.name in NON_RECIPE_FILENAMES:
            continue
        yield path


def label_matches_menu(group_label: str | None, menu: str) -> bool:
    """Case-insensitive keyword match between a file's own group_label and the selected --menu
    name -- select_menu_winners()'s step 3 tie-break. A plain substring check for every menu
    except "low-carb", which needs both real spellings seen in this library ("Low-Carb/Paleo"
    and "Low Carb / Paleo" both have to match)."""
    if not group_label:
        return False
    low = group_label.lower()
    if menu == "low-carb":
        return "low-carb" in low or "low carb" in low
    return menu in low


def _pick_winner(records: list[dict], menu: str) -> dict:
    """Steps 3-5 of select_menu_winners()'s hierarchy: `records` must already be restriction- and
    menu-type-filtered (steps 1-2). Prefers a group_label match (step 3), then lowest `position`
    (step 4), then lowest `id` as the final, always-unique tiebreak (step 5)."""
    pool = [r for r in records if label_matches_menu(r.get("group_label"), menu)] or records
    return min(pool, key=lambda r: (r["position"] if r.get("position") is not None else float("inf"), r.get("id")))


def select_menu_winners(recipes_dir: Path, menu: str, avoid: set[int],
                         servings: int | None = None) -> dict[tuple[int, str | None, int | None], int]:
    """Pre-pass (whole-library scan) deciding, for every (recipe_id, units, serving_count) slot,
    which single file wins under the given --menu/--avoid -- so a family with several diet-swap
    siblings of the same underlying recipe (Classic, Vegan, a gluten-free version, ...) never
    exports more than one, and a family where Mealime re-issued the same dish under a whole new
    id set (confirmed genuinely happening -- see the module docstring) never exports both copies.

    Deliberately does NOT group files by dish_key() (group_label + name) first the way the
    now-removed select_dish_winners()/select_variant_winners() pair did -- that grouping is
    exactly what let two genuinely different recipes sharing an unreliable label (confirmed on
    315 dishes library-wide, e.g. family 1184's "Default" covering both a vegan and a real-cheese
    burrito bowl) get silently merged, with the "winner" then chosen by id alone with no chance
    to prefer whichever copy was actually, explicitly labeled for the requested menu. Here,
    group_label is consulted only as a tie-break PREFERENCE (via _pick_winner(), step 3), never as
    a hard identity key -- see the module docstring's SELECTION section for the full rationale of
    each step (1. restrictions, 2. menu type, 3. label match, 4. position, 5. id).

    --servings FALLBACK, done HERE now rather than via a separate dish_key-based pass: candidates
    are first grouped by (recipe_id, units) ALONE (every serving count together), so a family's
    OWN sibling files can settle whether the requested serving count is genuinely unavailable
    anywhere in the family, not just on one arbitrarily-named dish within it. If `servings` is
    given and the group has a candidate at that exact count, ONLY those compete (steps 3-5 pick
    one). If NONE of the group matches, the whole group (every serving count) competes together
    for ONE overall winner instead, regardless of its serving count -- this is what makes a
    genuinely single-serving-count recipe still export instead of being silently dropped. This
    fixes a real bug the old dish_key-based fallback had: family 100 carries two DIFFERENT real
    recipes both labeled "Classic" -- a "with kale" version only ever offered at 4 servings, and a
    "without kale" version only ever offered at 6 -- and grouping by exact dish name let the old
    fallback rescue the 6-serving one even when --servings 4 was requested and the family already
    had a perfectly good, directly-matching 4-serving answer (the kale version). Grouping by
    (recipe_id, units) alone instead means an exact match anywhere in the family always wins
    outright, and the whole-group fallback only ever fires when the family truly has nothing at
    the requested count.

    Returns {(recipe_id, units, serving_count): winning_id} -- the third element is the winner's
    OWN actual serving_count, which only differs from a requested `servings` in the fallback case.
    A (recipe_id, units) group with no candidate surviving steps 1-2 at all contributes nothing --
    that family/units combo exports nothing under this menu/avoid."""
    print(f"Scanning library to select one recipe per (family, units) group for "
          f"menu={menu!r} avoid={sorted(avoid) or None}"
          + (f" servings={servings}" if servings is not None else "") + "...")
    menu_type_id = MENU_NAME_TO_TYPE_ID[menu]
    all_families: set[int] = set()
    grouped: dict[tuple[int, str | None], list[dict]] = defaultdict(list)
    scanned = 0
    for path in find_recipe_files(recipes_dir):
        scanned += 1
        if scanned % 2000 == 0:
            print(f"...{scanned} files scanned")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(record, dict) or "recipe_id" not in record or "name" not in record:
            continue
        all_families.add(record["recipe_id"])
        violated = record.get("violated_restriction_ids")
        if avoid and (not isinstance(violated, list) or set(violated) & avoid):
            continue
        if menu_type_id not in (record.get("allowed_type_ids") or []):
            continue
        grouped[(record["recipe_id"], record.get("units"))].append(record)
    print(f"Done scanning {scanned} file(s); {len(grouped)} (family, units) group(s) have a candidate.")

    winners: dict[tuple[int, str | None, int | None], int] = {}
    fallback_groups = 0
    for (recipe_id, units_val), records in grouped.items():
        if servings is not None:
            exact = [r for r in records if r.get("serving_count") == servings]
            if exact:
                winners[(recipe_id, units_val, servings)] = _pick_winner(exact, menu)["id"]
            else:
                winner = _pick_winner(records, menu)  # fallback: any serving count, one overall winner
                winners[(recipe_id, units_val, winner.get("serving_count"))] = winner["id"]
                fallback_groups += 1
        else:
            by_serving: dict[int | None, list[dict]] = defaultdict(list)
            for r in records:
                by_serving[r.get("serving_count")].append(r)
            for serving_count, recs in by_serving.items():
                winners[(recipe_id, units_val, serving_count)] = _pick_winner(recs, menu)["id"]

    families_with_winner = {slot[0] for slot in winners}
    print(f"{len(families_with_winner)} of {len(all_families)} families have a recipe satisfying "
          f"menu={menu!r} avoid={sorted(avoid) or None} (across {len(winners)} units/servings "
          f"slot(s), {fallback_groups} of them a --servings fallback); "
          f"{len(all_families) - len(families_with_winner)} family(ies) excluded entirely.")
    return winners


def _parse_leading_number(text: str) -> tuple[float, str] | None:
    """Strict core of parse_quantity's leading-number search: returns None when
    the text has no recognizable leading number, instead of parse_quantity's
    display-oriented fallback of amount=1/full-text-as-unit. That fallback is a
    sentinel meant for callers (ORF, Paprika) that just need *something* to show
    on screen -- it is never a real quantity, so anything that feeds a parsed
    number into arithmetic (e.g. a step-ingredient share ratio) must use this
    instead and treat None as genuinely unknown, not as 1."""
    text = (text or "").strip()
    if not text:
        return None

    for char, value in FRACTION_MAP.items():
        if text.startswith(char):
            return value, text[len(char):].strip()
        whole_match = re.match(rf"^(\d+)\s*{char}(.*)$", text, re.DOTALL)
        if whole_match:
            return int(whole_match.group(1)) + value, whole_match.group(2).strip()

    match = LEADING_NUMBER_RE.match(text)
    if match:
        return float(match.group(1)), match.group(2).strip()

    return None


def parse_quantity(quantity: str) -> tuple[float, str]:
    """Best-effort split of a free-text quantity like '6 fl oz' or '\u00bd (15 oz) can'
    into a numeric amount and the remaining unit text. Falls back to (1, quantity)
    when no leading number can be found (e.g. '', a pantry item with no amount) --
    a display sentinel for callers (ORF/Paprika) that just need something to show;
    never use this for real arithmetic -- see _parse_leading_number for that."""
    text = (quantity or "").strip()
    if not text:
        return 1, "(to taste / as needed)"

    parsed = _parse_leading_number(text)
    if parsed is not None:
        return parsed

    return 1, text


def ingredient_line(ing: dict) -> str:
    quantity = (ing.get("quantity") or "").strip()
    return f"{quantity} {ing['name']}".strip() if quantity else ing["name"]


def step_text_and_callouts(step: dict) -> tuple[str, list[str]]:
    primary = step["primary_message"].strip()
    secondary = (step.get("secondary_message") or "").strip()
    callouts = [line.strip() for line in secondary.split("\n") if line.strip()]
    return primary, callouts


NUTRITION_DISPLAY = (
    ("energy", "Calories", "", 0),
    ("fat", "Fat", "g", 1),
    ("saturated", "Saturated Fat", "g", 1),
    ("carbs", "Carbohydrates", "g", 1),
    ("fiber", "Fiber", "g", 1),
    ("sugars", "Sugars", "g", 1),
    ("protein", "Protein", "g", 1),
    ("sodium", "Sodium", "mg", 1),
    ("cholesterol", "Cholesterol", "mg", 1),
)


def nutrition_lines(nutrition: dict) -> list[str]:
    lines = []
    for key, label, unit, decimals in NUTRITION_DISPLAY:
        value = nutrition.get(key)
        if value is None:
            continue
        lines.append(f"{label}: {float(value):.{decimals}f}{' ' + unit if unit else ''}")
    return lines


# All 18 present in every recipe's nutrition block, confirmed 2026-09-11 across the
# full library regardless of source (marketing_site vs. authenticated_backfill).
AMINO_ACID_FIELDS = (
    ("alanine", "Alanine"), ("arginine", "Arginine"), ("aspartic_acid", "Aspartic Acid"),
    ("cystine", "Cystine"), ("glutamic_acid", "Glutamic Acid"), ("glycine", "Glycine"),
    ("histidine", "Histidine"), ("isoleucine", "Isoleucine"), ("leucine", "Leucine"),
    ("lysine", "Lysine"), ("methionine", "Methionine"), ("phenylalanine", "Phenylalanine"),
    ("proline", "Proline"), ("serine", "Serine"), ("threonine", "Threonine"),
    ("tryptophan", "Tryptophan"), ("tyrosine", "Tyrosine"), ("valine", "Valine"),
)


def amino_acid_lines(nutrition: dict) -> list[str]:
    lines = []
    for key, label in AMINO_ACID_FIELDS:
        value = nutrition.get(key)
        if value is None:
            continue
        lines.append(f"{label}: {float(value):.2f} g")
    return lines


# --- HTML + schema.org ---------------------------------------------------------
#
# schema.org's RestrictedDiet enumeration (the value type for suitableForDiet) has
# only 11 members as of 2026: DiabeticDiet, GlutenFreeDiet, HalalDiet, HinduDiet,
# KosherDiet, LowCalorieDiet, LowFatDiet, LowLactoseDiet, LowSaltDiet, VeganDiet,
# VegetarianDiet -- most of Mealime's own menu-type/restriction vocabulary has no
# matching member at all (no PaleoDiet, LowCarbDiet, KetoDiet, NutFreeDiet,
# ShellfishFreeDiet, etc.), so only an EXACT correspondence is mapped below rather
# than picking the "closest" enum member and overclaiming a guarantee the source
# data doesn't actually make. Notably: Dairy is deliberately NOT mapped to
# LowLactoseDiet -- a dairy allergy/restriction and lactose intolerance are
# different things, and Mealime's "violates Dairy" flag says nothing about lactose
# content specifically.
MENU_TYPE_TO_DIET = {
    "Vegetarian": "https://schema.org/VegetarianDiet",
    "Vegan": "https://schema.org/VeganDiet",
}
RESTRICTION_TO_FREE_DIET = {
    "Gluten": "https://schema.org/GlutenFreeDiet",
}


def build_suitable_for_diet(record: dict) -> list[str]:
    """schema.org/Recipe's suitableForDiet, mapped from Mealime's own decoded
    menu-type/restriction labels. allowed_type_ids is a direct positive match
    (this dish IS suitable for that menu). violated_restriction_ids is the
    opposite polarity -- it lists what the dish VIOLATES (i.e. contains), so
    the corresponding *-free diet is only asserted when that restriction is
    absent from the list, never when it's present. A None violated_restriction_ids
    means restrictions were never evaluated for this recipe at all (e.g. an
    Albertsons-native recipe fetched before any --restriction-discovery run tagged
    it) -- that's different from an evaluated, empty list, and must NOT assert any
    *-free diet: doing so would be a false "certified gluten-free"-style claim on
    an exported page, not an absence of the allergen."""
    diets: list[str] = []

    for menu_type in decode_types(record.get("allowed_type_ids") or []):
        diet = MENU_TYPE_TO_DIET.get(menu_type)
        if diet and diet not in diets:
            diets.append(diet)

    violated_ids = record.get("violated_restriction_ids")
    if violated_ids is not None:
        violated = set(decode_restrictions(violated_ids))
        for restriction, diet in RESTRICTION_TO_FREE_DIET.items():
            if restriction not in violated and diet not in diets:
                diets.append(diet)

    return diets


def build_schema_org(record: dict, image_src: str | None) -> dict:
    ingredients = [ingredient_line(ing) for ing in record["ingredients"]]

    steps = []
    for step in record["instructions"]:
        primary, callouts = step_text_and_callouts(step)
        text = f"{primary} ({'; '.join(callouts)})" if callouts else primary
        steps.append({"@type": "HowToStep", "text": text})

    nutrition = record.get("nutrition") or {}
    nutrition_info: dict = {"@type": "NutritionInformation"}
    if nutrition.get("energy") is not None:
        nutrition_info["calories"] = f"{nutrition['energy']:.0f} calories"
    if nutrition.get("fat") is not None:
        nutrition_info["fatContent"] = f"{nutrition['fat']:.1f} g"
    if nutrition.get("saturated") is not None:
        nutrition_info["saturatedFatContent"] = f"{nutrition['saturated']:.1f} g"
    if nutrition.get("carbs") is not None:
        nutrition_info["carbohydrateContent"] = f"{nutrition['carbs']:.1f} g"
    if nutrition.get("fiber") is not None:
        nutrition_info["fiberContent"] = f"{nutrition['fiber']:.1f} g"
    if nutrition.get("sugars") is not None:
        nutrition_info["sugarContent"] = f"{nutrition['sugars']:.1f} g"
    if nutrition.get("protein") is not None:
        nutrition_info["proteinContent"] = f"{nutrition['protein']:.1f} g"
    if nutrition.get("sodium") is not None:
        nutrition_info["sodiumContent"] = f"{nutrition['sodium']:.1f} mg"
    if nutrition.get("cholesterol") is not None:
        nutrition_info["cholesterolContent"] = f"{nutrition['cholesterol']:.1f} mg"

    # schema.org's NutritionInformation has no dedicated property per amino acid --
    # additionalProperty/PropertyValue is the standard, generic extension mechanism
    # for exactly this ("extra data with no purpose-built property"), rather than
    # inventing non-standard keys directly on the object.
    amino_props = [
        {"@type": "PropertyValue", "name": label, "value": f"{nutrition[key]:.2f} g"}
        for key, label in AMINO_ACID_FIELDS if nutrition.get(key) is not None
    ]
    if amino_props:
        nutrition_info["additionalProperty"] = amino_props

    schema: dict = {
        "@context": "https://schema.org/",
        "@type": "Recipe",
        "name": record["name"],
        "recipeYield": f"{record['serving_count']} servings",
        "recipeIngredient": ingredients,
        "recipeInstructions": steps,
    }
    if record.get("cooking_minutes"):
        schema["totalTime"] = f"PT{int(record['cooking_minutes'])}M"
    if image_src:
        schema["image"] = [image_src]
    meta = record.get("schema_metadata") or {}
    if meta.get("category"):
        schema["recipeCategory"] = meta["category"]
    if meta.get("cuisine"):
        schema["recipeCuisine"] = meta["cuisine"]
    if meta.get("keywords"):
        keywords = meta["keywords"]
        schema["keywords"] = ", ".join(keywords) if isinstance(keywords, list) else keywords
    suitable_for_diet = build_suitable_for_diet(record)
    if suitable_for_diet:
        schema["suitableForDiet"] = suitable_for_diet
    if len(nutrition_info) > 1:
        schema["nutrition"] = nutrition_info
    return schema


def render_html(record: dict, image_src: str | None, show_amino_acids: bool = True) -> str:
    """image_src is used verbatim as the <img>/JSON-LD image reference -- a relative
    filename for a saved .html file (portable alongside its thumbnail), or an
    absolute file:// URI when rendering for PDF (see the pdf branch in run()).

    show_amino_acids controls only the VISIBLE amino-acid table -- the schema.org
    JSON-LD embedded in <script> always includes amino acids regardless (as
    additionalProperty entries), since it's invisible either way and doesn't affect
    printed page count. Deliberately off for the PDF path (see run()) -- kept out of
    the printed page by request, not because the data isn't there."""
    schema = build_schema_org(record, image_src)
    types = decode_types(record.get("allowed_type_ids") or [])
    restrictions = decode_restrictions(record.get("violated_restriction_ids") or [])

    ingredients_html = "\n".join(
        f"    <li>{html_escape(ingredient_line(ing))}</li>" for ing in record["ingredients"]
    )

    steps_html_parts = []
    for step in record["instructions"]:
        primary, callouts = step_text_and_callouts(step)
        item = f"    <li>{html_escape(primary)}"
        if callouts:
            callout_text = "<br>".join(html_escape(c) for c in callouts)
            item += f'<div class="callout">{callout_text}</div>'
        item += "</li>"
        steps_html_parts.append(item)
    steps_html = "\n".join(steps_html_parts)

    equipment_html = ""
    if record.get("cookwares"):
        items = "\n".join(f"    <li>{html_escape(c['name'])}</li>" for c in record["cookwares"])
        equipment_html = f"<h2>Equipment</h2>\n<ul>\n{items}\n</ul>\n"

    nutrition_html = ""
    lines = nutrition_lines(record.get("nutrition") or {})
    if lines:
        rows = "\n".join(f"    <tr><td>{html_escape(l.split(':')[0])}</td><td>{html_escape(l.split(':', 1)[1].strip())}</td></tr>" for l in lines)
        nutrition_html = f"<h2>Nutrition (per serving)</h2>\n<table>\n{rows}\n</table>\n"

    if show_amino_acids:
        amino_lines_found = amino_acid_lines(record.get("nutrition") or {})
        if amino_lines_found:
            amino_rows = "\n".join(
                f"    <tr><td>{html_escape(l.split(':')[0])}</td><td>{html_escape(l.split(':', 1)[1].strip())}</td></tr>"
                for l in amino_lines_found
            )
            nutrition_html += f"<h2>Amino Acids (per serving)</h2>\n<table>\n{amino_rows}\n</table>\n"

    badges = "".join(f'<span class="badge">{html_escape(t)}</span>' for t in types)
    warnings = "".join(f'<span class="badge warn">{html_escape(r)}</span>' for r in restrictions)

    image_html = f'<img src="{html_escape(image_src)}" alt="{html_escape(record["name"])}">' if image_src else ""

    # Ingredients + nutrition side by side, so both land on the first printed page
    # together; falls back to a single column when there's no nutrition data to pair
    # with (an empty second column would just look like unbalanced whitespace).
    ingredients_block = f"""<h2>Ingredients</h2>
<ul>
{ingredients_html}
</ul>"""
    if nutrition_html:
        ingredients_section = f"""<div class="two-col">
<div>
{ingredients_block}
</div>
<div>
{nutrition_html}
</div>
</div>"""
    else:
        ingredients_section = ingredients_block

    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html_escape(record['name'])}</title>
<script type="application/ld+json">
{schema_json}
</script>
<style>
body {{ font-family: Georgia, serif; max-width: 720px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ font-size: 1.8em; }}
img {{ max-width: 100%; border-radius: 8px; }}
.badge {{ display: inline-block; background: #e6f4ea; color: #1a5e2a; border-radius: 12px; padding: 2px 10px; margin: 2px 4px 2px 0; font-size: 0.85em; }}
.badge.warn {{ background: #fce8e6; color: #8a1c13; }}
.meta {{ color: #555; margin: 0.5em 0 1em; }}
.callout {{ color: #666; font-size: 0.9em; font-style: italic; margin-top: 2px; }}
table {{ border-collapse: collapse; }}
td {{ padding: 2px 12px 2px 0; }}
.two-col {{ display: flex; gap: 2.5em; align-items: flex-start; }}
.two-col > div {{ flex: 1; min-width: 0; }}
.page-break {{ page-break-before: always; }}
</style>
</head>
<body>
<h1>{html_escape(record['name'])}</h1>
{image_html}
<p class="meta">{record['serving_count']} servings &middot; {record.get('cooking_minutes', '?')} min &middot; {html_escape(record.get('units', ''))} units</p>
<div>{badges}{warnings}</div>
{ingredients_section}
<div class="page-break">
<h2>Instructions</h2>
<ol>
{steps_html}
</ol>
{equipment_html}
</div>
</body>
</html>
"""


# --- Open Recipe Format (ORF) ----------------------------------------------------

def _restriction_and_equipment_notes(record: dict) -> list[str]:
    """The "Contains/violates: ..." and "Equipment: ..." lines, shared verbatim between
    build_orf's and build_paprika's otherwise-different notes blocks."""
    notes = []
    restrictions = decode_restrictions(record.get("violated_restriction_ids") or [])
    if restrictions:
        notes.append("Contains/violates: " + ", ".join(restrictions))
    if record.get("cookwares"):
        notes.append("Equipment: " + ", ".join(c["name"] for c in record["cookwares"]))
    return notes


def build_orf(record: dict) -> dict:
    ingredients_orf = []
    for ing in record["ingredients"]:
        amount, unit = parse_quantity(ing.get("quantity"))
        ingredients_orf.append({ing["name"]: {"amounts": [{"amount": amount, "unit": unit}]}})

    steps_orf = []
    for step in record["instructions"]:
        primary, callouts = step_text_and_callouts(step)
        entry: dict = {"step": primary}
        if callouts:
            entry["notes"] = callouts
        steps_orf.append(entry)

    orf: dict = {
        "recipe_uuid": f"recipe-archive:{record['id']}",
        "recipe_name": record["name"],
        "yields": [{"amount": record["serving_count"], "unit": "servings"}],
        "ingredients": ingredients_orf,
        "steps": steps_orf,
    }

    notes = []
    if record.get("cooking_minutes"):
        notes.append(f"Total cook time: {record['cooking_minutes']} minutes")
    types = decode_types(record.get("allowed_type_ids") or [])
    if types:
        notes.append("Suitable menus: " + ", ".join(types))
    notes += _restriction_and_equipment_notes(record)
    lines = nutrition_lines(record.get("nutrition") or {})
    if lines:
        notes.append("Nutrition (per serving): " + "; ".join(lines))
    if notes:
        orf["notes"] = notes

    return orf


# --- Paprika format ----------------------------------------------------------
#
# Field set (uid/created/description/prep_time/cook_time/total_time/difficulty/
# "N servings"/source_url/photo family/hash) reverse-engineered from a real
# .paprikarecipe export produced and tested in a separate session -- not just
# the sparser shape this script originally wrote. photo_hash is confirmed to be
# sha256 of the raw image bytes (not the base64 text); the top-level hash has no
# known-correct formula from a single example, so it's defined here as a content
# fingerprint (sha256 of the rest of the fields, canonically serialized) rather
# than guessed -- good enough for internal consistency even if it doesn't match
# whatever Paprika's own app would compute.

def build_paprika(record: dict, image_bytes: bytes | None = None, image_ext: str = ".jpg") -> dict:
    ingredients_text = "\n".join(ingredient_line(ing) for ing in record["ingredients"])

    directions_parts = []
    for step in record["instructions"]:
        primary, callouts = step_text_and_callouts(step)
        text = primary
        if callouts:
            text += "\n(" + "; ".join(callouts) + ")"
        directions_parts.append(text)
    directions_text = "\n\n".join(directions_parts)

    notes_parts = _restriction_and_equipment_notes(record)

    fields = {
        "uid": str(uuid.uuid5(PAPRIKA_UUID_NAMESPACE, str(record["id"]))).upper(),
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name": record["name"],
        "description": "",
        "ingredients": ingredients_text,
        "directions": directions_text,
        "notes": "\n".join(notes_parts),
        "nutritional_info": "\n".join(
            nutrition_lines(record.get("nutrition") or {}) + amino_acid_lines(record.get("nutrition") or {})
        ),
        "prep_time": "",
        "cook_time": f"{record['cooking_minutes']} min" if record.get("cooking_minutes") else "",
        "total_time": "",
        "difficulty": "",
        "servings": f"{record['serving_count']} servings",
        "rating": 0,
        "source": "",
        "source_url": record.get("source_url") or "",
        "photo": None,
        "photo_large": None,
        "photo_hash": None,
        "image_url": None,
        "categories": export_tag_names(record),
        "photos": [],
    }

    # Real Paprika exports store the image twice: a top-level photo/photo_data/photo_hash
    # trio, plus a "photos" array entry (for its newer multi-photo support) whose
    # "filename" matches photo_large and whose own "hash" is the SHA-256 of ITS data.
    # We only have one source image, so it's reused for both slots -- confirmed against
    # a real export (photo_hash == sha256(photo_data), same pattern for the photos entry).
    if image_bytes:
        photo_filename = f"{str(uuid.uuid4()).upper()}{image_ext}"
        photo_large_filename = f"{str(uuid.uuid4()).upper()}{image_ext}"
        photo_b64 = base64.b64encode(image_bytes).decode("ascii")
        photo_hash = hashlib.sha256(image_bytes).hexdigest().upper()

        fields["photo"] = photo_filename
        fields["photo_data"] = photo_b64
        fields["photo_hash"] = photo_hash
        fields["photo_large"] = photo_large_filename
        fields["photos"] = [{
            "name": "1",
            "filename": photo_large_filename,
            "hash": photo_hash,
            "data": photo_b64,
        }]

    fields["hash"] = hashlib.sha256(
        json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest().upper()

    return fields


# --- Norish archive format (recipe.json inside a .norishrecipes archive) -------
#
# Reverse-engineered from Norish's own source (norish-recipes/norish): the
# archive writer (norish-writer.ts/norish-format.ts) and its Zod contracts
# (recipe.ts, recipe-ingredients.ts, step-ingredients.ts), cross-checked against
# their test fixtures (norish-writer.test.ts/norish-fixtures.ts), which run a
# fixture recipe through the real production writer and assert on the actual
# output -- not just guessed from reading the schema.

PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*")


def _ingredient_name_variants(name: str) -> set[str]:
    """Name forms worth trying as a suffix match against a step's
    secondary_message line: the full name, the part before its first comma
    (drops a qualifier like ', dried' that the callout often omits), the name
    with any parenthetical alias/qualifier removed ('green onions
    (scallions)' -> 'green onions', 'italian (flat-leaf) parsley' -> 'italian
    parsley' -- 15 of this library's 379 distinct ingredients have one), and a
    naive singular/plural toggle of each. Covers every gap found empirically
    against this library without overreaching into real fuzzy matching."""
    variants = {name.strip()}
    if "," in name:
        variants.add(name.split(",", 1)[0].strip())
    for variant in list(variants):
        stripped = PARENTHETICAL_RE.sub(" ", variant).strip()
        if stripped:
            variants.add(stripped)
    for variant in list(variants):
        variants.add(variant[:-1] if variant.endswith("s") and len(variant) > 1 else variant + "s")
    return {v for v in variants if v}


def _match_callout_ingredient(line: str, ingredients: list[dict]) -> dict | None:
    """The ingredient whose name matches as a case-insensitive suffix of this
    callout line, preferring the longest/most specific name on a tie (so
    'shredded cheese, Italian blend' wins over a shorter partial match)."""
    line_norm = line.strip().rstrip(".").lower()
    best, best_len = None, -1
    for ing in ingredients:
        for variant in _ingredient_name_variants(ing["name"]):
            variant_lower = variant.lower()
            if line_norm.endswith(variant_lower) and len(variant_lower) > best_len:
                best, best_len = ing, len(variant_lower)
    return best


def build_norish_step_ingredients(record: dict) -> list[list[dict]]:
    """One list of {ingredientOrder, share, order} chips per instruction step,
    derived from that step's secondary_message -- Mealime's free-text callout
    lines, which turn out to already be '<quantity> <ingredient name>' per line
    (confirmed empirically: 94.9%+ of callout lines matched an ingredient by
    name on a full-library sample, with the remaining gap being either the
    two name-variant issues _ingredient_name_variants covers, or an untracked
    pantry item like water that was never a real ingredient line to begin
    with). ingredientOrder is the ingredient's 0-based position in this same
    record's ingredients list, matching how recipeIngredients below is built
    from it. share is the fraction of that ingredient's total quantity used in
    this one step -- 1.0 unless the line is split across multiple steps."""
    ingredients = record.get("ingredients") or []
    per_step: list[list[dict]] = []

    for step in record.get("instructions") or []:
        secondary = (step.get("secondary_message") or "").strip()
        chips: list[dict] = []

        for line in (l.strip() for l in secondary.split("\n") if l.strip()):
            ing = _match_callout_ingredient(line, ingredients)
            if ing is None:
                continue

            # Identity-based lookup, not value-equality (list.index would always
            # return the position of the FIRST value-equal entry, silently wrong
            # if a recipe ever has two ingredient-list lines with the same name,
            # quantity, and ingredient_id and the match picked the second one).
            ingredient_order = next(i for i, x in enumerate(ingredients) if x is ing)

            # Real arithmetic, not display -- use the strict parser and treat a
            # miss on either side as "unknown", defaulting to the full ingredient
            # (share=1.0) rather than silently feeding parse_quantity's
            # display-oriented "amount 1" sentinel into a ratio.
            share = 1.0
            step_parsed = _parse_leading_number(line)
            total_parsed = _parse_leading_number(ing.get("quantity") or "")
            if step_parsed is not None and total_parsed is not None:
                step_amount, _ = step_parsed
                total_amount, _ = total_parsed
                if total_amount:
                    ratio = round(min(step_amount / total_amount, 1.0), 4)
                    if ratio > 0:
                        share = ratio

            chips.append({
                "ingredientOrder": ingredient_order,
                "share": share,
                "order": len(chips),
            })

        per_step.append(chips)

    return per_step


# tbsp counts that convert to a cup fraction instead of staying in tbsp --
# snapped to the nearest fraction a measuring cup actually offers rather than
# tbsp/16's exact (and uglier) division, e.g. 6 tbsp is exactly 0.375 cup but
# reads as the 1/3 cup measure, same as 5 tbsp. Deliberately only these four
# exact counts (an explicit ask) -- 7, 9-15, 16, and non-whole tbsp amounts
# stay in tbsp rather than guessing at a fuller table.
TBSP_TO_CUP_FRACTION = {4: 1 / 4, 5: 1 / 3, 6: 1 / 3, 8: 1 / 2}


def _rescale_norish_amount(amount: float, unit: str | None) -> tuple[float, str | None]:
    """Rescale a mass/volume/spoon amount to a friendlier display unit, per an
    explicit ask: kg collapses to g below 1 kg, and stays kg (rounded to 2
    decimals) at or above 1; L collapses to mL below 1 L, same rounding above;
    tbsp collapses to tsp below 1 tbsp (1 tbsp = 3 tsp) and to a cup fraction
    at the specific counts in TBSP_TO_CUP_FRACTION, otherwise stays tbsp.
    Every fraction-valued result here (tsp from a fractional tbsp, and every
    tbsp->cup conversion) is stored as that fraction's clean decimal (1/3 ->
    0.333, not tbsp/16's raw division) rather than left as an odd float, so
    Norish's own fraction-display toggle renders it as a clean unicode
    fraction rather than an ugly one. Norish has no rescaling of its own --
    confirmed by reading its actual unit-formatting code, which only picks
    singular/plural wording, never a different unit in the same family -- so
    this has to happen here, before export. Every other unit (already-small
    forms, cups, pieces, counts, etc.) passes through untouched."""
    if unit is None:
        return amount, unit
    unit_lower = unit.strip().lower()

    if unit_lower == "kg":
        return (round(amount * 1000, 3), "g") if amount < 1 else (round(amount, 2), "kg")
    if unit_lower in ("l", "liter", "liters", "litre", "litres"):
        return (round(amount * 1000, 3), "mL") if amount < 1 else (round(amount, 2), "L")
    if unit_lower == "tbsp":
        if amount < 1:
            return round(amount * 3, 3), "tsp"
        nearest_whole = round(amount)
        if abs(amount - nearest_whole) < 0.001 and nearest_whole in TBSP_TO_CUP_FRACTION:
            return round(TBSP_TO_CUP_FRACTION[nearest_whole], 3), "cup"
        return amount, "tbsp"

    return amount, unit


NUMERIC_6_2_LIMIT = 9999.99  # numeric(6,2)'s max representable magnitude (4 integer digits)


def format_numeric_6_2(value: float | None) -> str | None:
    """Formats a value for one of Norish's numeric(6,2) nutrition columns
    (fat/carbs/protein), or None if there's nothing to format or the value is
    corrupt enough to overflow the column -- see build_norish_recipe()'s
    docstring for a confirmed real example. None here means "don't know",
    same as an absent source value -- never fabricated as 0 or clamped to the
    limit, since either would misrepresent a recipe's actual nutrition."""
    if value is None:
        return None
    if abs(value) > NUMERIC_6_2_LIMIT:
        return None
    return f"{value:.2f}"


def build_norish_recipe(record: dict) -> dict:
    """Map a record to Norish's recipe.json wire shape. Fields with no Mealime
    equivalent (description, notes, origin/provenance, categories, cuisines,
    author) are left at their empty/null default rather than guessed. tags IS
    populated, from variety_tag_ids (see mealime_id_reference.VARIETY_TAGS for
    what's verified and why), the menu types the recipe satisfies (MENU_TYPE_TAG_IDS), and a
    "<Restriction>-Free" tag for every restriction it doesn't violate (export_tag_names -- shared
    with build_paprika's categories, so both formats offer the same filtering). Albertsons
    cookbooks, including "Kid friendly", arrive as variety_tag_ids (see the note by
    MENU_TYPE_TAG_IDS), so they need no separate handling here.
    url is left null even though source_url once held it, since that field was
    deliberately wiped from this library; restore it from
    mealime_field_backup.json first if you want it to travel.

    fat/carbs/protein are formatted as 2-decimal strings ("12.50"), matching
    Norish's numeric(6,2) columns as drizzle-zod serializes them (confirmed
    against their test fixture) -- calories, by contrast, is a plain integer
    column and travels as a real int.

    A value that would overflow numeric(6,2) (more than 4 digits before the
    decimal point, i.e. >9999.99) is dropped to None instead of formatted --
    confirmed on real library data: a handful of records have a corrupted
    nutrition object (e.g. one "Vegan"+Metric sibling with carbs=11683.93,
    protein=433.03 -- physically impossible for a single recipe -- while its own
    US sibling and every other group's Metric sibling in the same family have
    normal values, so this is corrupted source data, not a units mixup this
    function could correct). Sending it through as-is doesn't just record bad
    data -- it makes Norish's import FAIL the entire recipe (a numeric field
    overflow error), taking down an otherwise-good recipe. See
    NUMERIC_6_2_LIMIT/format_numeric_6_2() below.

    favorite (their spelling, not ours -- see the comment at its assignment below)
    is a dedicated top-level boolean field, not a tag, present only when
    mealime_favourites_sync.py has backfilled a definite value for this record."""
    system_used = NORISH_SYSTEM_USED.get(record.get("units") or "", "metric")
    nutrition = record.get("nutrition") or {}
    step_chips = build_norish_step_ingredients(record)

    recipe_ingredients = []
    for i, ing in enumerate(record.get("ingredients") or []):
        amount, unit_text = parse_quantity(ing.get("quantity"))
        amount, unit = _rescale_norish_amount(amount, unit_text.strip() or None)
        recipe_ingredients.append({
            "ingredientId": None,
            "ingredientName": ing["name"],
            "amount": round(amount, 3),  # matches Norish's amount column: numeric(10, 3)
            "unit": unit,
            "systemUsed": system_used,
            "order": i,
        })

    steps = []
    for i, step in enumerate(record.get("instructions") or []):
        steps.append({
            "step": step["primary_message"],
            "order": i,
            "systemUsed": system_used,
            "images": [],
            "stepIngredients": step_chips[i],
        })

    tags = [{"name": name} for name in export_tag_names(record)]

    # "favourite" (our own field name, see mealime_favourites_sync.py) maps to a real
    # dedicated field in Norish's own archive schema -- NOT a tag -- confirmed against
    # its actual source (NorishArchiveRecipeSchema in norish-format.ts: "favorite":
    # z.boolean().optional(), and norish-parser.ts's importedFavorite is threaded
    # through to addFavorite(userId, recipeId) on import). That field is spelled the
    # American way in Norish's own code/schema, unlike the rest of this project, so the
    # key below is "favorite" on purpose -- it has to match their Zod schema exactly or
    # the mark is silently dropped on import. Only genuine Mealime records ever carry
    # our own "favourite" field (Albertsons-sourced recipes aren't part of Mealime's own
    # favourites list at all), and only as an explicit True/False once backfilled, so
    # the key is omitted entirely rather than defaulted to False when we have no data.
    favourite = record.get("favourite")
    is_favourite = isinstance(favourite, bool)

    return {
        "name": record["name"],
        "description": None,
        "url": None,
        "notes": None,
        "servings": record["serving_count"],
        "systemUsed": system_used,
        "prepMinutes": None,
        "cookMinutes": None,
        "totalMinutes": record.get("cooking_minutes"),
        "calories": round(nutrition["energy"]) if nutrition.get("energy") is not None else None,
        "fat": format_numeric_6_2(nutrition.get("fat")),
        "carbs": format_numeric_6_2(nutrition.get("carbs")),
        "protein": format_numeric_6_2(nutrition.get("protein")),
        "originCountry": None,
        "originCountryName": None,
        "originRegion": None,
        "provenanceNote": None,
        "categories": [],
        "tags": tags,
        "cuisines": [],
        "recipeIngredients": recipe_ingredients,
        "steps": steps,
        "image": None,
        "images": [],
        "videos": [],
        "authorName": None,
        **({"favorite": favourite} if is_favourite else {}),
    }


# --- Main conversion loop -----------------------------------------------------

def run(recipes_dir: Path, out_dir: Path, formats: set[str],
        units: str | None = None, servings: int | None = None,
        menu: str = "classic", avoid: set[int] | None = None, dry_run: bool = False,
        norish_exporter_name: str | None = None,
        norish_exporter_origin: str = NORISH_DEFAULT_EXPORTER_ORIGIN) -> None:
    avoid = avoid or set()
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    browser = None
    page = None
    playwright_cm = None
    if "pdf" in formats and not dry_run:
        from playwright.sync_api import sync_playwright
        playwright_cm = sync_playwright()
        pw = playwright_cm.__enter__()
        browser = pw.chromium.launch()
        page = browser.new_page()

    paprika_bundle_entries: list[tuple[str, bytes]] = []
    norish_bundle_entries: list[tuple[str, dict, Path | None]] = []
    norish_family_recipe_ids: dict[int, list[int]] = {}
    counts = {fmt: 0 for fmt in ALL_FORMATS}
    total = 0
    scanned = 0
    skipped = 0
    fallback_included = 0
    menu_excluded = 0

    # Which single file wins each (recipe_id, units, serving_count) slot for this menu/avoid/
    # servings -- always computed now that --menu has no "no filter" state (see
    # select_menu_winners()'s docstring, including its --servings fallback, and the module
    # docstring's SELECTION section).
    menu_winners = select_menu_winners(recipes_dir, menu, avoid, servings)

    print("Converting..." if not dry_run else "Converting (dry run -- nothing will be written)...")
    try:
        for path in find_recipe_files(recipes_dir):
            scanned += 1
            if scanned % 500 == 0:
                print(f"...{scanned} file(s) scanned, {total} exported so far")

            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(record, dict) or "recipe_id" not in record:
                continue  # e.g. a stray non-recipe .json left in the library (not dict-shaped,
                          # or dict-shaped but not a recipe -- manifest.json et al.)

            if units is not None and record.get("units") != units:
                skipped += 1
                continue
            menu_slot = (record["recipe_id"], record.get("units"), record.get("serving_count"))
            if menu_winners.get(menu_slot) != record.get("id"):
                menu_excluded += 1
                continue
            if servings is not None and record.get("serving_count") != servings:
                fallback_included += 1  # this family/units' fallback answer -- see select_menu_winners()
            total += 1

            family_src_dir = path.parent
            family_out_dir = out_dir / FAMILIES_DIRNAME / str(record["recipe_id"])
            if not dry_run:
                family_out_dir.mkdir(parents=True, exist_ok=True)

            thumbnail_rel = None
            thumbnail_path = None
            if not dry_run:
                for thumb in family_src_dir.glob("thumbnail.*"):
                    dest = family_out_dir / thumb.name
                    if not dest.exists():
                        shutil.copy2(thumb, dest)
                    thumbnail_rel = thumb.name
                    thumbnail_path = dest
                    break

            base_name = f"{record['id']}_{record['slug']}"

            if "json" in formats:
                if not dry_run:
                    exported_record = {**record, **EXPORT_CLEARED_FIELDS}
                    (family_out_dir / f"{base_name}.json").write_text(
                        json.dumps(exported_record, indent=2, ensure_ascii=False), encoding="utf-8")
                counts["json"] += 1

            if "norish" in formats:
                norish_recipe = build_norish_recipe(record)
                if not dry_run:
                    (family_out_dir / f"{base_name}.norish.json").write_text(
                        json.dumps(norish_recipe, indent=2, ensure_ascii=False), encoding="utf-8")
                norish_bundle_entries.append((str(record["id"]), norish_recipe, thumbnail_path))
                norish_family_recipe_ids.setdefault(record["recipe_id"], []).append(record["id"])
                counts["norish"] += 1

            if "html" in formats:
                # Relative src -- portable as long as the file stays next to its
                # thumbnail, which is how it's saved here and however the user copies
                # the family folder elsewhere.
                if not dry_run:
                    (family_out_dir / f"{base_name}.html").write_text(
                        render_html(record, thumbnail_rel), encoding="utf-8")
                counts["html"] += 1

            if "orf" in formats:
                if not dry_run:
                    orf_dict = build_orf(record)
                    yaml_text = yaml.safe_dump(orf_dict, sort_keys=False, allow_unicode=True)
                    (family_out_dir / f"{base_name}.orf.yaml").write_text(yaml_text, encoding="utf-8")
                counts["orf"] += 1

            if "paprika" in formats:
                if not dry_run:
                    image_bytes = thumbnail_path.read_bytes() if thumbnail_path else None
                    image_ext = thumbnail_path.suffix if thumbnail_path else ".jpg"
                    paprika_dict = build_paprika(record, image_bytes=image_bytes, image_ext=image_ext)
                    gz_bytes = gzip.compress(json.dumps(paprika_dict, ensure_ascii=False).encode("utf-8"))
                    (family_out_dir / f"{base_name}.paprikarecipe").write_bytes(gz_bytes)
                    arcname = f"{sanitize_filename(record['name'])}_{record['id']}.paprikarecipe"
                    paprika_bundle_entries.append((arcname, gz_bytes))
                counts["paprika"] += 1

            if "pdf" in formats and dry_run:
                counts["pdf"] += 1
            elif "pdf" in formats:
                # Rendered as its own copy (rather than reusing the "html" format's
                # content) because its <img> src needs to be an absolute file:// URI:
                # the source html is written to a short, flat temp path rather than
                # inside family_out_dir -- a long recipe slug plus a deeply nested
                # --out-dir can push the real output path past Windows' ~260-char
                # MAX_PATH, which Chromium (a separate OS process from Python) fails
                # to open even when Python itself can write there. That temp file's
                # own location has nothing to do with family_out_dir, so a thumbnail
                # reference relative to it wouldn't resolve -- hence the absolute URI.
                pdf_image_src = thumbnail_path.resolve().as_uri() if thumbnail_path else None
                pdf_html_content = render_html(record, pdf_image_src, show_amino_acids=False)

                # Guaranteed set by the "pdf" in formats and not dry_run setup above (this branch
                # is only reachable under that same condition) -- asserted rather than left implicit
                # so the type checker can see it too.
                assert page is not None
                fd, tmp_name = tempfile.mkstemp(suffix=".html")
                tmp_html_path = Path(tmp_name)
                try:
                    os.close(fd)
                    tmp_html_path.write_text(pdf_html_content, encoding="utf-8")
                    page.goto(tmp_html_path.resolve().as_uri())
                    page.pdf(path=str(family_out_dir / f"{base_name}.pdf"), format="Letter", print_background=True,
                             margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"})
                finally:
                    tmp_html_path.unlink(missing_ok=True)
                counts["pdf"] += 1
    finally:
        if browser:
            browser.close()
        if playwright_cm:
            playwright_cm.__exit__(None, None, None)

    if "paprika" in formats and paprika_bundle_entries and not dry_run:
        bundle_path = out_dir / PAPRIKA_BUNDLE_FILENAME
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_STORED) as zf:
            for arcname, gz_bytes in paprika_bundle_entries:
                zf.writestr(arcname, gz_bytes)
        print(f"Combined Paprika bundle: {bundle_path} ({len(paprika_bundle_entries)} recipes)")

    if "norish" in formats and norish_bundle_entries:
        multi_variant_families = {rid: ids for rid, ids in norish_family_recipe_ids.items() if len(ids) > 1}
        if multi_variant_families:
            print(f"\nWARNING: {len(multi_variant_families)} recipe famil(ies) have more than one "
                  f"serving/unit variant bundled into this .norishrecipes archive (e.g. recipe_id "
                  f"{next(iter(multi_variant_families))} -> ids {next(iter(multi_variant_families.values()))}). "
                  f"Norish scales servings and converts units live from a single stored recipe, so each extra "
                  f"variant becomes a separate near-duplicate recipe on import rather than one that scales. "
                  f"Re-run with --units/--servings to pick just one variant per family.")

        if not dry_run:
            bundle_name = f"norish-recipes-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.norishrecipes"
            bundle_path = out_dir / bundle_name
            manifest = {
                "format": NORISH_ARCHIVE_FORMAT,
                "formatVersion": NORISH_ARCHIVE_FORMAT_VERSION,
                "exportedAt": datetime.now(timezone.utc).isoformat(),
                "exporter": {"name": norish_exporter_name, "origin": norish_exporter_origin},
                "recipeCount": len(norish_bundle_entries),
            }
            with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
                for folder_key, norish_recipe, thumb_path in norish_bundle_entries:
                    recipe_to_write = norish_recipe
                    if thumb_path is not None:
                        # A copy, not a mutation -- the same dict is also the loose
                        # per-recipe .norish.json written earlier, which has no images/
                        # subfolder to be relative to and must keep image/images null.
                        image_rel = f"images/{thumb_path.name}"
                        recipe_to_write = {**norish_recipe, "image": image_rel,
                                           "images": [{"image": image_rel, "order": 0}]}
                        zf.write(thumb_path, f"{folder_key}/{image_rel}")
                    zf.writestr(f"{folder_key}/recipe.json",
                                json.dumps(recipe_to_write, indent=2, ensure_ascii=False))
            print(f"Combined Norish archive: {bundle_path} ({len(norish_bundle_entries)} recipes)")

    print(f"\n{'Dry run: would process' if dry_run else 'Done. Processed'} {total} recipe(s)."
          + (f" Skipped {skipped} not matching the units filter." if skipped else "")
          + (f" Included {fallback_included} despite the --servings filter as their family/units' "
             f"only available serving count." if fallback_included else "")
          + (f" Excluded {menu_excluded} not selected by --menu/--avoid for their (family, units, "
             f"servings) slot." if menu_excluded else ""))
    for fmt in ALL_FORMATS:
        if fmt in formats:
            print(f"  {fmt}: {counts[fmt]} file(s) {'would be written' if dry_run else 'written'}")
    if dry_run:
        print("Dry run: no files were written.")
    else:
        print(f"Saved to: {out_dir}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert scraped recipe JSON into the original JSON, HTML (schema.org), "
                    "ORF YAML, Paprika format, and/or PDF, keeping the families/<id>/ folder structure."
    )
    parser.add_argument("--recipes-dir", type=Path, default=DEFAULT_RECIPES_DIR,
                        help=f"Source library folder (default: {DEFAULT_RECIPES_DIR})")
    parser.add_argument("--out-dir", type=Path, required=True,
                        help="Destination folder for the converted files (created if needed)")
    parser.add_argument("--formats", nargs="+", choices=ALL_FORMATS, default=list(ALL_FORMATS),
                        help=f"Which format(s) to generate (default: all of {', '.join(ALL_FORMATS)})")
    parser.add_argument("--units", type=normalize_units, default=None,
                        help="Only convert this units system: metric or US (default: convert both)")
    parser.add_argument("--servings", type=int, choices=(2, 4, 6), default=None,
                        help="Only convert this serving size: 2, 4, or 6 (default: convert all)")
    parser.add_argument("--menu", choices=MENU_CHOICES, default="classic",
                        help="Collapse each family to the one recipe matching this menu, instead of "
                             "exporting every diet-swap sibling -- always applied, no \"export "
                             "everything uncollapsed\" mode exists (default: classic; see the "
                             "module docstring)")
    parser.add_argument("--avoid", nargs="+", choices=AVOID_CHOICES, default=None,
                        help="Also drop any candidate that violates one of these restrictions "
                             "(composes with --menu; see the module docstring)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be exported (including --menu/--avoid's effect) "
                             "without writing any files")
    parser.add_argument("--norish-exporter-name", type=str, default=None,
                        help="Attribution name written into the .norishrecipes manifest (default: none)")
    parser.add_argument("--norish-exporter-origin", type=str, default=NORISH_DEFAULT_EXPORTER_ORIGIN,
                        help="Origin URL written into the .norishrecipes manifest "
                             f"(default: {NORISH_DEFAULT_EXPORTER_ORIGIN})")
    return parser.parse_args()


def main():
    args = parse_args()
    avoid_ids = {AVOID_NAME_TO_RESTRICTION_ID[name] for name in (args.avoid or [])}
    run(args.recipes_dir, args.out_dir, set(args.formats), units=args.units, servings=args.servings,
        menu=args.menu, avoid=avoid_ids, dry_run=args.dry_run,
        norish_exporter_name=args.norish_exporter_name, norish_exporter_origin=args.norish_exporter_origin)


if __name__ == "__main__":
    main()
