#!/usr/bin/env python3
"""
Imports NYT Cooking recipes the same way seriouseats_scraper.py imports Serious Eats -- and with its machinery:
the registry, the polite fetch loop that stops when refused, thumbnails, and external_align.py, which turns each
page into the library's US + Metric records. What is NYT-specific lives here: finding the recipes, and reading a
page (parse_nyt_page) into the same shape external_recipes.parse_page gives for Serious Eats.

FINDING THE RECIPES
  A recipe URL is /recipes/<NYT number>-<slug>, and the slug is required: the bare number, or a wrong slug, is a
  404 with no redirect. The slugs come from www.nytimes.com's cooking sitemap: an index of one file per month
  (1958 onward, ~540 files) listing every recipe URL with its last-modified time (~25,700 recipes in Oct 2026).
  When a recipe's slug changes (same NYT number, new URL) its registry row follows the new URL and keeps its id.

READING A PAGE  (no login needed: a plain request gets the whole recipe; the paywall is only a flag)
  * the schema.org Recipe JSON-LD: name, yield, total time, categories, keywords, rating, image
  * the page's __NEXT_DATA__ -> props.pageProps.scoopRecipe: the ingredient list WITH its section headings (the
    JSON-LD flattens them), the steps, the headnote (used as the description), tips and FAQs (-> Notes), and 12
    nutrients per serving (incl. mono-/polyunsaturated fat, which the JSON-LD leaves out).
  NYT writes "½ cup/120 milliliters vegetable oil" and "an 8-inch/20-centimeter pan"; the metric part is turned into
  the "(120 milliliters)" brackets the aligner reads, and a bare "350 degrees" into "350°F".

COMMANDS
    python nytcooking_scraper.py discover                  read the sitemaps, record every recipe URL (~540 small requests)
    python nytcooking_scraper.py fetch --limit 5           fetch pages that are new or changed (try a few first)
    python nytcooking_scraper.py fetch --sample 100 --align --out-dir ../scratch_nyt1    a random test batch, into a scratch folder
    python nytcooking_scraper.py align --limit 5           re-align pages already saved (no network): the dev loop
    python nytcooking_scraper.py fetch                     all of them (~14 h for ~25,700 pages at the 2 s default)
    python nytcooking_scraper.py thumbnails                download the picture of every recipe that has none
    python nytcooking_scraper.py status                    counts by state
    python nytcooking_scraper.py run                       discover, then fetch + align (what a scheduled job would call)

  --out-dir, --review-file, --no-raw, --no-thumbs, --match, --ids, --sample/--seed, --delay and --retry-wait work as
  in seriouseats_scraper.py. Rows go in the same <library>/source_registry.csv, with source "nytcooking".

IDS  NYT Cooking's stretch of the imported-recipe block starts at family id 6,000,106 (external_recipes.
  SOURCE_INDEX_OFFSET); Metric twin = id + 71, as for Serious Eats. New URLs are numbered in order of NYT's own
  recipe number; ids are permanent once handed out.

SAVED PAGES  <raw-dir>/nytcooking/<id>.html.gz -- not the whole page (~700 KB, mostly site code and reader comments)
  but the two parts the parser reads, the Recipe JSON-LD and the scoopRecipe data, ~5 KB gzipped (~150 MB for all).
  A page those parts can't be cut out of is saved whole.

Needs `pip install requests` (and Pillow for thumbnails).
"""

import argparse
import gzip
import html as html_lib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

import seriouseats_scraper as se
from external_align import Aligner
from external_recipes import (SOURCE_INDEX_OFFSET, family_index, first_number, is_family_id, iso_minutes,
                              load_recipe_jsonld, nth_family_id, parse_nutrition)
from library_paths import DEFAULT_LIBRARY_DIR, TOOLS_DIR

SOURCE = "nytcooking"
SITEMAP_URL = "https://www.nytimes.com/sitemaps/new/cooking.xml.gz"
RECIPE_URL_RE = re.compile(r"^https://cooking\.nytimes\.com/recipes/(\d+)-[^/?#]+$")
INDEX_OFFSET = SOURCE_INDEX_OFFSET[SOURCE]
DEFAULT_DELAY = 2.0


# --- discovery -------------------------------------------------------------------------------

def site_id(url: str) -> int:
    """NYT's own recipe number, the part of the URL that never changes ('/recipes/1020571-pumpkin-bread' -> 1020571)."""
    return int(RECIPE_URL_RE.match(url).group(1))


def get_xml(session: requests.Session, url: str) -> str:
    """A sitemap's text. The files are named .xml.gz; the server usually sends them already unpacked, but not always."""
    response = se.http_get(session, url)
    if response.status_code != 200:
        raise RuntimeError(f"{url}: HTTP {response.status_code}")
    body = response.content
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body.decode("utf-8")


def sitemap_entries(session: requests.Session, sitemap_url: str = SITEMAP_URL) -> list[tuple[str, str]]:
    """[(url, lastmod)] of every URL in the cooking sitemaps (the index first, then each monthly file)."""
    xml = get_xml(session, sitemap_url)
    children = re.findall(r"<sitemap>.*?<loc>([^<]+)</loc>.*?</sitemap>", xml, re.S)
    if not children:
        children, xml_files = [], [xml]
    else:
        xml_files = []
        for n, child in enumerate(children, start=1):
            xml_files.append(get_xml(session, child.strip()))
            if n % 100 == 0:
                print(f"  ...{n}/{len(children)} sitemap files read")
    entries = []
    for text in xml_files:
        for block in re.findall(r"<url>(.*?)</url>", text, re.S):
            loc = re.search(r"<loc>([^<]+)</loc>", block)
            mod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            if loc:
                entries.append((html_lib.unescape(loc.group(1).strip()), mod.group(1).strip() if mod else ""))
    return entries


def check_ids_current(mine: list[dict]) -> None:
    if any(not is_family_id(int(r["id"])) or family_index(int(r["id"])) < INDEX_OFFSET for r in mine):
        raise RuntimeError(f"the registry has {SOURCE} ids outside NYT Cooking's stretch of the id block")


def discover(session: requests.Session, rows: dict, urls_file: Path | None = None) -> tuple[int, int, int, int]:
    """Add new recipe URLs to the registry, follow renamed ones, refresh lastmods.
    Returns (new, changed, renamed, total_recipe_urls)."""
    if urls_file:
        entries = [(line.strip(), "") for line in urls_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        entries = sitemap_entries(session)
    recipes: dict[str, str] = {}
    for url, mod in entries:
        url = url.split("?")[0].split("#")[0]
        if RECIPE_URL_RE.match(url) and mod >= recipes.get(url, ""):
            recipes[url] = mod

    mine = [r for (src, _), r in rows.items() if src == SOURCE]
    check_ids_current(mine)
    by_number = {site_id(r["url"]): r for r in mine}
    next_index = max((family_index(int(r["id"])) for r in mine), default=INDEX_OFFSET - 1) + 1
    new = changed = renamed = 0
    for url in sorted(recipes, key=site_id):
        key = (SOURCE, url)
        if key in rows:
            if recipes[url] and recipes[url] != rows[key]["lastmod"]:
                rows[key]["lastmod"] = recipes[url]
                if rows[key]["status"] in ("ok", "norecipe") and se.is_stale(rows[key]):
                    rows[key]["status"] = "stale"
                    changed += 1
            continue
        old = by_number.get(site_id(url))
        if old is not None and old["url"] not in recipes:     # same recipe, new slug: keep its id, follow the new URL
            del rows[(SOURCE, old["url"])]
            old["url"], old["lastmod"] = url, recipes[url]
            if old["status"] in ("ok", "norecipe", "gone"):
                old["status"] = "stale"
            rows[key] = old
            renamed += 1
            continue
        rows[key] = {"source": SOURCE, "url": url, "id": str(nth_family_id(next_index)), "lastmod": recipes[url],
                     "fetched_lastmod": "", "fetched_at": "", "status": "new"}
        by_number[site_id(url)] = rows[key]
        next_index += 1
        new += 1
    return new, changed, renamed, len(recipes)


# --- reading a page --------------------------------------------------------------------------

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
JSONLD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>.*?</script>', re.S)
BLOCK_TYPES = {"paragraph", "list_item", "h3", "step", "heading"}
METRIC_UNIT = r"(?:kilograms?|grams?|milli(?:liter|litre)s?|lit(?:er|re)s?|centimet(?:er|re)s?|millimet(?:er|re)s?|kg|g|ml|l|cm|mm)"
# "cup/120 milliliters", "cup/6 ounces": a slash after a word (never "1/2", a fraction) and a measure
SLASH_METRIC_RE = re.compile(r"(?<=[A-Za-z)])\s*/\s*(\d[\d.,½¼¾⅓⅔⅛ ]*(?:\s*(?:to|-|–)\s*\d[\d.,]*|[\s-]*(?:by|x|×)[\s-]*\d[\d.,]*)*[\s-]*"
                             r"(?:" + METRIC_UNIT + r"|ounces?|pounds?|sticks?))\b")
BARE_DEGREES_RE = re.compile(r"\b([2-5]\d\d)(\s*(?:to|-|–)\s*[2-5]\d\d)?\s*degrees(?:\s+Fahrenheit)?\b(?!\s*(?:F\b|C\b|Celsius))", re.I)
SHORT_UNITS = [(r"kilograms?", "kg"), (r"grams?", "g"), (r"milli(?:liter|litre)s?", "ml"), (r"lit(?:er|re)s?", "l"),
               (r"centimet(?:er|re)s?", "cm"), (r"millimet(?:er|re)s?", "mm")]
SCOOP_FIELDS = ("title", "ingredients", "preparation", "headnote", "tips", "faqs", "yield", "legacyTime", "nutritionalInfo")
NUTRIENT_KEYS = {"CALORIE": "energy", "FAT": "fat", "SATURATED_FAT": "saturated", "MONOUNSATURATED_FAT": "monounsaturated",
                 "POLYUNSATURATED_FAT": "polyunsaturated", "TRANS_FAT": "transfats", "CARBOHYDRATE": "carbs",
                 "FIBER": "fiber", "SUGAR": "sugars", "PROTEIN": "protein", "SODIUM": "sodium", "CHOLESTEROL": "cholesterol"}


def scoop_recipe(page: str) -> dict | None:
    m = NEXT_DATA_RE.search(page)
    if not m:
        return None
    try:
        return json.loads(m.group(1))["props"]["pageProps"].get("scoopRecipe")
    except (ValueError, KeyError, TypeError):
        return None


def rich_text(node) -> str:
    """Plain text of a rich-text tree (scoopRecipe's ingredients, steps, headnote, tips); one line per block."""
    if isinstance(node, list):
        return "".join(rich_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text") or ""
    inner = rich_text(node.get("content") or node.get("doc") or [])
    return inner + "\n" if node.get("type") in BLOCK_TYPES else inner


def clean(text: str) -> str:
    lines = (re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def short_metric(amount: str) -> str:
    """"120 milliliters" -> "120 ml", "20-centimeter" -> "20 cm": the way the aligner reads metric amounts."""
    amount = re.sub(r"(\d)[\s-]+(?=" + METRIC_UNIT + r"\b)", r"\1 ", amount)
    for long, short in SHORT_UNITS:
        amount = re.sub(r"\b" + long + r"\b", short, amount)
    return amount


def metric_in_brackets(text: str) -> str:
    """"½ cup/120 milliliters oil" -> "½ cup (120 ml) oil"; "8-inch/20-centimeter pan" -> "8-inch (20 cm) pan"."""
    return SLASH_METRIC_RE.sub(lambda m: f" ({short_metric(m.group(1))})", text)


def fahrenheit(text: str) -> str:
    """NYT's ovens are in Fahrenheit without saying so: "350 degrees" -> "350°F", "425 to 450 degrees" -> "425 to 450°F"."""
    return BARE_DEGREES_RE.sub(lambda m: f"{m.group(1)}{m.group(2) or ''}°F", text)


def doc_children(field) -> list:
    """The top-level nodes of a scoopRecipe rich-text field ({'doc': {'content': [body]}} -> body's children)."""
    doc = (field or {}).get("doc") if isinstance(field, dict) else None
    nodes = (doc or {}).get("content") or []
    if len(nodes) == 1 and nodes[0].get("type", "").startswith("scoop_recipe_"):
        nodes = nodes[0].get("content") or []
    return nodes


def ingredient_sections(scoop: dict) -> list[dict]:
    """[{'section': 'For the Vinaigrette' | None, 'items': [{'raw', 'name', 'quantity', 'note'}]}] in page order."""
    sections, heading = [], None
    for node in doc_children(scoop.get("ingredients")):
        if node.get("type") in ("h3", "heading"):
            heading = clean(rich_text(node)).rstrip(":").strip() or None
            if heading and heading.isupper():                  # "FOR THE VINAIGRETTE" -> "For the vinaigrette"
                heading = heading.capitalize()
        elif node.get("type") == "bullet_list":
            items = []
            for li in node.get("content") or []:
                raw = metric_in_brackets(clean(rich_text(li)).replace("\n", " "))
                if raw:
                    items.append({"raw": raw, "name": raw, "quantity": None, "note": None})
            if items:
                sections.append({"section": heading, "items": items})
            heading = None
    return sections


def steps_of(scoop: dict) -> list[str]:
    """Step texts in order; NYT's step-group headings ("FOR THE VINAIGRETTE") are dropped, as for Serious Eats."""
    return [fahrenheit(metric_in_brackets(clean(rich_text(n)))) for n in doc_children(scoop.get("preparation"))
            if n.get("type") == "step" and clean(rich_text(n))]


def notes_of(scoop: dict) -> str | None:
    paragraphs = [clean(rich_text(t.get("details"))) for t in scoop.get("tips") or [] if isinstance(t, dict)]
    for faq in scoop.get("faqs") or []:
        if isinstance(faq, dict):
            parts = [v if isinstance(v, str) else rich_text(v) for k, v in faq.items() if k not in ("uri", "__typename")]
            paragraphs.append(clean(" ".join(parts)).replace("\n", " "))
    text = "\n".join(p for p in paragraphs if p)
    return fahrenheit(metric_in_brackets(text)) or None


def servings_of(scoop: dict | None, recipe_yield) -> int | None:
    """Servings from the yield: the 'servings' entry where there is one ("1 loaf (about 10 servings)" -> 10), else the first count."""
    entries = ((scoop or {}).get("yield") or {}).get("servings") or []
    for entry in entries:
        if "serving" in (entry.get("unit") or "").lower() and entry.get("count"):
            return int(entry["count"])
    yields = recipe_yield if isinstance(recipe_yield, list) else [recipe_yield] if recipe_yield else []
    for text in yields:
        if "serving" in str(text).lower():
            return first_number(text)
    if entries and entries[0].get("count"):
        return int(entries[0]["count"])
    return first_number(yields[0]) if yields else None


def nutrition_of(scoop: dict | None, ld_nutrition) -> dict:
    found = {}
    for n in ((scoop or {}).get("nutritionalInfo") or {}).get("perServingNutrients") or []:
        key = NUTRIENT_KEYS.get(n.get("code"))
        if key and isinstance(n.get("quantity"), (int, float)):
            found[key] = round(n["quantity"], 2)
    return found or parse_nutrition(ld_nutrition)


def split_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def parse_nyt_page(page: str, url: str | None = None) -> dict:
    """An NYT Cooking page -> the dict external_recipes.parse_page returns for Serious Eats (what external_align reads)."""
    ld = load_recipe_jsonld(page)
    scoop = scoop_recipe(page)
    authors = ld.get("author") or []
    if isinstance(authors, dict):
        authors = [authors]
    image = ld.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    image_url = image.get("url") if isinstance(image, dict) else image
    agg = ld.get("aggregateRating") if isinstance(ld.get("aggregateRating"), dict) else None
    try:
        rating = (float(agg["ratingValue"]), int(float(agg.get("ratingCount") or agg.get("reviewCount") or 0))) if agg else None
    except (KeyError, TypeError, ValueError):
        rating = None
    if scoop:
        sections, steps = ingredient_sections(scoop), steps_of(scoop)
        description = clean(rich_text(scoop.get("headnote"))) or None
        notes = notes_of(scoop)
    else:                                                       # no page data: the JSON-LD alone (no sections, no tips)
        sections = [{"section": None, "items": [{"raw": metric_in_brackets(clean(s)), "name": clean(s), "quantity": None, "note": None}
                                                for s in ld.get("recipeIngredient") or [] if clean(s)]}]
        steps = [fahrenheit(metric_in_brackets(clean(s.get("text", "") if isinstance(s, dict) else str(s))))
                 for s in ld.get("recipeInstructions") or []]
        description, notes = None, None
    if not any(s["items"] for s in sections):
        raise ValueError("no ingredient list found in the page")
    description = description or (clean(html_lib.unescape(ld.get("description") or "")) or None)
    total = ld.get("totalTime")
    legacy = ((scoop or {}).get("legacyTime") or {}).get("totalTime") or {}
    return {
        "rating": rating,
        "name": clean(html_lib.unescape(ld.get("name") or (scoop or {}).get("title") or "")),
        "description": fahrenheit(metric_in_brackets(description)) if description else None,
        "url": url or ld.get("url"),
        "authors": [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")],
        "servings": servings_of(scoop, ld.get("recipeYield")),
        "total_minutes": iso_minutes(total) if total else legacy.get("minutes"),
        "total_minutes_max": iso_minutes(total, longest=True) if total else legacy.get("minutes"),
        "prep_minutes": iso_minutes(ld.get("prepTime")),
        "cook_minutes": iso_minutes(ld.get("cookTime")),
        "categories": split_list(ld.get("recipeCategory")),
        "cuisines": split_list(ld.get("recipeCuisine")),
        "keywords": ld.get("keywords"),
        "nutrition": nutrition_of(scoop, ld.get("nutrition")),
        "image_url": image_url,
        "ingredient_sections": sections,
        "steps": steps,
        "extra_sections": {"Notes": notes} if notes else {},
        "jsonld_ingredient_count": len(ld.get("recipeIngredient") or []),
        "jsonld_step_count": len(ld.get("recipeInstructions") or []),
    }


def trimmed_page(page: bytes) -> bytes:
    """Just the parts parse_nyt_page reads: the Recipe JSON-LD and a __NEXT_DATA__ holding scoopRecipe's SCOOP_FIELDS.
    The whole page when they can't be found (it is then kept as served)."""
    text = page.decode("utf-8", errors="replace")
    scoop = scoop_recipe(text)
    blocks = [b for b in JSONLD_RE.findall(text) if re.search(r'"@type"\s*:\s*\[?\s*"Recipe"', b)]
    if not scoop or not blocks:
        return page
    scoop = {k: scoop[k] for k in SCOOP_FIELDS if k in scoop}
    next_data = json.dumps({"props": {"pageProps": {"scoopRecipe": scoop}}}, ensure_ascii=False).replace("</", "<\\/")
    return (f"<!doctype html>\n<!-- trimmed by nytcooking_scraper.py, {datetime.now():%Y-%m-%d} -->\n{blocks[0]}\n"
            f'<script id="__NEXT_DATA__" type="application/json">{next_data}</script>\n').encode("utf-8")


# --- command line ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["discover", "fetch", "align", "thumbnails", "status", "run"])
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR, help="Library root (holds source_registry.csv)")
    parser.add_argument("--raw-dir", type=Path, default=se.DEFAULT_RAW_DIR, help=f"Where saved pages go (default {se.DEFAULT_RAW_DIR})")
    parser.add_argument("--urls-file", type=Path, help="discover from a file of URLs (one per line) instead of the sitemap")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Seconds between page fetches (default {DEFAULT_DELAY:g})")
    parser.add_argument("--retry-wait", type=float, default=se.RETRY_WAIT,
                        help=f"fetch: seconds to wait before retrying a refused page (then double; 0 = no retries; default {se.RETRY_WAIT:g})")
    parser.add_argument("--limit", type=int, help="fetch at most this many pages")
    parser.add_argument("--match", help="fetch only URLs containing this text (e.g. 'pad-thai')")
    parser.add_argument("--ids", help="fetch only these library ids (comma-separated)")
    parser.add_argument("--sample", type=int, help="fetch: a random sample of this many of the pages still to fetch (for test batches)")
    parser.add_argument("--seed", type=int, help="with --sample: makes the random sample repeatable")
    parser.add_argument("--align", action="store_true", help="fetch: also align each page into library records as it arrives (implied by `run`)")
    parser.add_argument("--no-raw", action="store_true", help="fetch: don't keep the page on disk (fetch -> align in one pass)")
    parser.add_argument("--no-thumbs", action="store_true", help="fetch --align: don't download each recipe's picture (thumbnail.jpg)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where aligned records and the ingredient master are written (default: the library); a scratch "
                             "folder while developing gets a COPY of the master, so the real one is untouched")
    parser.add_argument("--review-file", type=Path, default=TOOLS_DIR / "nytcooking_review_report.json",
                        help="where the alignment review report goes (default: tools/nytcooking_review_report.json)")
    args = parser.parse_args()

    registry_path = args.library_dir / "source_registry.csv"
    rows = se.load_registry(registry_path)
    session = se.make_session()
    try:
        check_ids_current([r for (src, _), r in rows.items() if src == SOURCE])
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return se.EXIT_ERROR
    aligner = None
    if args.command == "align" or args.align or args.command == "run":
        aligner = Aligner(args.library_dir, args.out_dir or args.library_dir, args.review_file, source=SOURCE, parse=parse_nyt_page)
    ids = {int(x) for x in args.ids.split(",")} if args.ids else None
    try:
        if args.command in ("discover", "run"):
            new, changed, renamed, total = discover(session, rows, args.urls_file)
            se.save_registry(registry_path, rows)
            print(f"Discovered {total} recipe URL(s): {new} new, {changed} changed since last fetched, {renamed} renamed.")
        if args.command in ("fetch", "run"):
            def on_page(row: dict, page: bytes) -> None:
                if not args.no_raw:                                     # the trimmed page; run_fetch's own (whole-page) save is off
                    path = se.raw_path(args.raw_dir, row)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(gzip.compress(trimmed_page(page)))
                if aligner is not None:
                    info = aligner.align(row, page)
                    if not args.no_thumbs:
                        se.note_thumbnail(se.ensure_thumbnail(session, info["family_dir"], info["image_url"], info["previous_url"]), row)
            counts = se.run_fetch(session, rows, registry_path, args.raw_dir, delay=args.delay,
                                  limit=args.limit, match=args.match, ids=ids, keep_raw=False, on_page=on_page,
                                  retry_wait=args.retry_wait, sample=args.sample, seed=args.seed, source=SOURCE)
            print("Fetched:", counts or "nothing")
        if args.command == "thumbnails":
            se.run_thumbnails(session, rows, args.raw_dir, args.out_dir or args.library_dir, delay=args.delay,
                              limit=args.limit, match=args.match, ids=ids, source=SOURCE, parse=parse_nyt_page)
        if se.THUMB_COUNTS:
            print("Thumbnails:", se.THUMB_COUNTS)
        if args.command == "align":
            failed = se.run_align(rows, args.raw_dir, aligner, limit=args.limit, match=args.match, ids=ids, source=SOURCE)
            print(f"Aligned {aligner.aligned} recipe(s)" + (f", {failed} could not be aligned" if failed else ""))
        if aligner is not None:
            review = aligner.finish()
            new_ings = review["new_ingredients"]
            print(f"Review report: {args.review_file}\n  new ingredients: {new_ings['count']} ({new_ings['undecided']} still need a "
                  f"restriction decision) | loose matches: {len(review['loose_matches'])} | optional lines moved to notes: "
                  f"{len(review['optional_lines_moved_to_notes'])} | categories without a tag: {len(review['categories_without_a_tag'])}")
        se.print_status(rows, SOURCE)
    except se.Blocked as exc:
        if aligner is not None:
            aligner.finish()
        print(f"\nSTOPPED: {exc}")
        se.print_status(rows, SOURCE)
        return se.EXIT_BLOCKED
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return se.EXIT_ERROR
    return se.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
