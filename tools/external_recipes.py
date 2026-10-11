#!/usr/bin/env python3
"""
Turns a saved recipe web page from an outside site into a record in this library's format, so it
flows through the same ingredient indexing, manifest and exporters as the Mealime/Albertsons
recipes. PROTOTYPE: so far only the Serious Eats page layout (Dotdash Meredith's "mm-recipes"
markup, shared with Allrecipes, Simply Recipes, Food & Wine and others) is understood, and only
from a saved .html file -- crawling/discovering URLs is deliberately a separate, later step.

WHERE THE DATA COMES FROM (a page carries it twice, and neither copy is enough on its own):
  * the schema.org Recipe JSON-LD: name, yield, times, nutrition, categories/cuisine/keywords,
    author, image, description -- but its ingredient list is flat (the same "6 scallions" appears
    for the broth, the oil and the topping with nothing saying which is which).
  * the page's HTML: ingredient SECTIONS ("For the Broth:"), every ingredient already split into
    quantity / unit / name spans, one <li> per step, and the Notes / Special Equipment / Make-Ahead
    text. Section headings and the split are taken from here.

HOW IT MAPS ONTO THE LIBRARY FORMAT (see to_library_record):
  * one record per page; units "US" (the page's metric figures stay in the quantity text, e.g.
    "1 tablespoon (15 ml)"); serving_count from recipeYield; cooking_minutes from totalTime.
  * ingredients -> {"ingredient_id": null, "name", "quantity", plus "note" (the prep text after the
    name, e.g. "split in half"), "section" and "raw" (the original line)}. ASCII fractions in the
    quantity become vulgar fractions ("1/2" -> "½") because the exporters parse a leading number and
    would read "1/2" as 1.
  * instructions -> {"id", "primary_message", "secondary_message": null}; nutrition -> the library's
    keys (energy, fat, saturated, carbs, fiber, sugars, protein, sodium, cholesterol).
  * category / cuisine / keywords go to schema_metadata (the HTML/JSON-LD exporter already reads it).
  * source = the site's name, source_url = the page, author kept as extra fields.
  * allowed_type_ids / violated_restriction_ids / variety_tag_ids are NOT in the page. Defaults are
    set here and flagged in the output -- see DEFAULT_ALLOWED_TYPE_IDS.

Usage:
    python external_recipes.py "Crispy Kimchi Cheese Rice Recipe.html" --out-dir ./scratch_library
    python external_recipes.py page.html --start-id 50000007 --url https://www.seriouseats.com/...

Standard library only.
"""

import argparse
import html as html_lib
import json
import re
import sys
import unicodedata
from pathlib import Path

SOURCES = ("seriouseats",)

# Ids for imported recipes. Every recipe gets a family id from one block starting at 5,000,000 and its Metric
# twin is family id + 71. The block is laid out 71 family ids, then 71 numbers left free for their Metric twins,
# then the next 71 families ...:  5,000,000-5,000,070 (families) | 5,000,071-5,000,141 (their Metric twins) |
# 5,000,142-... so no id is ever another recipe's Metric id. Kept clear of the Mealime/Albertsons ids: those are
# below 1,036,000, and Albertsons Metric twins (family x 10 + 1) start at 10,000,001.
ID_BLOCK_START = 5_000_000
METRIC_ID_OFFSET = 71
ID_LIMIT = 10_000_000


def nth_family_id(n: int) -> int:
    """The n-th (0-based) family id handed out, in the layout described above."""
    family_id = ID_BLOCK_START + (n // METRIC_ID_OFFSET) * 2 * METRIC_ID_OFFSET + n % METRIC_ID_OFFSET
    if family_id + METRIC_ID_OFFSET >= ID_LIMIT:
        raise ValueError(f"the imported-recipe id block is full (id {family_id})")
    return family_id


def family_index(family_id: int) -> int:
    """Inverse of nth_family_id (how many family ids come before this one)."""
    offset = family_id - ID_BLOCK_START
    return (offset // (2 * METRIC_ID_OFFSET)) * METRIC_ID_OFFSET + offset % (2 * METRIC_ID_OFFSET)


def metric_id(family_id: int) -> int:
    return family_id + METRIC_ID_OFFSET


def is_family_id(value: int) -> bool:
    """True for an id in a family slot of the block (not a Metric slot, not outside the block)."""
    offset = value - ID_BLOCK_START
    return offset >= 0 and offset % (2 * METRIC_ID_OFFSET) < METRIC_ID_OFFSET and value + METRIC_ID_OFFSET < ID_LIMIT


SITE_NUMBER_RE = re.compile(r"-recipe-(\d{6,})$")


def site_number(url: str) -> int | None:
    """The numeric page id some Serious Eats URLs end in ('...-recipe-12168126'); older URLs have none."""
    m = SITE_NUMBER_RE.search(url)
    return int(m.group(1)) if m else None
# Classic + Flexitarian: what every recipe satisfies. Without a 1 here the exporter's mandatory
# Classic menu would drop the recipe from every export. Vegetarian/Vegan etc. can't be read from the
# page, so they are left out rather than guessed.
DEFAULT_ALLOWED_TYPE_IDS = [1, 3]

VULGAR = {(1, 2): "½", (1, 3): "⅓", (2, 3): "⅔", (1, 4): "¼", (3, 4): "¾", (1, 5): "⅕", (2, 5): "⅖",
          (3, 5): "⅗", (4, 5): "⅘", (1, 6): "⅙", (5, 6): "⅚", (1, 8): "⅛", (3, 8): "⅜", (5, 8): "⅝",
          (7, 8): "⅞"}


# --- small text helpers -----------------------------------------------------------------------

def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def fraction_to_vulgar(match: re.Match) -> str:
    num, den = int(match.group(1)), int(match.group(2))
    return VULGAR.get((num, den)) or (f"{num / den:.3f}".rstrip("0").rstrip(".") if den else match.group(0))


def normalize_quantity(text: str) -> str:
    """'1 1/2' -> '1 ½', '1/16' -> '0.062', leaving everything else (ranges, parentheses) untouched."""
    text = re.sub(r"(?<![\d/])(\d+)/(\d+)(?![\d/])", fraction_to_vulgar, text)
    return re.sub(r"\s+", " ", text).strip()


def iso_minutes(duration, longest: bool = False) -> int | None:
    """Minutes from an ISO-8601 duration. Recipes with long rests give a range object instead of a string
    ({"@type": "Duration", "minValue": "PT105M", "maxValue": "PT1485M"}); the shortest figure is used unless
    longest=True."""
    if isinstance(duration, dict):
        low, high = duration.get("minValue"), duration.get("maxValue")
        duration = (high or low) if longest else (low or high)
    if not duration or not isinstance(duration, str):
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", duration.strip())
    if not m or not any(m.groups()):
        return None
    days, hours, minutes = (int(g) if g else 0 for g in m.groups())
    return days * 1440 + hours * 60 + minutes


MAX_UNSPLIT_TOTAL_MINUTES = 90   # a lone totalTime above this is mostly waiting, not work


def active_minutes(parsed: dict) -> int | None:
    """Hands-on time, which is what the library's cooking_minutes means (median 35, 99% <= 60): prep + cook
    when the page gives either, else totalTime only if it is short enough not to be mostly waiting."""
    prep, cook = parsed.get("prep_minutes"), parsed.get("cook_minutes")
    if prep or cook:
        return (prep or 0) + (cook or 0)
    total = parsed.get("total_minutes")
    return total if total is not None and total <= MAX_UNSPLIT_TOTAL_MINUTES else None


def format_duration(minutes: int) -> str:
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} day{'s' if days != 1 else ''}" if days else "", f"{hours} hr" if hours else "", f"{mins} min" if mins else ""]
    return " ".join(p for p in parts if p) or "0 min"


def total_time_note(parsed: dict) -> str | None:
    """'Total time, including resting: ...' when the whole recipe takes clearly longer than the hands-on time."""
    low, high = parsed.get("total_minutes"), parsed.get("total_minutes_max")
    active = active_minutes(parsed)
    if not low or (active is not None and low - active < 30):
        return None
    if active is None and low <= MAX_UNSPLIT_TOTAL_MINUTES:
        return None
    span = format_duration(low) if not high or high == low else f"{format_duration(low)} to {format_duration(high)}"
    return f"Total time, including resting: {span}."


def first_number(value) -> int | None:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else None


def number_with_unit(value: str | None) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else None


def slugify(text: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))   # Albóndigas -> Albondigas
    text = re.sub(r"['’]", "", text.lower())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


# --- reading the page -------------------------------------------------------------------------

def load_recipe_jsonld(page: str) -> dict:
    for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else data.get("@graph", [data])):
            kind = item.get("@type")
            if kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind):
                return item
    raise ValueError("no schema.org Recipe found in the page")


INGREDIENT_SPAN = re.compile(r'<span[^>]*data-ingredient-(quantity|unit|name)="true"[^>]*>(.*?)</span>', re.S)
# A name starting with one of these is really "<count word> <ingredient>" ("cloves garlic"), where the
# library keeps the count word with the quantity ("2 cloves") and the bare ingredient as the name.
COUNT_WORDS = {"clove", "cloves", "head", "heads", "sprig", "sprigs", "stalk", "stalks", "slice", "slices",
               "piece", "pieces", "leaf", "leaves", "strip", "strips", "can", "cans", "bunch", "bunches",
               "pinch", "pinches", "dash", "dashes", "knob", "knobs", "segment", "segments", "rib", "ribs",
               "sheet", "sheets", "stick", "sticks", "stem", "stems", "wedge", "wedges", "ear", "ears",
               "fillet", "fillets", "package", "packages", "pkg", "jar", "jars", "bulb", "bulbs", "round", "rounds"}


def raw_text(fragment: str) -> str:
    """Tags removed and entities decoded, but spacing left alone (strip_tags collapses/trims it)."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", fragment))


def parse_ingredient_item(li_html: str) -> dict:
    """One <li> of the structured ingredient list -> quantity text, name, prep note, original line.

    The page marks up quantity / unit / name spans, with plain text in between. Plain text with
    letters that sits among the quantity and unit spans ("4 thinly sliced medium scallions") is
    really part of the name, so it moves there; plain punctuation stays in the quantity ("1 (4-inch)").
    """
    p = re.search(r"<p[^>]*>(.*?)</p>", li_html, re.S)
    inner = p.group(1) if p else li_html
    raw = strip_tags(inner)

    tokens, pos = [], 0
    for m in INGREDIENT_SPAN.finditer(inner):
        if m.start() > pos:
            tokens.append(("plain", raw_text(inner[pos:m.start()])))
        tokens.append((m.group(1), raw_text(m.group(2))))
        pos = m.end()
    tokens.append(("plain", raw_text(inner[pos:])))

    name_idx = [i for i, (kind, _) in enumerate(tokens) if kind == "name"]
    if not name_idx:                    # no marked-up name: keep the whole line as the name
        return {"quantity": "", "name": raw, "note": None, "raw": raw}
    first, last = name_idx[0], name_idx[-1]
    has_measure = any(kind in ("quantity", "unit") for kind, _ in tokens[:first])

    quantity_parts, name_prefix, lead_note = [], [], None
    for kind, text in tokens[:first]:
        if kind in ("quantity", "unit"):
            quantity_parts.append(text)
        elif re.search(r"[A-Za-z]", text):
            if has_measure:
                name_prefix.append(text.strip())
                quantity_parts.append(" ")  # keep "4" and "medium" apart once the words between are moved out
            else:                       # e.g. "Caps and tender stems from 6 ounces ... (above)": a back-reference
                lead_note = " ".join(text.split())
        else:
            quantity_parts.append(text)
    name = " ".join(name_prefix + [" ".join("".join(t for _, t in tokens[first:last + 1]).split())])
    quantity = re.sub(r"\s+", " ", "".join(quantity_parts))
    quantity = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", quantity)).strip()

    words = name.split()
    if len(words) > 1 and words[0].lower() in COUNT_WORDS:
        quantity, name = f"{quantity} {words[0]}".strip(), " ".join(words[1:])

    tail = "".join(t for _, t in tokens[last + 1:])
    note = " ".join(x for x in (lead_note, " ".join(tail.split()).lstrip(",; ").strip()) if x) or None
    return {"quantity": normalize_quantity(quantity), "name": name, "note": note, "raw": raw}


def parse_ingredient_sections(page: str) -> list[dict]:
    """[{'section': 'For the Broth' | None, 'items': [...]}, ...] in page order."""
    start = page.find("mm-recipes-structured-ingredients__heading")
    if start < 0:
        raise ValueError("no structured ingredient list found in the page")
    end = page.find("mm-recipes-steps__heading", start)
    block = page[start:end if end > 0 else len(page)]
    token = re.compile(r'<p[^>]*structured-ingredients__list-heading[^>]*>(.*?)</p>'
                       r'|<ul[^>]*structured-ingredients__list"[^>]*>(.*?)</ul>', re.S)
    sections, pending_heading = [], None
    for m in token.finditer(block):
        if m.group(1) is not None:
            pending_heading = strip_tags(m.group(1)).rstrip(":").strip() or None
            continue
        items = [parse_ingredient_item(li) for li in
                 re.findall(r'<li[^>]*structured-ingredients__list-item[^>]*>(.*?)</li>', m.group(2), re.S)]
        sections.append({"section": pending_heading, "items": items})
        pending_heading = None
    return sections


def parse_steps(page: str) -> list[str]:
    start = page.find("mm-recipes-steps__content")
    if start < 0:
        raise ValueError("no directions found in the page")
    block = page[start:]
    steps = []
    for li in re.findall(r"<li[^>]*mntl-sc-block-group--LI[^>]*>(.*?)</li>", block, re.S | re.I):
        paragraphs = [strip_tags(p) for p in re.findall(r"<p[^>]*mntl-sc-block-html[^>]*>(.*?)</p>", li, re.S | re.I)]
        text = "\n".join(p for p in paragraphs if p)
        if text:
            steps.append(text)
    return steps


def parse_text_sections(page: str) -> dict[str, str]:
    """Plain-text sections after the directions ('Special Equipment', 'Notes', 'Make-Ahead and Storage')."""
    out = {}
    start = page.find("mm-recipes-steps__content")
    area = page[start:] if start >= 0 else page
    # Each section is <h2 class="...mntl-sc-block-heading"><span ...>Title</span></h2> followed by
    # <p class="...mntl-sc-block-html"> paragraphs, up to the next heading.
    headings = list(re.finditer(r'<h2[^>]*mntl-sc-block-heading[^>]*>(.*?)</h2>', area, re.S | re.I))
    for i, m in enumerate(headings):
        title = strip_tags(m.group(1))
        if title not in ("Special Equipment", "Notes", "Make-Ahead and Storage"):
            continue
        stop = headings[i + 1].start() if i + 1 < len(headings) else m.end() + 6000
        paragraphs = [strip_tags(x) for x in
                      re.findall(r"<p[^>]*mntl-sc-block-html[^>]*>(.*?)</p>", area[m.end():stop], re.S | re.I)]
        text = "\n".join(p for p in paragraphs if p)
        if text:
            out[title] = text
    return out


def parse_nutrition(node: dict | None) -> dict:
    node = node or {}
    mapping = {"calories": "energy", "fatContent": "fat", "saturatedFatContent": "saturated",
               "carbohydrateContent": "carbs", "fiberContent": "fiber", "sugarContent": "sugars",
               "proteinContent": "protein", "sodiumContent": "sodium", "cholesterolContent": "cholesterol",
               "transFatContent": "transfats"}
    out = {}
    for src, key in mapping.items():
        value = number_with_unit(node.get(src))
        if value is not None:
            out[key] = value
    return out


def parse_page(page: str, url: str | None = None) -> dict:
    ld = load_recipe_jsonld(page)
    entity = ld.get("mainEntityOfPage")
    page_url = url or (entity.get("@id") if isinstance(entity, dict) else entity if isinstance(entity, str) else None)
    authors = ld.get("author") or []
    if isinstance(authors, dict):
        authors = [authors]
    image = ld.get("image")
    image_url = image.get("url") if isinstance(image, dict) else (image[0] if isinstance(image, list) and image else image)
    agg = ld.get("aggregateRating") if isinstance(ld.get("aggregateRating"), dict) else None
    try:
        rating = (float(agg["ratingValue"]), int(float(agg.get("ratingCount") or agg.get("reviewCount") or 0))) if agg else None
    except (KeyError, TypeError, ValueError):
        rating = None
    return {
        "rating": rating,                  # (value out of 5, number of ratings) or None
        "name": re.sub(r"\s+Recipe$", "", html_lib.unescape(ld.get("name") or ld.get("headline") or "")).strip(),
        "description": html_lib.unescape(ld.get("description") or "") or None,
        "url": page_url,
        "authors": [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")],
        "servings": first_number(ld.get("recipeYield")),
        "total_minutes": iso_minutes(ld.get("totalTime")),
        "total_minutes_max": iso_minutes(ld.get("totalTime"), longest=True),
        "prep_minutes": iso_minutes(ld.get("prepTime")),
        "cook_minutes": iso_minutes(ld.get("cookTime")),
        "categories": ld.get("recipeCategory") or [],
        "cuisines": ld.get("recipeCuisine") or [],
        "keywords": ld.get("keywords"),
        "nutrition": parse_nutrition(ld.get("nutrition")),
        "image_url": image_url,
        "ingredient_sections": parse_ingredient_sections(page),
        "steps": parse_steps(page),
        "extra_sections": parse_text_sections(page),
        "jsonld_ingredient_count": len(ld.get("recipeIngredient") or []),
        "jsonld_step_count": len(ld.get("recipeInstructions") or []),
    }


# --- building the library record --------------------------------------------------------------

def to_library_record(parsed: dict, record_id: int, source: str = "seriouseats") -> dict:
    ingredients = []
    for section in parsed["ingredient_sections"]:
        for item in section["items"]:
            entry = {"ingredient_id": None, "name": item["name"], "quantity": item["quantity"]}
            if item["note"]:
                entry["note"] = item["note"]
            if section["section"]:
                entry["section"] = section["section"]
            entry["raw"] = item["raw"]
            ingredients.append(entry)
    name = parsed["name"]
    return {
        "id": record_id,
        "recipe_id": record_id,
        "name": name,
        "slug": slugify(name),
        "source_url": parsed["url"],
        "canonical_variant_url": None,
        "serving_count": parsed["servings"],
        "cooking_minutes": active_minutes(parsed),
        "units": "US",
        "group_label": "Default",
        "position": 0,
        "unit_family_id": 2,
        "is_pro": None,
        "average_rating": None,
        "rating_count": 0,
        "allowed_type_ids": list(DEFAULT_ALLOWED_TYPE_IDS),
        "violated_restriction_ids": None,
        "variety_tag_ids": [],
        "cuisine_tag_ids": [],
        "recipe_category_id": None,
        "kid_friendly": False,
        "cookwares": [],
        "ingredients": ingredients,
        "instructions": [{"id": i + 1, "primary_message": text, "secondary_message": None}
                         for i, text in enumerate(parsed["steps"])],
        "nutrition": parsed["nutrition"],
        "schema_metadata": {"category": parsed["categories"], "cuisine": parsed["cuisines"],
                            "keywords": parsed["keywords"]},
        "sibling_variant_ids": [],
        "family_dir": f"families/{record_id}",
        "source": source,
        "author": parsed["authors"],
        "description": parsed["description"],
        "thumbnail_image_url": parsed["image_url"],
        "notes": parsed["extra_sections"] or None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("html_files", nargs="+", type=Path, help="Saved recipe page(s) (.html)")
    parser.add_argument("--out-dir", type=Path, required=True, help="Library root to write families/<id>/ into")
    parser.add_argument("--source", default="seriouseats", choices=SOURCES)
    parser.add_argument("--start-id", type=int, default=None, help=f"First id to use (default: {ID_BLOCK_START})")
    parser.add_argument("--url", default=None, help="The page's URL (single file only; else read from the page)")
    args = parser.parse_args()

    next_id = args.start_id or ID_BLOCK_START
    for path in args.html_files:
        parsed = parse_page(path.read_text(encoding="utf-8", errors="replace"), args.url)
        record = to_library_record(parsed, next_id, args.source)
        fdir = args.out_dir / "families" / str(next_id)
        fdir.mkdir(parents=True, exist_ok=True)
        out = fdir / f"{next_id}_{record['slug']}.json"
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{next_id}: {record['name']} -> {out}")
        next_id = nth_family_id(family_index(next_id) + 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
