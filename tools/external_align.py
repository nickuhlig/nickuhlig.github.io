#!/usr/bin/env python3
"""
Aligns a recipe parsed from another site (external_recipes.parse_page) with THIS library's record
format, so it enters the library looking like every other recipe and the exporters, menu selection,
restriction filters and web page need no special cases. It runs during the fetch (seriouseats_scraper.py
--align); nothing downstream has to know where a recipe came from.

WHAT IT PRODUCES  one family = two records (the library's usual US/Metric pair):
    US      id = family id (the registry id)        units "US"      unit_family_id 2   position 0
    Metric  id = family id + 71                     units "Metric"  unit_family_id 1   position 1
  (family ids come from the 5,000,000 block described in external_recipes.py; +71 never lands on another id)
  with the same fields as an Albertsons-native record, plus a few the exporters pass through when present
  (description, notes) and some that are just kept (author, thumbnail_image_url).

UNITS, matching Mealime's own Metric recipes (measured on the library's real US/Metric pairs):
  * spoons and cups stay (tsp / tbsp / cup), spelled the library's way ("1 tablespoon" -> "1 tbsp");
  * lb -> kg (two decimals), oz -> g (whole), fl oz -> ml (whole), inch -> cm (nearest half);
  * when the page gives its own metric figure in brackets ("6 ounces (170 g)") that figure is used; the
    conversion only fills the gaps -- so no invented number ever overrides the author's;
  * US recipes carry NO metric figures (the bracketed metric is removed, in ingredients and in directions);
  * temperatures: US "425°F"; Metric "220°C" -- but OVEN temperatures keep the Fahrenheit in brackets in the
    Metric recipe too, "220°C (425°F)", because ovens in metric countries are often still Fahrenheit.

INGREDIENTS  each page line is parsed from its text (the page's own span markup is unreliable: it tags
  "apple," as a name and "peeled," as a unit) into  quantity | name | prep  and then
  * short prep text is folded into the name after a comma, as Mealime does ("onion, finely sliced"); long
    prep and cross-references are dropped (the directions cover them);
  * optional / serving-suggestion lines ("... for serving", "as garnish", "optional") are NOT ingredients:
    they go to `notes`, so they never affect restrictions or diet types ("plus more ..." lines are real
    ingredients and stay);
  * `ingredient_id` is set HERE, on the clean name, against ingredient_master.json (new names are added
    with violates null for you to decide); "A or B" lines keep the FIRST as the ingredient and list the rest
    in the notes ("Optional substitutions: A → B"), so substitutions never affect restrictions;
  * a count of sized pieces ("Four 5- to 6-ounce thighs") becomes one weight (the high end of a range times the
    count); packages (cans, jars...) keep "2 (28 oz) cans". Any weight of 16 oz or more is written in pounds, to
    the nearest 1/4 lb;
  * section headings are dropped (the directions carry labels such as "Make the Broth:").

Everything that needed a judgement call is recorded in the review report (external_review_report.json).
"""

import csv
import dataclasses
import json
import math
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from external_recipes import active_minutes, metric_id, parse_page, slugify, total_time_note
from fetch_albertsons_native_recipes import format_quantity_number
from ingredient_master import IngredientMaster, load_master, master_path_for, name_key, protected_qualifiers, save_master

SOURCE = "seriouseats"
VULGAR = {"½": .5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": .25, "¾": .75, "⅕": .2, "⅖": .4, "⅗": .6, "⅘": .8,
          "⅙": 1 / 6, "⅚": 5 / 6, "⅛": .125, "⅜": .375, "⅝": .625, "⅞": .875}
VG = "".join(VULGAR)
NUM = rf"(?:\d+\s+\d+/\d+|\d+/\d+|\d+\s*[{VG}]|[{VG}]|\d+(?:\.\d+)?)"
RANGE_SEP = r"\s*(?:-\s*to|to|-|–|—)\s*"

# --- grams / ml per imperial unit (the factors Mealime's own Metric recipes use) --------------------------
G_PER_OZ, G_PER_LB, ML_PER_FL_OZ, CM_PER_INCH = 28.3495, 453.592, 29.5735, 2.54
ML_PER = {"quart": 946.353, "pint": 473.176, "gallon": 3785.41}

UNIT_MAP = {
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp", "tbsps": "tbsp", "tbs": "tbsp", "tbl": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsp": "tsp", "tsps": "tsp",
    "cup": "cup", "cups": "cup",
    "ounce": "oz", "ounces": "oz", "oz": "oz",
    "pound": "lb", "pounds": "lb", "lb": "lb", "lbs": "lb",
    "gram": "g", "grams": "g", "g": "g", "kilogram": "kg", "kilograms": "kg", "kg": "kg",
    "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml", "ml": "ml",
    "liter": "l", "liters": "l", "litre": "l", "litres": "l", "l": "l",
    "quart": "quart", "quarts": "quart", "pint": "pint", "pints": "pint", "gallon": "gallon", "gallons": "gallon",
    "inch": "inch", "inches": "inch", "cm": "cm", "centimeter": "cm", "centimeters": "cm",
}
SPELLED_OUT = {"tbsp", "tsp", "cup", "oz", "lb", "quart", "pint", "gallon", "inch"}   # US units that may need converting
SIZE_WORDS = {"small", "medium", "large", "extra-large", "big", "generous"}   # "whole" is handled separately: "2 whole heads" vs "whole peeled tomatoes"
SIZE_RE = r"small|medium|large|extra[- ]large|jumbo|big"
# words that only describe (no noun): a name made of nothing else is really the start of "adjectives, noun"
LEADING_ADJECTIVES = {"cubed", "sweet", "ripe", "fresh", "firm", "soft", "hard", "hot", "mild", "spicy", "bitter", "sour", "crisp", "crunchy",
                      "tender", "juicy", "sliced", "chopped", "diced", "minced", "shredded", "grated", "dried", "raw", "cooked", "frozen",
                      "large", "small", "medium", "thick", "thin", "lean", "fatty", "young", "baby", "mini", "whole", "toasted", "roasted"}
# (singular, plural) the library's way: "1 clove", "2 cloves"
COUNT_FORMS = {}
for _s, _p in [("clove", "cloves"), ("head", "heads"), ("sprig", "sprigs"), ("stalk", "stalks"), ("slice", "slices"),
               ("piece", "pieces"), ("leaf", "leaves"), ("strip", "strips"), ("can", "cans"), ("bunch", "bunches"),
               ("pinch", "pinches"), ("dash", "dashes"), ("knob", "knobs"), ("segment", "segments"), ("rib", "ribs"),
               ("sheet", "sheets"), ("stick", "sticks"), ("stem", "stems"), ("wedge", "wedges"), ("ear", "ears"),
               ("fillet", "fillets"), ("pkg", "pkgs"), ("package", "pkgs"), ("jar", "jars"), ("bulb", "bulbs"),
               ("round", "rounds"), ("handful", "handfuls"), ("bottle", "bottles"), ("bag", "bags"), ("box", "boxes"),
               ("container", "containers"), ("cube", "cubes"), ("drop", "drops"), ("serving", "servings"),
               ("scoop", "scoops"), ("shot", "shots"), ("block", "blocks"), ("envelope", "envelopes"), ("packet", "packets"),
               ("dozen", "dozen"), ("loaf", "loaves"), ("rack", "racks"), ("portion", "portions")]:
    COUNT_FORMS[_s] = (_s if _s != "package" else "pkg", _p)
    COUNT_FORMS[_p] = COUNT_FORMS[_s]
ADVERB_OR_PREP_WORDS = {"sliced", "chopped", "diced", "minced", "grated", "shredded", "crushed", "peeled", "trimmed",
                        "halved", "quartered", "cubed", "torn", "cored", "seeded", "drained", "rinsed", "thinly", "thickly",
                        "finely", "roughly", "coarsely", "very", "freshly", "lightly", "firmly", "loosely", "skin-on",
                        "boneless", "skinless", "fresh", "ripe", "cooked", "raw", "frozen", "packed", "heaping", "level"}


# --- number helpers ----------------------------------------------------------------------------------------------

def parse_number(text: str) -> float | None:
    text = text.strip()
    total, ok = 0.0, False
    for tok in re.findall(rf"\d+/\d+|\d+(?:\.\d+)?|[{VG}]", text):
        if "/" in tok:
            a, b = tok.split("/")
            total += int(a) / int(b)
        elif tok in VULGAR:
            total += VULGAR[tok]
        else:
            total += float(tok)
        ok = True
    return total if ok else None


def fmt_number(n: float) -> str:
    return format_quantity_number(n)


def fmt_range(lo: float, hi: float | None) -> str:
    return fmt_number(lo) if hi is None else f"{fmt_number(lo)} to {fmt_number(hi)}"


def round_half_cm(cm: float) -> float:
    return round(cm * 2) / 2


# --- metric formatting (Mealime's conventions) ------------------------------------------------------------------------

def g_text(grams: float) -> str:
    return f"{round(grams / 1000, 2):g} kg" if grams >= 1000 else f"{round(grams):g} g"


def kg_text(grams: float) -> str:
    return f"{round(grams / 1000, 2):g} kg"


def ml_text(ml: float) -> str:
    return f"{round(ml):g} ml"


def cm_text(cm: float) -> str:
    return f"{fmt_number(round_half_cm(cm))} cm"


METRIC_RE = re.compile(r"(?:~|about\s+|approximately\s+|approx\.?\s+)?(\d+(?:\.\d+)?)\s*(kg|g|ml|l|cm|mm|grams?|kilograms?|milliliters?|millilitres?|liters?|litres?)\b", re.I)
METRIC_UNIT = {"kg": "kg", "kilogram": "kg", "kilograms": "kg", "g": "g", "gram": "g", "grams": "g", "ml": "ml", "milliliter": "ml",
               "milliliters": "ml", "millilitre": "ml", "millilitres": "ml", "l": "l", "liter": "l", "liters": "l", "litre": "l",
               "litres": "l", "cm": "cm", "mm": "mm"}


def metric_part(text: str) -> tuple[float, str] | None:
    """The metric measure inside a bracket's text ("3-ounce; 85 g" -> (85, 'g'); '15 ml' -> (15, 'ml'))."""
    m = METRIC_RE.search(text)
    return (float(m.group(1)), METRIC_UNIT[m.group(2).lower()]) if m else None


IMPERIAL_IN_PAREN = re.compile(rf"({NUM})\s*-?\s*(fluid ounces?|fl\.? ?oz\.?|ounces?|oz\.?|pounds?|lbs?\.?|inch(?:es)?|cups?|quarts?|pints?)\b", re.I)


def imperial_part(text: str) -> tuple[float, str] | None:
    """The imperial measure inside a bracket's text ("28-ounce" -> (28, 'oz'))."""
    m = IMPERIAL_IN_PAREN.search(text)
    if not m:
        return None
    word = re.sub(r"[.\s]", "", m.group(2).lower())
    unit = "fl oz" if word.startswith("fl") or word.startswith("fluid") else UNIT_MAP.get(word.rstrip("s"), UNIT_MAP.get(word))
    return (parse_number(m.group(1)) or 0.0, unit) if unit else None


def metric_value_to_library(value: float, unit: str, us_unit: str) -> str:
    """The author's metric figure, written in the unit Mealime uses for that kind of measure."""
    grams = value * 1000 if unit == "kg" else value if unit == "g" else None
    ml = value * 1000 if unit == "l" else value if unit == "ml" else None
    if unit == "cm":
        return cm_text(value)
    if unit == "mm":
        return cm_text(value / 10)
    if us_unit == "lb" and grams is not None:
        return kg_text(grams)
    if grams is not None:
        return g_text(grams)
    return ml_text(ml)


def convert_us_to_metric(amount: float, unit: str) -> str | None:
    """Table fallback for the imperial units Mealime converts; None for anything else."""
    if unit == "lb":
        return kg_text(amount * G_PER_LB)
    if unit == "oz":
        return g_text(amount * G_PER_OZ)
    if unit == "fl oz":
        return ml_text(amount * ML_PER_FL_OZ)
    if unit in ML_PER:
        return ml_text(amount * ML_PER[unit])
    if unit == "inch":
        return cm_text(amount * CM_PER_INCH)
    return None


# --- ingredient line parsing --------------------------------------------------------------------------------------------

AMOUNT_RE = re.compile(rf"^(?P<a>{NUM})(?:{RANGE_SEP}(?P<b>{NUM}))?(?=\s|\(|$)")
TOKEN_RE = re.compile(r"\([^)]*\)|[^\s()]+")
OPTIONAL_RE = re.compile(r"\boptional\b", re.I)
SERVING_RE = re.compile(r"\b(?:for|as(?:\s+an?)?|to)\s+(?:serv(?:ing|e)|garnish(?:ing)?|top(?:ping)?)\b", re.I)
PLUS_MORE_RE = re.compile(r"\bplus\s+(more|extra|additional)\b", re.I)
A_PINCH_RE = re.compile(r"^(?:(?:a|an|one)\s+)?(pinch|dash|handful|splash|drizzle)(?:\s+of)?\s+", re.I)   # "a pinch of salt", "Pinch kosher salt"
CROSS_REF_RE = re.compile(r"\bmade through step \d+\b|\(\s*(?:see|below|above)[^)]*\)|\b(?:see|noted?)\s+(?:note|notes|below|above|recipe)\b|\((?:above|below)\)", re.I)


# "Canola, vegetable, or olive oil" / "basil, cilantro, and/or mint leaves": a comma list of alternatives (no digits, no
# prepared-ingredient words like "peeled"/"diced"), which would otherwise be cut at its first comma
_LIST_ITEM = r"(?![^,]*\b\w{3,}(?:ed|ly)\b)[A-Za-z][A-Za-z' -]{1,30}?"
# preparation described after the name without a comma: "onion grated on a microplane grater", "potatoes peeled and cut into chunks"
PREPARED_TAIL = (r"\s+(?:(?:grated|peeled|trimmed|cored|seeded|halved|quartered|cubed|crushed|drained|rinsed|thawed|softened|beaten|sifted)"
                 r"\s+(?:on|and|into|in|at|to|with|lengthwise|crosswise|if)|cut\s+(?:into|in|at|to)|"
                 r"(?:stirred|mixed|whisked|dissolved|combined|blended)\s+(?:in|into|with)|dissolved\b)\b.*$")
LIST_OF_ALTERNATIVES_RE = re.compile(rf"^((?:{_LIST_ITEM}, ){{1,4}}(?:and/or |or ){_LIST_ITEM}(?= *(?:,|$)))( ?.*)$")
GARNISH_LINE_RE = re.compile(r"^\s*(?:garnish(?:es)?\s*:|toppings?\b)|\bof your choice\b|\bany of the following\b|\bcomposed of\b", re.I)
# "Garnish: mint sprig", "Toppings of your choice, ...", "Vegetables of your choice"


def is_optional_line(raw: str) -> bool:
    if OPTIONAL_RE.search(raw) or GARNISH_LINE_RE.search(raw):
        return True
    return bool(SERVING_RE.search(raw)) and not PLUS_MORE_RE.search(raw)


NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
                "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}
NUMBER_WORD_RE = "|".join(NUMBER_WORDS)
PRODUCT_ATTRIBUTES = r"boneless|skinless|bone-in|skin-on"
PLUS_AMOUNT_RE = re.compile(rf"(?:,\s*|\s+)plus\s+(?=(?:\d|[{VG}]|a\s|an\s|(?:{NUMBER_WORD_RE})\s))", re.I)


def normalize_raw(raw: str) -> str:
    """Spelled-out counts and package sizes, as the page wrote them, into the forms the parser reads:
    'Two (12-ounce; 334 g) pork chops' -> '2 (12-ounce; 334 g) pork chops';
    'One 16-ounce can beans' -> '1 (16-ounce) can beans'; 'Four 5- to 6-ounce each thighs' -> '4 (5- to 6-ounce) thighs';
    'boneless, skinless' / 'bone-in, skin-on' pairs lose their comma so they are not read as 'name, prep'."""
    s = " ".join(raw.replace("\xa0", " ").split())
    s = re.sub(rf"^(?:about|approximately|roughly|scant|generous|heaping|rounded|a scant|a generous|just over|just under)\s+(?=\d|[{VG}])", "", s, flags=re.I)
    s = re.sub(rf"(?<=[\d/{VG}])\s+(?:scant|heaping|generous|rounded|level)\s+(?=(?:tablespoons?|teaspoons?|cups?)\b)", " ", s, flags=re.I)
    s = re.sub(rf"^({NUMBER_WORD_RE})\b(?=\s+[\d(a-z])", lambda m: str(NUMBER_WORDS[m.group(1).lower()]), s, flags=re.I)
    s = re.sub(r"^(?:and|to|plus)\s+(?=\d)", "", s, flags=re.I)                           # "and 1 teaspoon Cajun seasoning"
    s = re.sub(rf"^half of ({NUM})\s+", lambda m: f"{fmt_number((parse_number(m.group(1)) or 0) / 2)} ", s, flags=re.I)   # "Half of 1 yellow onion"
    s = re.sub(rf"^(\d+)\s+pounds?\s+({NUM})\s+ounces?\b",                                  # "1 pound 10.5 ounces" -> "26.5 ounces"
               lambda m: f"{int(m.group(1)) * 16 + (parse_number(m.group(2)) or 0):g} ounces", s, flags=re.I)
    s = re.sub(r"^(\d+(?:\.\d+)?)(g|kg|ml)\b", r"\1 \2", s)                              # "555g flour" -> "555 g flour"
    s = re.sub(r"\((\d+(?:\.\d+)?)\)\s?(g|kg|ml)\b", r"(\1 \2)", s)                       # "8 tablespoons (113)g butter" -> "(113 g)"
    s = re.sub(r"\b(\w+(?: \w+)?) \(\d[^)]*\) \1\b", r"\1", s)                            # "unsalted butter (113 g) unsalted butter": the name said twice
    s += ")" * max(0, s.count("(") - s.count(")"))                                          # "(about 8 ounces each, ends trimmed" -> a missing closing bracket
    s = re.sub(rf"^({NUM})\s+({PROSE_UNIT})\s+to\s+({NUM})\s+\2\b", r"\1 to \3 \2", s, flags=re.I)   # "1 1/2 teaspoons to 3 teaspoons" -> "1 1/2 to 3 teaspoons"
    s = re.sub(rf"^({NUM})\s+of\s+(?:an?|the)\s+", r"\1 ", s)                              # "1/2 of a jalapeño" -> "1/2 jalapeño"
    s = re.sub(rf"^({NUM})\s*[-–]\s*(inch|ounce|pound)s?\b(?=\s+(?:\([^)]*\)\s*)?(?:knob|piece|block|chunk|slab|stick|strip|segment)\b)",
               lambda m: f"1 ({m.group(1)}-{m.group(2)})", s, flags=re.I)                    # "1/2-inch knob ginger" -> "1 (1/2-inch) knob ginger"
    size = rf"{NUM}(?:\s*-?\s*(?:to|by)\s*{NUM})?"                                            # "5- to 6", "3- by 1", "1 1/2"
    s = re.sub(rf"^(\d+)\s+(?:(?:roughly|about|approximately|nearly)\s+)?((?:whole|large|small|medium)\s+)?({size})\s*[-–]\s*(ounces?|pounds?|inch(?:es)?)\b(?:\s+each)?",
               lambda m: f"{m.group(1)} ({m.group(3)}-{m.group(4)}) {m.group(2) or ''}".rstrip() + " ", s, flags=re.I)
    s = re.sub(rf"^((?:whole\s+)?)({size})\s*[-–]\s*(ounces?|pounds?)\b(?!\s*\))",                  # "12-ounce English cucumber", "1-pound loaf soft Italian"
               lambda m: f"1 ({m.group(2)}-{m.group(3)}) {m.group(1)}".rstrip() + " ", s, flags=re.I)
    s = re.sub(r"\b\d{2,3}%\s*(?=lean|juice|pure|natural)", "", s, flags=re.I)                    # "80% lean ground beef", "100% pineapple juice"
    s = re.sub(rf"\b({PRODUCT_ATTRIBUTES}),\s+(?=(?:{PRODUCT_ATTRIBUTES})\b)", r"\1 ", s, flags=re.I)
    return s


def split_plus_clause(raw: str) -> list[str]:
    """'1 chipotle chile, chopped, plus 1 tablespoon adobo sauce' -> two lines. A 'plus' that only adds more of the
    same ingredient ('1 whole bun, plus 1 bottom bun'; 'plus more to taste') stays one line."""
    m = next((x for x in PLUS_AMOUNT_RE.finditer(raw) if raw[:x.start()].count("(") <= raw[:x.start()].count(")")), None)   # not inside brackets
    if not m:
        return [raw]
    before, after = raw[:m.start()].strip(), raw[m.end():].strip()
    first, second = parse_line(before), parse_line(after)
    words_first = set(re.findall(r"[a-z]+", first.name.lower()))
    words_second = re.findall(r"[a-z]+", second.name.lower())
    if not second.name or re.match(r"(?:for|to|if|as|so)\b", second.name.lower()):
        return [raw]
    if words_second and words_second[-1] in words_first and not FRUIT_SOURCE_RE.match(second.name):
        return [raw]
    return [before, after]


@dataclass
class Line:
    raw: str
    amount: float | None = None
    amount_hi: float | None = None
    unit: str | None = None
    parens: list = field(default_factory=list)        # bracket texts that follow the amount, in order
    trailing: list = field(default_factory=list)      # bracket texts after the name starts ("(about 1 cup)", "(1 pound)")
    alts: list = field(default_factory=list)          # alternatives that carry their own amount ("or four 8-ounce bone-in thighs")
    extra: str = ""                                   # a trailing part of the amount: "minus 1 teaspoon", "plus 1 tablespoon"
    fixed_us: str | None = None                       # a quantity written by hand (external_line_overrides.csv)
    fixed_metric: str | None = None
    tail: list = field(default_factory=list)          # size / count words ("medium", "cloves")
    name: str = ""                                    # clean name, no prep
    prep: str | None = None


def _is_prefix_word(tok: str) -> bool:
    bare = tok.strip(".,;:").lower()
    return bare in ADVERB_OR_PREP_WORDS or bare.endswith("ly")


def parse_line(raw: str) -> Line:
    s = normalize_raw(raw)
    line = Line(raw=s)
    pinch = A_PINCH_RE.match(s)
    if pinch:
        line.amount, line.tail = 1.0, [pinch.group(1).lower()]
        s = s[pinch.end():]
        tokens = TOKEN_RE.findall(s)
        i = 0
    else:
        minus = re.match(rf"^({NUM}\s+(?:{PROSE_UNIT}))\s+(minus\s+{NUM}\s+(?:{PROSE_UNIT}))\b", s, re.I)    # "1/4 cup minus 1 teaspoon (55 g) milk"
        if minus:
            s = s[:minus.start(2)] + s[minus.end(2):]
            line.extra = re.sub(r"\s+", " ", localize_units_short(minus.group(2)))
        m = AMOUNT_RE.match(s)
        tokens = TOKEN_RE.findall(s[m.end():] if m else s)
        i = 0
        if m:
            line.amount = parse_number(m.group("a"))
            line.amount_hi = parse_number(m.group("b")) if m.group("b") else None
    prefix: list[str] = []
    if line.amount is not None:
        while i < len(tokens):
            tok = tokens[i]
            bare = tok.strip(".,;:").lower()
            if tok.startswith("("):
                line.parens.append(tok[1:-1].strip()); i += 1; continue
            if bare == "of" and (line.unit or line.tail):
                i += 1; continue
            if bare in ("fluid", "fl") and i + 1 < len(tokens) and tokens[i + 1].strip(".,;:").lower() in ("ounce", "ounces", "oz"):
                line.unit = "fl oz"; i += 2; continue
            if line.unit is None and not line.tail and bare in UNIT_MAP:
                line.unit = UNIT_MAP[bare]; i += 1; continue
            if bare in SIZE_WORDS and line.unit:
                break                                               # "1 pound large shrimp": with a weight the size stays in the text and becomes prep (below)
            if bare in SIZE_WORDS:
                line.tail.append(bare); i += 1
                while i + 1 < len(tokens) and tokens[i].lower() == "or" and tokens[i + 1].strip(".,;:").lower() in SIZE_WORDS:
                    i += 2                                          # "3 large or medium eggs": the first size is kept
                continue
            if bare == "whole" and i + 1 < len(tokens) and tokens[i + 1].strip(".,;:").lower() in COUNT_FORMS:
                line.tail.append(bare); i += 1; continue
            if bare in COUNT_FORMS and i + 1 < len(tokens) and not tok.endswith(","):    # "24 whole cloves, divided": cloves IS the name
                line.tail.append(bare); i += 1; continue
            j = i
            while j < len(tokens) and j - i < 3 and _is_prefix_word(tokens[j]):
                j += 1
            if j > i and j < len(tokens) and tokens[j].strip(".,;:").lower() in (SIZE_WORDS | set(UNIT_MAP) | set(COUNT_FORMS)):
                prefix += tokens[i:j]; i = j; continue
            break
    if line.unit == "oz":                                   # "1 3/4 ounces (50 ml)": the figure in ml says it's a volume
        author = _author_metric_for(line)
        if author and author[1] in ("ml", "l"):
            line.unit = "fl oz"
    # brackets after the name ("(about 1 cup)", "(~80 g)", "(see note)") are approximations or cross-references:
    # they are not carried into the name or the prep
    line.trailing = [t[1:-1].strip() for t in tokens[i:] if t.startswith("(")]
    text = " ".join(prefix + [t for t in tokens[i:] if not t.startswith("(")])
    text = re.sub(r"^[\s.,;:)\]]+", "", text)
    text = re.sub(r"^(fresh|ripe|dry|dried|raw|cooked|frozen|whole),\s+", r"\1 ", text, flags=re.I)  # "fresh, crustless bread"
    text = re.sub(r"\b(?:homemade or store[- ]bought|store[- ]bought or homemade)\s+", "", text, flags=re.I)   # either is fine: no alternative
    such_as = re.search(r",?\s+such as\b.*$", text, re.I)                      # "neutral oil, such as peanut, canola, or vegetable oil": examples, not alternatives
    examples = ""
    if such_as:
        text, examples = text[:such_as.start()], such_as.group(0).strip(" ,")
    text = LIST_OF_ALTERNATIVES_RE.sub(lambda m: re.sub(r",\s+(?:and/or\s+|or\s+)?|\s+and/or\s+", " or ", m.group(1)) + m.group(2), text, count=1)
    text = re.sub(r"\band/or\b", "or", text)                                                          # "Tajín and/or kosher salt"
    cut = re.search(r"[,;]", text)                       # name ends at the first comma or semicolon; the rest is prep
    head, prep = (text[:cut.start()], text[cut.end():]) if cut else (text, "")
    line.name = re.sub(r"\s+", " ", head).strip(" .,;:")
    if prep.strip() and line.name and all(w in LEADING_ADJECTIVES for w in line.name.lower().split()):
        noun, _, rest = prep.strip().partition(",")                               # "cubed sweet, ripe mango, from 2 mangoes": the noun follows the comma
        if noun.strip():
            prep = ", ".join(x for x in (line.name, rest.strip()) if x)
            line.name = noun.strip()
    if line.unit and line.amount is not None:                                      # "1 pound large or medium shrimp": sizes are prep, not the name
        sizes = re.match(rf"^((?:{SIZE_RE})(?:\s+or\s+(?:{SIZE_RE}))*)\s+(?=\S)", line.name, re.I)
        if sizes:
            prep = ", ".join(x for x in (sizes.group(1), prep.strip()) if x)
            line.name = line.name[sizes.end():]
    line.name = re.sub(r"\b(\w+) \1\b", r"\1", line.name, flags=re.I)             # a typo on the page: "ground ground cumin"
    line.name = re.sub(r"^fruit\s+", "", line.name, flags=re.I)                     # "1 fruit (2-3/8" dia) lemon"
    line.name = re.sub(r"^(roll|bun|loaf|slice|piece|head|bulb|can|jar)s?\s+(.*\b\1s?)$", r"\2", line.name, flags=re.I)   # "4 roll potato rolls": the unit said twice
    line.prep = " ".join(x for x in (prep.strip(), examples) if x) or None
    if line.amount is not None and not line.tail and line.unit is None:             # "4 garlic cloves" -> "4 cloves garlic" (the library's order)
        noun = re.match(r"^(.+?)\s+(cloves?|sprigs?|stalks?)$", line.name, re.I)
        if noun:
            line.tail = [noun.group(2).lower()]
            line.name = noun.group(1)
    lead_or = re.match(r"^or\s+(\d.*)$", line.name, re.I)                           # "1 large or 2 medium waxy potato"
    if lead_or:
        line.alts.append(lead_or.group(1))
        line.name = parse_line(lead_or.group(1)).name
    # an alternative with its own amount ("... or four 8-ounce bone-in thighs") is kept for the substitutions note
    alt = re.search(rf"\s+or\s+((?:\d[\d/ ]*|(?:{NUMBER_WORD_RE}))\s+\S.*)$", line.name, re.I)
    if alt and alt.start() > 0:
        line.alts.append(alt.group(1).strip())
        line.name = line.name[:alt.start()].strip()
    # clauses that belong to prep rather than the name
    for pattern in (r"\s+(?:and|with)\s+(?:their|its)\s+(?:juices?|liquid)\b.*$",    # "whole peeled tomatoes and their juices"
                    r"\s+from\s+(?:about\s+)?\d.*$",                                 # "chipotle chile from 1 can of ..."
                    r"\s+per\s+\w+(?:\s+\w+)?$",                                     # "mole poblano per tortilla"
                    r"\s+plus\s+(?:more|extra|additional)\b.*$",                     # "kosher salt plus more for seasoning"
                    r"\s+for\s+(?:deep[- ])?(?:frying|coating|greasing|brushing|dusting|drizzling|rubbing|seasoning)\b.*$",
                    r"\s+from\s+the\s+.+$",                                          # "seasoning oil from the chinese vinaigrette"
                    r"\s+about\s+\d[\d/ ]*\s*(?:inch|inches|cm|-inch)\b.*$",         # "French bread about 12 inches long"
                    PREPARED_TAIL):                                                  # "onion grated on a microplane grater"
        m = re.search(pattern, line.name, re.I)
        if m and m.start() > 0 and not CHIPOTLE_CANNED_RE.search(line.name) and not FRUIT_SOURCE_RE.match(line.name):
            line.prep = " ".join(x for x in (m.group(0).strip(), line.prep) if x)
            line.name = line.name[:m.start()].strip()
    return line


# --- a count of sized pieces becomes one weight -------------------------------------------------------------------------------------

CONTAINER_WORDS = {"can", "cans", "pkg", "pkgs", "package", "jar", "jars", "bottle", "bottles", "bag", "bags", "box", "boxes",
                   "container", "containers", "carton", "cartons", "tub", "tubs", "bunch", "bunches", "envelope", "envelopes",
                   "packet", "packets"}


def collapse_package_weight(line: Line) -> None:
    """'Four 5- to 6-ounce chicken thighs' -> 24 oz (the higher end of a range, times the count); 'Two 8-ounce potatoes' ->
    16 oz. The author's own metric figure is scaled the same way. Packages (cans, jars...) keep 'N (size) cans'."""
    if line.amount is None or line.amount_hi is not None or line.unit is not None or any(w in CONTAINER_WORDS for w in line.tail):
        return
    size = None
    for text in line.parens:
        rng = paren_range(text)
        imp = imperial_part(text)
        if rng and rng[2] in ("oz", "lb"):
            size = (rng[1], rng[2]); break
        if imp and imp[1] in ("oz", "lb"):
            size = (imp[0], imp[1]); break
    if not size:
        return
    author = next((mp for mp in (metric_part(t) for t in line.parens) if mp), None)
    count = line.amount
    line.amount, line.unit, line.tail = round(count * size[0], 4), size[1], []
    line.parens = [f"{round(author[0] * count, 4):g} {author[1]}"] if author else []
    pounds_if_large(line)


def pounds_if_large(line: Line) -> None:
    """Ounces from 16 up are written as pounds, to the nearest 1/4 lb ("20 oz" -> 1 1/4 lb; exactly 16 oz -> 1 lb).
    The author's own metric figure, when given, still decides the Metric quantity."""
    if line.unit != "oz" or line.amount is None:
        return
    top = line.amount_hi if line.amount_hi is not None else line.amount
    if top < 16:
        return

    def quarter_pounds(ounces: float) -> float:
        return max(0.25, math.floor(ounces / 16 * 4 + 0.5) / 4)

    line.amount = quarter_pounds(line.amount)
    if line.amount_hi is not None:
        line.amount_hi = quarter_pounds(line.amount_hi)
        if line.amount_hi == line.amount:
            line.amount_hi = None
    line.unit = "lb"


@dataclass
class Substitution:
    """'brown sugar or granulated sugar': the first is the ingredient; the rest are listed in the notes."""
    main: str
    alts: list                       # (Line | None, name): a Line when the alternative has its own amount
    metric_text: str | None = None   # hand-written substitutions (overrides csv) can word the Metric side differently

    def text(self, system: str) -> str:
        if system == "Metric" and self.metric_text:
            return f"{self.main} → {self.metric_text}"
        parts = []
        for line, name in self.alts:
            amount = (render_us(line) if system == "US" else render_metric(line)) if line is not None else ""
            parts.append(f"{amount} {name}".strip())
        return f"{self.main} → {' or '.join(parts)}"


# --- renames: certain kinds of line are written in the library's own words -------------------------------------------------------

FRUITS = r"limes?|lemons?|oranges?|grapefruits?"
# "juice from about 2 limes", "freshly squeezed juice from 1 whole lemon", "zest of 1 orange", "zest and juice from 1 lime"
FRUIT_SOURCE_RE = re.compile(
    rf"^(?:(?:fresh|freshly[- ]squeezed|squeezed|grated|finely grated|freshly grated)\s+)*(?P<kind>juice|zest|peels?|rinds?)(?:\s+and\s+(?:juice|zest))?\s+(?:from|of)\s+"
    rf"(?:about\s+|approximately\s+|roughly\s+)?(?:(?P<n>\d+(?:\s+\d+/\d+)?|\d+/\d+|{NUMBER_WORD_RE}|half(?:\s+an?)?|an?)\s+)?"
    rf"(?:(?:whole|large|medium|small|big)\s+)?(?P<fruit>{FRUITS})\b", re.I)
# chipotles that come from a can of chipotles in adobo, however the author words it
CHIPOTLE_CANNED_RE = re.compile(r"\bchipotles?\b(?:\s+(?:chil(?:i|e)s?|peppers?))?.*?\b(?:in\s+adobo|adobo|from\s+\d+\s+cans?)\b", re.I)
CHIPOTLE_DRIED_RE = re.compile(r"\bdried\b.*\bchipotles?\b|\bchipotles?\b.*\bdried\b", re.I)
CANNED_CHIPOTLE_NAME = "chipotle chilis, canned in adobo sauce"
CANNED_CHIPOTLE_MASTER = "chipotle peppers in adobo sauce"     # the master's own entry for it
DRIED_CHIPOTLE_NAME = "chipotle chilis, dried"
PREP_BEFORE_CHIPOTLE = re.compile(r"^((?:finely |roughly |coarsely )?(?:minced|chopped|diced|sliced|mashed|pureed))\s+", re.I)


def apply_renames(line: Line) -> str | None:
    """Rewrites a line in the library's words where a kind of line has a fixed wording (the user's rename table).
    Returns the name to MATCH against the master when it differs from the name shown (None = match the shown name)."""
    name = line.name
    bare = re.match(r"^(?:(?:fresh|freshly squeezed|squeezed|grated|finely grated|freshly grated)\s+)*(juice|zest)$", name, re.I)
    if bare:                                                # "fresh juice (from 1 lemon)", "3/4 cup juice (4 whole lemons)": the fruit is named beside it
        context = " ".join([line.prep or "", *line.trailing, line.raw])
        fruit_in = re.search(rf"\b({FRUITS})\b", context, re.I)
        if fruit_in:
            fruit = re.sub(r"s$", "", fruit_in.group(1).lower())
            if line.unit is None:                           # "Fresh juice from 1/2 lemon (about 1 tablespoon; 15ml)": the bracket gives the volume
                for text in line.trailing + line.parens:
                    vol = next((VOLUME_IN_PAREN.search(part) for part in text.split(";") if VOLUME_IN_PAREN.search(part)), None)
                    if vol:
                        line.amount, line.amount_hi, line.unit = parse_number(vol["a"]) or 1.0, None, UNIT_MAP[vol["u"].lower()]
                        break
            if line.unit:
                line.name = f"{fruit} {bare.group(1).lower()}"
                return line.name
    m = FRUIT_SOURCE_RE.match(name)
    if m:
        fruit = re.sub(r"s$", "", m["fruit"].lower())
        if line.unit is None:                               # "Fresh juice from 1/2 lemon (about 1 tablespoon; 15ml)": the bracket gives a volume
            for text in line.trailing + line.parens:
                vol = next((VOLUME_IN_PAREN.search(part) for part in text.split(";") if VOLUME_IN_PAREN.search(part)), None)
                if vol:
                    line.amount, line.amount_hi, line.unit = parse_number(vol["a"]) or 1.0, None, UNIT_MAP[vol["u"].lower()]
                    break
        if line.unit:                                       # a volume is given: "2 tablespoons juice from 2 limes" -> "2 tbsp lime juice"
            line.name = f"{fruit} {m['kind'].lower()}"
            return line.name
        word = (m["n"] or "1").lower()
        count = NUMBER_WORDS.get(word) or (0.5 if word.startswith("half") else 1 if word in ("a", "an") else parse_number(word) or 1.0)
        line.amount = line.amount if line.amount is not None else float(count)
        verb = {"juice": "juiced", "zest": "zested"}.get(m["kind"].lower(), "peel only")
        line.name = f"{fruit}{'s' if (line.amount or 1) > 1 else ''}, {verb}"
        return fruit
    if CHIPOTLE_CANNED_RE.search(name) and not CHIPOTLE_DRIED_RE.search(name):
        lead = PREP_BEFORE_CHIPOTLE.match(name)
        if lead:
            line.prep = ", ".join(x for x in (lead.group(1).lower(), line.prep) if x)
        line.name = CANNED_CHIPOTLE_NAME
        return CANNED_CHIPOTLE_MASTER
    if CHIPOTLE_DRIED_RE.search(name):
        line.name = DRIED_CHIPOTLE_NAME
        return line.name
    batch = re.match(r"^(?:recipe|batch)\s+(.+)$", name, re.I)    # "1/2 recipe vanilla pastry cream": the generic ingredient, no measurable amount
    if batch:
        line.name, line.amount, line.amount_hi, line.unit, line.parens, line.tail = batch.group(1).lower(), None, None, None, [], []
        return line.name
    if re.search(r"\s+recipe$", name, re.I):                # a reference to another recipe: becomes the generic ingredient
        line.name = re.sub(r"\s+recipe$", "", name, flags=re.I).lower()
        for text in line.trailing:                          # "(1 pound)" after the name gives the quantity
            imp = imperial_part(text)
            if imp and imp[1] in ("oz", "lb") and line.unit is None:
                mp = metric_part(text)                      # keep only the author's metric figure, so the US side shows no duplicate
                line.amount, line.amount_hi, line.unit, line.tail = imp[0], None, imp[1], []
                line.parens = [f"{mp[0]:g} {mp[1]}"] if mp else []
                break
        return line.name
    return None


# --- rendering a parsed line in one unit system ---------------------------------------------------------------------------

def _pluralize(word: str, amount: float | None, unit: str | None = None) -> str:
    forms = COUNT_FORMS.get(word)
    if not forms:
        return word
    if unit == "inch":                  # "1 1/2 inch piece": the one piece is 1 1/2 inches long
        return forms[0]
    return forms[1] if amount is not None and amount > 1.01 else forms[0]


def _amount_text(line: Line) -> str:
    return fmt_range(line.amount, line.amount_hi) if line.amount is not None else ""


RANGE_PAREN_RE = re.compile(rf"^({NUM})\s*-?\s*to\s*({NUM})\s*-?\s*(ounces?|oz|pounds?|lbs?|inch(?:es)?)\b", re.I)


def paren_range(text: str) -> tuple[float, float, str] | None:
    """'5- to 6-ounce' -> (5, 6, 'oz'): a package size given as a range."""
    m = RANGE_PAREN_RE.match(text.strip())
    if not m:
        return None
    return parse_number(m.group(1)) or 0.0, parse_number(m.group(2)) or 0.0, _canon_prose_unit(m.group(3))


def _paren_us(text: str) -> str | None:
    rng = paren_range(text)
    if rng:
        return f"({fmt_number(rng[0])} to {fmt_number(rng[1])} {rng[2]})"
    imp = imperial_part(text)
    if imp and imp[1] in ("oz", "fl oz", "lb", "inch"):        # package / piece sizes: "(15 oz)", "(4 inch)"
        return f"({fmt_number(imp[0])} {imp[1]})"
    return None                       # metric-only or free-text brackets are dropped from a US quantity


def localize_units_short(text: str) -> str:
    """'minus 1 teaspoon' -> 'minus 1 tsp' (the library's short unit names)."""
    return re.sub(rf"\b({PROSE_UNIT})\b", lambda m: _canon_prose_unit(m.group(1)), text, flags=re.I)


VOLUME_IN_PAREN = re.compile(rf"(?:about\s+|approximately\s+)?(?P<a>{NUM})(?:{RANGE_SEP}(?P<b>{NUM}))?\s*(?P<u>cups?|tablespoons?|teaspoons?|tbsp|tsp)\b", re.I)


def us_equivalent(line: Line) -> tuple[float, float | None, str, str]:
    """For a line the page wrote metric-first ('555g (19 1/2 ounces; about 4 1/4 cups) flour'): its US measure, as
    (amount, amount_hi, unit, extra). Cups and spoons are preferred (the library writes them; '1 1/2 cups plus 1 tablespoon'
    keeps its 'plus 1 tbsp' as extra), then ounces / pounds; the table fills in when the page gives neither."""
    for text in line.parens:                                      # ounces first, as the author gives them (never converted to pounds)
        for part in text.split(";"):
            both = re.search(rf"({NUM})\s*(?:pounds?|lbs?)\s*(?:and\s*)?({NUM})\s*(?:ounces?|oz)\b", part, re.I)
            if both:
                return (parse_number(both.group(1)) or 0.0) * 16 + (parse_number(both.group(2)) or 0.0), None, "oz", ""
            ounces = re.search(rf"(?<![\w/]){NUM}\s*(?:ounces?|oz)\b(?!\s*\))", part, re.I) if not re.search(r"fl(?:uid)?\.?\s*oz", part, re.I) else None
            if ounces:
                number = re.match(NUM, ounces.group(0))
                return parse_number(number.group(0)) or 0.0, None, "oz", ""
            pounds = re.search(rf"({NUM})\s*(?:pounds?|lbs?)\b", part, re.I)
            if pounds:
                return (parse_number(pounds.group(1)) or 0.0) * 16, None, "oz", ""
    for text in line.parens:                                      # no weight given: cups and spoons
        for part in text.split(";"):
            m = VOLUME_IN_PAREN.search(part)
            if m:
                plus = re.match(rf"\s*plus\s+({NUM})\s*({PROSE_UNIT})\b", part[m.end():], re.I)
                extra = f"plus {fmt_number(parse_number(plus.group(1)) or 0)} {_canon_prose_unit(plus.group(2))}" if plus else ""
                return parse_number(m["a"]) or 0.0, (parse_number(m["b"]) if m["b"] else None), UNIT_MAP[m["u"].lower()], extra
    for text in line.parens:
        for part in text.split(";"):
            imp = imperial_part(part)
            if imp and imp[1] == "fl oz":
                return imp[0], None, imp[1], ""
    top = line.amount_hi if line.amount_hi is not None else line.amount
    if line.unit in ("g", "kg"):
        return max(0.25, round(top * (1000 if line.unit == "kg" else 1) / G_PER_OZ * 4) / 4), None, "oz", ""
    if line.unit in ("ml", "l"):
        return max(0.25, round(top * (1000 if line.unit == "l" else 1) / ML_PER_FL_OZ * 4) / 4), None, "fl oz", ""
    return top, None, line.unit, ""


def render_us(line: Line) -> str:
    if line.fixed_us is not None:
        return line.fixed_us
    if line.amount is None:
        return ""
    if line.unit in ("g", "kg", "ml", "l"):                    # the US recipe carries no metric figures
        amount, amount_hi, unit, extra = us_equivalent(line)
        us = dataclasses.replace(line, amount=amount, amount_hi=amount_hi, unit=unit, parens=[], extra=extra or line.extra)
        return render_us(us)                                    # baking-by-weight lines stay in ounces: no pounds conversion here
    top = line.amount_hi if line.amount_hi is not None else line.amount
    parts = [_amount_text(line)]
    if line.unit:
        parts.append("cups" if line.unit == "cup" and top > 1.01 else line.unit)
    parts += [p for p in (_paren_us(t) for t in line.parens) if p]
    parts += [_pluralize(w, top, line.unit) if w in COUNT_FORMS else w for w in line.tail]
    if line.extra:
        parts.append(line.extra)
    return " ".join(parts)


def _author_metric_for(line: Line) -> tuple[float, str] | None:
    for text in line.parens:
        mp = metric_part(text)
        if mp:
            return mp
    return None


def render_metric(line: Line) -> str:
    if line.fixed_us is not None:
        return line.fixed_metric if line.fixed_metric is not None else line.fixed_us
    if line.amount is None:
        return ""
    top = line.amount_hi if line.amount_hi is not None else line.amount
    if line.unit in ("tbsp", "tsp", "cup") or line.unit is None and not line.parens:
        return render_us(line)
    parts = []
    if line.unit in ("oz", "lb", "fl oz", "quart", "pint", "gallon", "inch"):
        author = _author_metric_for(line)
        if author and line.amount_hi is None:
            parts.append(metric_value_to_library(author[0], author[1], line.unit))
        elif line.amount_hi is not None:
            lo, hi = convert_us_to_metric(line.amount, line.unit), convert_us_to_metric(line.amount_hi, line.unit)
            parts.append(f"{lo.rsplit(' ', 1)[0]} to {hi}" if lo and hi else _amount_text(line))
        else:
            parts.append(convert_us_to_metric(line.amount, line.unit) or f"{_amount_text(line)} {line.unit}")
        parts += [_pluralize(w, top, line.unit) if w in COUNT_FORMS else w for w in line.tail]
        return " ".join(parts)
    if line.unit in ("g", "kg", "ml", "l", "cm"):
        parts = [_amount_text(line), line.unit.replace("l", "L") if line.unit == "l" else line.unit]
        parts += [_pluralize(w, top, line.unit) if w in COUNT_FORMS else w for w in line.tail]
        return " ".join(parts)
    # no unit: a count, optionally with a package size in brackets ("1 (15 ounce) can")
    parts = [_amount_text(line)]
    for text in line.parens:
        rng = paren_range(text)
        imp = imperial_part(text)
        mp = metric_part(text)
        if rng:                                              # a size range: converted from the table (the author's bracket would give one end)
            lo, hi = convert_us_to_metric(rng[0], rng[2]), convert_us_to_metric(rng[1], rng[2])
            parts.append(f"({lo.rsplit(' ', 1)[0]} to {hi})" if lo and hi else f"({fmt_number(rng[0])} to {fmt_number(rng[1])} {rng[2]})")
        elif any(paren_range(t) for t in line.parens):
            continue
        elif mp:
            parts.append(f"({metric_value_to_library(mp[0], mp[1], imp[1] if imp else 'oz')})")
        elif imp:
            conv = convert_us_to_metric(imp[0], imp[1])
            parts.append(f"({conv})" if conv else f"({fmt_number(imp[0])} {imp[1]})")
    parts += [_pluralize(w, top, line.unit) if w in COUNT_FORMS else w for w in line.tail]
    return " ".join(parts)


# --- prep folding ----------------------------------------------------------------------------------------------------------

def fold_prep(prep: str | None) -> str | None:
    """Short prep text only; long text and cross-references are dropped (the directions cover them)."""
    if not prep:
        return None
    prep = re.sub(r"\(\s*[~≈]?\s*\d[^)]*\b(?:g|kg|ml|l|cm|grams?)\b[^)]*\)", "", prep)     # metric weights of the item
    prep = CROSS_REF_RE.sub("", prep)
    prep = re.sub(r"\s+", " ", prep).strip(" ,;.")
    if re.match(rf"or\s+(?:\d|(?:{NUMBER_WORD_RE})\b)", prep, re.I):
        return None                                  # "or 2 boneless thighs": an alternative with its own amount, not prep
    prep = re.sub(r"^(?:or|and)\s+", "", prep)
    if not prep or len(prep) > 40 or "(" in prep or "optional" in prep.lower():
        return "divided" if re.match(r"divided\b", prep or "", re.I) else None      # "divided; for table salt, use half..." keeps "divided"
    return prep


# --- text (directions, notes, description) ------------------------------------------------------------------------------------

BAKING_WORDS = re.compile(r"\b(oven|preheat|bake|baking|broil|broiler|roast|roasting)\b", re.I)
# One pass with three alternatives (so a rewritten temperature is never scanned again):
#   "400°F (200°C)"  |  "200°C (400°F)"  |  a bare "400°F"
TEMPERATURE = re.compile(r"(?P<f1>\d{2,3})\s*°\s*F\s*\(\s*(?:about\s*)?(?P<c1>\d{2,3})\s*°\s*C\s*\)"
                         r"|(?P<c2>\d{2,3})\s*°\s*C\s*\(\s*(?P<f2>\d{2,3})\s*°\s*F\s*\)"
                         r"|(?P<f3>\d{2,3})\s*°\s*F\b")
PROSE_UNIT = r"(?:tablespoons?|teaspoons?|tbsp\.?|tsp\.?|cups?|fluid ounces?|fl\.? ?oz\.?|ounces?|oz\.?|pounds?|lbs?\.?|quarts?|pints?|gallons?|inch(?:es)?)"
MEASURE_PAREN = re.compile(rf"(?P<amt>{NUM}(?:{RANGE_SEP}{NUM})?)\s*(?P<hy>-)?\s*(?P<unit>{PROSE_UNIT})(?P<dot>\.?)\s*\((?P<paren>[^)]*)\)", re.I)
BARE_IMPERIAL = re.compile(rf"(?<![\w/.])(?P<amt>{NUM}(?:{RANGE_SEP}{NUM})?)\s*(?P<hy>-)?\s*(?P<unit>fluid ounces?|fl\.? ?oz\.?|ounces?|oz\b\.?|pounds?|lbs?\b\.?)(?![\w(])", re.I)
BARE_INCH = re.compile(rf"(?<![\w/.])(?P<amt>{NUM}(?:{RANGE_SEP}{NUM})?)\s*(?P<hy>-)?\s*(?:inch(?:es)?|in\.)\b(?!\s*\()", re.I)
METRIC_PAREN_LEFTOVER = re.compile(r"\s*\((?:~|about\s+|approximately\s+)?\d[\d.,/\s]*(?:\s*(?:to|-|–)\s*[\d.]+)?\s*(?:g|kg|ml|l|cm|mm|grams?|liters?)\b[^)]*\)", re.I)


def _canon_prose_unit(word: str) -> str:
    w = re.sub(r"[.\s]", "", word.lower())
    if w.startswith("fl"):
        return "fl oz"
    return UNIT_MAP.get(w, UNIT_MAP.get(w.rstrip("s"), w))


def _c_from_f(f: float, baking: bool) -> int:
    c = (f - 32) * 5 / 9
    return int(round(c / 5) * 5) if baking else int(round(c))


def localize_text(text: str, system: str) -> str:
    """Rewrites the measures in running text for one unit system ("US" or "Metric")."""
    if not text:
        return text

    def is_baking(m) -> bool:
        return bool(BAKING_WORDS.search(text[max(0, m.start() - 100): m.end() + 30]))

    def temperature(m):
        f = m["f1"] or m["f2"] or m["f3"]
        if system == "US":
            return f"{f}°F"
        baking = is_baking(m)
        c = m["c1"] or m["c2"] or str(_c_from_f(int(f), baking))     # the author's own Celsius wins
        return f"{c}°C ({f}°F)" if baking else f"{c}°C"

    text = TEMPERATURE.sub(temperature, text)

    def with_paren(m):
        mp = metric_part(m["paren"])
        if not mp:
            return m.group(0)                                   # a bracket that isn't a metric figure: leave it
        unit = _canon_prose_unit(m["unit"])
        written = f"{m['amt']}{m['hy'] or ' '}{m['unit']}{m['dot']}"
        if system == "US":
            return written
        if unit in ("tbsp", "tsp", "cup"):
            return written                                      # spoons and cups stay in the Metric recipe
        hyphen = "-" if m["hy"] else " "
        return metric_value_to_library(mp[0], mp[1], unit if unit in ("lb", "oz") else "").replace(" ", hyphen if mp[1] == "cm" else " ")

    text = MEASURE_PAREN.sub(with_paren, text)
    if system == "US":
        return re.sub(r"\s{2,}", " ", METRIC_PAREN_LEFTOVER.sub("", text))

    def bare(m):
        unit = _canon_prose_unit(m["unit"])
        amt = m["amt"]
        parts = re.split(RANGE_SEP, amt)
        nums = [parse_number(p) for p in parts]
        if any(n is None for n in nums):
            return m.group(0)
        conv = [convert_us_to_metric(n, unit) for n in nums]
        if not all(conv):
            return m.group(0)
        if len(conv) == 1:
            return conv[0]
        return f"{conv[0].rsplit(' ', 1)[0]} to {conv[1]}"

    text = BARE_IMPERIAL.sub(bare, text)

    def inch(m):
        parts = re.split(RANGE_SEP, m["amt"])
        nums = [parse_number(p) for p in parts]
        if any(n is None for n in nums):
            return m.group(0)
        cms = [n * CM_PER_INCH for n in nums]
        big = cms[-1] >= 5
        vals = [f"{round(c) if big else round_half_cm(c):g}" for c in cms]   # prose: "25-cm pan", "1.5-cm dice"
        joined = " to ".join(vals)
        return f"{joined}-cm" if m["hy"] else f"{joined} cm"

    return BARE_INCH.sub(inch, text)


# --- ingredient resolution against the master -------------------------------------------------------------------------------

# Words about HOW an ingredient is prepared or presented, not WHAT it is: peeled off when matching, and when a new
# master entry has to be made (so "stemmed and chopped lacinato kale" is entered as "lacinato kale").
PREP_DESCRIPTORS = ("homemade or store bought", "store bought or homemade", "homemade", "store bought", "melted", "stemmed and chopped",
                    "very thinly sliced", "thinly sliced", "roughly chopped", "finely chopped", "finely minced", "finely diced",
                    "chopped", "grated", "shredded", "minced", "diced", "sliced", "crumbled", "pitted", "drained", "peeled",
                    "freshly picked", "fresh picked", "picked", "freshly squeezed", "freshly cracked", "freshly ground",
                    "cold", "warm", "ice cold", "chilled", "boiling", "lukewarm", "tap", "filtered", "heaping", "other",
                    "trimmed", "regular", "thick cut", "extra thick", "dutch process", "good quality", "high quality")
# Words that may describe the ingredient itself: peeled off only to find an existing match, never to name a new entry.
DESCRIPTORS = PREP_DESCRIPTORS + ("bone in skin on", "skin on bone in", "skin-on", "skin on", "bone in", "fresh", "ripe", "large", "small",
                                  "medium", "toasted", "cooked", "raw", "whole", "packed", "old fashioned", "reduced sodium",
                                  "low sodium", "boneless skinless", "boneless", "skinless")
MODIFIERS = ("bone in skin on", "boneless skinless", "unsalted", "salted", "boneless", "skinless", "bone in", "ground", "dried", "frozen",
             "smoked", "light", "dark", "reduced sodium", "low sodium")
GENERIC_HEADS = {"oil", "cheese", "sauce", "vinegar", "sugar", "flour", "salt", "milk", "cream", "butter", "broth", "paste", "powder",
                 "seeds", "beans", "rice", "noodles", "wine", "juice", "syrup", "mustard", "yogurt", "stock", "bread", "pasta", "rolls", "buns", "crackers"}
HEAD_NOUNS = GENERIC_HEADS | {"drippings", "fat", "lard", "shortening", "margarine", "creamer", "spray", "mayonnaise", "mayo", "ketchup", "honey",
                              "buttermilk", "water", "beer", "rum", "whiskey", "bourbon", "gin", "vodka", "tequila", "brandy", "molasses", "jam", "jelly"}
BROTH_RE = re.compile(r"\b(broth|stock|bouillon)\b")
FRUIT_PRODUCT_RE = re.compile(r"^(lime|lemon|orange|grapefruit)s? (?:juice|zest|peel)$")
# a page's wording -> the master's: plain words the master spells more specifically
ALIASES = {"ground black pepper": "black pepper", "ground white pepper": "white pepper", "freshly ground black pepper": "black pepper",
           "sugar": "granulated sugar", "olive oil": "extra virgin olive oil", "parmigiano reggiano": "parmesan cheese",
           "parmigiano reggiano cheese": "parmesan cheese", "grated parmigiano reggiano": "parmesan cheese",
           "buffalo hot sauce": "hot sauce", "louisiana style hot sauce": "hot sauce", "other louisiana style hot sauce": "hot sauce",
           "pepper": "black pepper", "cracked pepper": "black pepper", "fresh cracked pepper": "black pepper", "freshly ground pepper": "black pepper",
           "ground pepper": "black pepper", "fresh ground pepper": "black pepper", "freshly cracked pepper": "black pepper"}
ADVERB_RE = re.compile(r"^(?!only\b|family\b|early\b|daily\b|jelly\b|belly\b|holly\b|lily\b|italy\b)[a-z]{4,}ly\s+(?=\S)")   # "freshly minced garlic"

# Brand names are stripped to the generic ingredient (the user's rule); in drink recipes they stay in the shown name
# (the match is still on the generic ingredient). (pattern, generic replacement)
BRAND_RULES = [
    (r"\bFrank['’]?s(?:\s+Red\s?Hot)?(?:\s+(?:Original|Buffalo|Wings))*(?i:\s+sauce)?", "hot sauce"),
    (r"(?i:\bdiamond crystal\s+)", ""), (r"\bMorton(?:'s)?\s+", ""), (r"\bDuke'?s\s+", ""), (r"\bHellmann'?s\s+", ""),
    (r"\bBest Foods\s+", ""), (r"\bKikkoman\s+", ""), (r"\bHeinz\s+", ""), (r"\bBarilla\s+", ""), (r"\bKnorr\s+", ""),
    (r"\bSwanson\s+", ""), (r"\bPepperidge Farm\s+", ""), (r"\bSargento\s+", ""), (r"\bMaldon\s+", ""), (r"\bKerrygold\s+", ""),
    (r"\bLand O'?\s?Lakes\s+", ""), (r"\bPhiladelphia\s+", ""), (r"\bVelveeta\s+", ""), (r"\bTabasco\s+", ""),
    (r"\bKing Arthur(?: Baking| Flour)?\s+", ""), (r"\bBob'?s Red Mill\s+", ""), (r"\bGold Medal\s+", ""), (r"\bDomino\s+", ""),
    (r"\bBulleit\s+", ""), (r"\bCruzan\s+", ""), (r"\bCarpano Antica\s+", ""), (r"\bJack Daniel'?s\s+", ""), (r"\bMakers? Mark\s+", ""),
    (r"\b[A-Z][\w'’.&-]*\s+brand\s+", ""),                                 # "Wonderbread brand hamburger bun"
]


def strip_brands(name: str) -> str:
    out = name
    for pattern, generic in BRAND_RULES:
        out = re.sub(pattern, generic, out)
    return re.sub(r"\s+", " ", out).strip() or name


def squash(text: str) -> str:
    text = re.sub(r"\bfilets?\b", lambda m: m.group(0).replace("filet", "fillet"), text.lower())   # filet / fillet
    text = re.sub(r"\bstock\b", "broth", text)                                                      # stock and broth are one ingredient
    return re.sub(r"\s+", " ", text.replace("-", " ")).strip()


def suspicious_name(name: str) -> bool:
    """A name that is really a sentence fragment: such a thing must never become a master entry."""
    low = name.lower()
    return bool(re.search(r"\d|;|\b(?:plus|from|made through|such as|your choice|if desired)\b", low)) or len(low.split()) > 8


class Resolver:
    def __init__(self, master: IngredientMaster):
        self.master = master
        self.lookup: dict[str, int] = {}
        self.new: dict[int, str] = {}                  # ids created this run -> first recipe name
        self.cleaned: Counter = Counter()              # (page name, matched master name) of non-direct matches
        self.needs_rule: list = []                     # (fragment, recipe) names refused as master entries
        self._build()

    def _add(self, key: str, ing_id: int) -> None:
        if key:
            self.lookup.setdefault(key, ing_id)

    def _index_name(self, spelling: str, ing_id: int) -> None:
        s = squash(spelling)
        self._add(s, ing_id)
        self._add(name_key(s), ing_id)
        m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", s)           # "green onions (scallions)": both words are names
        if m:
            for part in (m.group(1), m.group(2)):
                self._add(squash(part), ing_id)
                self._add(name_key(squash(part)), ing_id)

    def _build(self) -> None:
        for key, entry in self.master.entries.items():
            for spelling in [entry["name"], *entry.get("names", {}), *entry.get("aliases", [])]:
                self._index_name(spelling, int(key))

    @staticmethod
    def candidates(name: str) -> list[str]:
        n = squash(re.sub(r"^[\s,.;:)]+|[\s,.;:(]+$", "", name))
        n = re.sub(r"\(.*?\)", "", n).strip()
        n = re.split(r",|\bsuch as\b", n)[0].strip()
        n = re.sub(r"\s+(?:optional|to taste|for serving)$", "", n)
        n = squash(ALIASES.get(n, n))
        # low-sodium broth is its own ingredient: the "low sodium" word is kept when the thing is a broth
        keep = {"low sodium", "reduced sodium"} if BROTH_RE.search(n) else set()
        out, cur = [n], n
        changed = True
        while changed:
            changed = False
            for d in sorted(DESCRIPTORS, key=len, reverse=True):
                d = squash(d)
                if d not in keep and cur.startswith(d + " "):
                    cur = cur[len(d) + 1:]
                    out.append(cur)
                    changed = True
                    break
            else:
                adverb = ADVERB_RE.match(cur)
                if adverb:
                    cur = cur[adverb.end():]
                    out.append(cur)
                    changed = True
        result = []
        for c in out:                                    # each form, then its "x, modifier" forms ("pork chops, bone in")
            result.append(c)
            for mod in MODIFIERS:
                if c.startswith(mod + " ") and mod not in keep:
                    result.append(f"{c[len(mod) + 1:]}, {mod}")
        return list(dict.fromkeys(x for x in result if x))

    @staticmethod
    def entry_name(name: str) -> str:
        """The name a NEW master entry is made under: the page's wording without its prep words ('melted ...',
        'stemmed and chopped ...'), stock as broth, 'low sodium' hyphenated -- and never a brand."""
        n = strip_brands(" ".join(name.split()).strip(" ,.;:()"))
        changed = True
        while changed:
            changed = False
            for d in sorted(PREP_DESCRIPTORS, key=len, reverse=True):
                spelling = re.escape(d).replace("\\ ", "[ -]")
                m = re.match(spelling + r"\s+(?=\S)", n, re.I)
                if m:
                    n = n[m.end():]
                    changed = True
                    break
            else:
                adverb = ADVERB_RE.match(n.lower())
                if adverb:
                    n = n[adverb.end():]
                    changed = True
        n = re.sub(r"\bstock\b", "broth", n, flags=re.I)
        return re.sub(r"\b(low|reduced)[ -]sodium\b", lambda m: f"{m.group(1).lower()}-sodium", n, flags=re.I)

    def _lookup(self, name: str):
        wanted = protected_qualifiers(name)
        for i, cand in enumerate(self.candidates(name)):
            for key in (cand, name_key(cand)):
                ing_id = self.lookup.get(key)
                if ing_id is not None and self.master._qualifiers_ok(ing_id, wanted):
                    return ing_id, ("direct" if i == 0 else "cleaned")
        for cand in self.candidates(name):                      # "lime juice", "grated fresh lemon zest": the fruit's own entry
            fruit = FRUIT_PRODUCT_RE.match(cand)
            if fruit:
                for key in (fruit.group(1), fruit.group(1) + "s"):
                    if self.lookup.get(key) is not None:
                        return self.lookup[key], "cleaned"
        return None, "new"

    def resolve(self, name: str, recipe_name: str = "") -> tuple[int | None, str]:
        """(master id, how). id is None for a name that is really a sentence fragment: it is reported and left out of
        the master instead of becoming an entry."""
        ing_id, how = self._lookup(name)
        if ing_id is None:
            clean = self.entry_name(name)
            if clean and clean != name:
                ing_id, how = self._lookup(clean)
        if ing_id is not None:
            if how == "cleaned":
                self.cleaned[(squash(name), self.master.display_name(ing_id))] += 1
            return ing_id, how
        clean = self.entry_name(name)
        if suspicious_name(clean):
            self.needs_rule.append((clean, recipe_name))
            return None, "needs_rule"
        ing_id, how = self.master.resolve_or_create(clean)
        if how == "new":
            entry = self.master.entries[str(ing_id)]
            entry["note"] = entry.get("note") or f"from {SOURCE}: {recipe_name}"[:120]
            self.new[ing_id] = recipe_name
            self._index_name(clean, ing_id)
        return ing_id, how

    @staticmethod
    def split_alternatives(name: str) -> list[str]:
        """'vegetable or canola oil' -> ['vegetable oil', 'canola oil']; 'A or B' -> [A, B]; single names unchanged."""
        n = re.split(r",|\bsuch as\b", name)[0].strip()
        n = re.sub(r"\s+or\s*$", "", n)                                               # "neutral oil or" (the rest was cut as prep)
        parts = [re.sub(r"^(?:other|any other)\s+", "", p.strip()) for p in re.split(r"\s+or\s+", n) if p.strip()]    # "peanut or other neutral oil"
        parts = [p for p in parts if not re.match(r"(?:an?|any)\s+(?:mix|combination|blend)\b", p, re.I)]   # "white or black sesame seeds or a mix"
        if len(parts) < 2:
            return [parts[0] if parts else n]
        last = parts[-1].split()
        out = []
        for p in parts[:-1]:
            pw = p.split()
            complete = pw[-1].lower() in HEAD_NOUNS or pw[-1].lower().endswith(("milk", "cream", "butter", "cheese", "yogurt"))   # "sour cream", "buttermilk": already a whole ingredient
            if complete:
                out.append(p)
            elif len(pw) == 1 and len(last) >= 2:
                out.append(f"{p} {' '.join(last[1:])}")                 # "vegetable or canola oil" -> "vegetable oil"
            elif len(pw) <= 2 and last and last[-1] in GENERIC_HEADS and pw[-1] != last[-1]:
                out.append(f"{p} {last[-1]}")                           # "peanut, rice bran, or neutral oil" -> "rice bran oil"
            else:
                out.append(p)
        return out + [parts[-1]]

    def resolve_line(self, name: str, recipe_name: str = "") -> int | None:
        """The ingredient for a line: with "A or B" it is A (B is an optional substitution, listed in the notes)."""
        return self.resolve(self.split_alternatives(name)[0], recipe_name)[0]


# --- suggestions for the review report ---------------------------------------------------------------------------------------

# Suggestions only (the user decides): (restriction id, label, pattern that suggests it, pattern that cancels the suggestion).
# The "cancels" patterns are the false positives found so far: plant milks and nut butters are not dairy, rice / tapioca
# flours and noodles are not gluten, sweet potatoes are not nightshades, oyster crackers / king oyster mushrooms are not shellfish...
SUGGEST = [
    (2, "dairy", r"\b(milk|cream|butter|cheese|yogh?urt|ghee|whey|buttermilk|mozzarella|parmesan|parmigiano|reggiano|pecorino|romano|grana padano|manchego|halloumi|brie|gouda|provolone|asiago|fontina|ricotta|mascarpone|cr[eè]me fra[iî]che|custard|kefir|paneer|gruy[eè]re|cotija|feta|cheddar|queso|jocoque|stracchino|casein|shokupan|ranch dressing|malted milk|nutella)\b",
        r"\b(?:coconut|almond|oat|soy|rice|cashew|hemp|nut|peanut|cocoa|shea|apple|vegan|non-?dairy|dairy-free)\s+(?:milk|cream|butter|yogh?urt|cheese|creamer)\b|\bbutter (?:lettuce|beans?|nut|pickle|oil)\b|\bbutternut\b|\bcream of tartar\b|\bice cream (?:cone|sandwich)\b"),
    (1, "gluten", r"\b(flour|bread|breadcrumbs?|crumbs|pasta|spaghetti|linguine|penne|orzo|macaroni|noodles?|ramen|udon|couscous|barley|rye|wheat|seitan|panko|tortillas?|pita|lavash|naan|brioche|baguette|rolls?|buns?|bagels?|croissants?|crackers?|cookies?|biscuits?|pretzels?|dough|pastry|crust|phyllo|filo|wonton wrappers?|dumpling wrappers?|semolina|bulgur|farro|spelt|matzo|graham|soy sauce|shoyu|teriyaki|beer|lager|ale|malt|stout|pancake mix|shokupan|festivals?|hoagie|malted)\b",
        r"\b(?:rice|tapioca|chickpea|almond|coconut|corn|potato|mung bean|glass|cellophane|bean thread|buckwheat|cassava|sweet rice|glutinous|oat|gram|besan|masa|arrowroot|sorghum|teff|millet|pho|rice stick)\b[ -]*(?:flour|noodles?|vermicelli|paper|starch|pasta|crackers?|cakes?)|\bgluten[- ]free\b|\bpretzel salt\b|\b(?:corn|rice|almond|coconut) (?:flour|tortillas?)\b|\bflour tortillas? chips\b|\bginger ale\b|\bcorn tortillas?\b"),
    (3, "fish", r"\b(fish|salmon|tuna|cod|anchov(?:y|ies)|sardines?|trout|halibut|mackerel|worcestershire|bonito|dashi|snapper|tilapia|haddock|haddie|herring|sea bass|bass|sole|carp|eel|pollock|surimi|mahi|swordfish|branzino|sierra|flounder|catfish|kippers?)\b",
        r"\bfish mint\b|\bfish sauce substitute\b|\bbass guitar\b"),
    (4, "shellfish", r"\b(shrimps?|prawns?|crabs?|lobsters?|clams?|mussels?|oysters?|scallops?|crawfish|crayfish|squid|calamari|octopus|cuttlefish|abalone|conch|langoustines?)\b",
        r"\boyster (?:crackers?|mushrooms?)\b|\bking oyster\b|\bscallop squash\b|\bsea scallions\b"),
    (5, "peanut", r"\bpeanuts?\b", r"\bpeanut[- ]free\b"),
    (6, "tree nut", r"\b(almonds?|walnuts?|pecans?|cashews?|pistachios?|hazelnuts?|macadamias?|pine nuts?|brazil nuts?|chestnuts?|candlenuts?|praline|amaretto|marzipan|nutella|dukkah|frangelico|filberts?)\b", r"\bwater chestnuts?\b"),
    (9, "soy", r"\b(soy|soya|soybeans?|tofu|tempeh|edamame|miso|natto|tamari|shoyu|teriyaki|hoisin|doubanjiang|douchi|gochujang|maggi|ponzu|kecap|fermented bean curd|bean curd)\b", r"\bsoy[- ]free\b|\bsoy lecithin\b"),
    (10, "nightshade", r"\b(tomato(?:es)?|tomatillos?|potato(?:es)?|eggplants?|aubergines?|paprika|piment[oó]n|cayenne|chil(?:i|e|li|es|is|ies)|chiltep[ií]n|bell peppers?|jalape[nñ]os?|serranos?|poblanos?|habaneros?|guajillos?|anchos?|arbol|[aá]rbol|aji|aj[ií]|pepperoncini|pimientos?|pimentos?|tajin|taj[ií]n|harissa|sriracha|gochujang|gochugaru|goji|salsa|ketchup|hot sauce|hot pepper|peppers?|merken|giardiniera|chorizo|chamoy|pico de gallo|shatta|sambal|french fries|fries|hash browns?|tater tots?|british chips)\b",
        r"\b(?:black|white|pink|green|sichuan|szechuan|long)\s+(?:pepper|peppercorns?)\b|\bpeppercorns?\b|\b(?:sweet|yam)\s+potato(?:es)?\b|\bpeppermint\b|\bpepper mint\b"),
    (11, "egg", r"\b(eggs?|mayonnaise|mayo|aioli|meringue|hollandaise|egg noodles?|custard|cole slaw|coleslaw)\b", r"\beggplants?\b|\bvegan mayo\b|\begg[- ]free\b"),
    (12, "sesame", r"\b(sesame|tahini|halva|halvah|za'?atar|benne|dukkah)\b", r""),
    (13, "mustard", r"\b(mustard|dijon)\b", r""),
    (14, "sulfite", r"\b(wine|sherry|vermouth|port|marsala|madeira|champagne|prosecco|dried (?:apricots?|fruit|mango|cherries|cranberries|currants)|olives?|pickles?|pickled|cornichons?|vinegar|balsamic|capers?|beer|lager|sauerkraut|horseradish|giardiniera|chimichurri)\b",
        r"\b(?:distilled )?white vinegar\b(?!.*(?:wine|cider|rice|balsamic))|\bdistilled vinegar\b|\bcream of tartar\b"),
]


def suggest_restrictions(name: str) -> list[dict]:
    low = name.lower().replace("’", "'")
    found = []
    for rid, label, pattern, unless in SUGGEST:
        m = re.search(pattern, low)
        if m and not (unless and re.search(unless, low)):
            found.append({"id": rid, "restriction": label, "because": m.group(0)})
    return found

# --- tags ---------------------------------------------------------------------------------------------------------------------------

CATEGORY_TAGS = {
    "dessert": 35, "desserts": 35, "breakfast": 36, "breakfast and brunch": 36, "brunch": 36,
    "snacks": 37, "snack": 37, "appetizers and hors d'oeuvres": 37, "appetizer": 37,
    "soups and stews": 2, "soup": 2, "stew": 2, "chili": 2, "cold soup": 2, "gazpacho": 2,
    "pasta": 1, "pizza": 1, "pasta and pizza": 1,
    "sandwiches": 4, "burgers": 4, "burger": 4, "sandwich": 4,
    "curry": 6, "curries": 6, "stir fry": 11, "stir-fry": 11,
    "grilling": 22, "barbecue": 22, "bbq": 22,
    "cocktail": 45, "cocktails": 45, "drinks": 44, "drink": 44, "beverages": 44, "beverage": 44, "smoothies": 44, "smoothie": 44,
    "rum": 45, "whiskey": 45, "whisky": 45, "tequila": 45, "gin": 45, "vodka": 45, "coffee": 44,     # spirit categories are cocktails
    "cake": [46, 35], "cakes": [46, 35], "pie": [47, 35], "pies": [47, 35], "ice cream": [48, 35],   # their own tag AND dessert
    "quick and easy": 38, "quick dinners": 38,
    "fruit desserts": 35, "candy": 35, "frosting": 35, "pastry": 35, "pancake": 36, "breakfast sweets": 36,
    "beer and wine": 44, "non-alcoholic": 44,
    "sides": 49, "side dish": 49, "side dishes": 49,                                                    # Sides: its own tag
    "jam / jelly": 50, "jam": 50, "jams and jellies": 50, "jelly": 50, "jams": 50,                      # Jams & jellies: its own tag
    "rubs and marinades": 51, "sauce": 51, "sauces": 51, "condiments and sauces": 51, "marinades": 51, "rubs": 51,   # Rubs, marinades & sauces: one tag
}


# categories that name a meal type or a kind of food, not a dish style: deliberately without a tag
UNTAGGED_CATEGORIES = {"dinner", "lunch", "entree", "mains", "condiment", "condiments", "ingredient", "spice mix", "bread", "salsa", "salad", "salads"}


def tag_ids_for(categories: list, keywords, name: str) -> list[int]:
    found = set()
    words = [c.lower() for c in categories or []]
    if isinstance(keywords, str):
        words += [k.strip().lower() for k in keywords.split(",")]
    for w in words:
        if w in CATEGORY_TAGS:
            found.update(CATEGORY_TAGS[w] if isinstance(CATEGORY_TAGS[w], list) else [CATEGORY_TAGS[w]])
    if re.search(r"\bcocktail\b", name, re.I):
        found.add(45)
    if 45 in found:
        found.add(44)                    # a cocktail is a drink
    return sorted(found)


# --- review report --------------------------------------------------------------------------------------------------------------------

def build_review(ctx: "Context", aligned: int) -> dict:
    """Everything the aligner had to judge, for a human to skim -- written to external_review_report.json."""
    master, resolver, review = ctx.resolver.master, ctx.resolver, ctx.review
    new_rows = []
    for ing_id, recipe in sorted(resolver.new.items()):
        entry = master.entries[str(ing_id)]
        new_rows.append({"id": ing_id, "name": entry["name"], "first_seen_in": recipe, "violates": entry.get("violates"),
                         "suggested_restrictions": suggest_restrictions(entry["name"])})
    return {
        "recipes_aligned": aligned,
        "new_ingredients": {"count": len(new_rows), "undecided": sum(1 for r in new_rows if r["violates"] is None), "items": new_rows},
        "loose_matches": [{"page_name": a, "matched_to": b, "lines": n} for (a, b), n in resolver.cleaned.most_common()],
        "optional_substitutions_in_notes": [{"recipe": r, "text": t} for r, t in review["substitutions"]],
        "needs_a_rule": [{"recipe": r, "text": t} for t, r in resolver.needs_rule],
        "optional_lines_moved_to_notes": [{"recipe": r, "line": l} for r, l in review["optional"]],
        "long_prep_dropped": [{"recipe": r, "prep": p} for r, p in review["dropped_prep"]],
        "lines_split_in_two": [{"recipe": r, "line": l} for r, l in review["split_lines"]],
        "unrecognised_units": dict(review["unknown_units"]),
        "categories_without_a_tag": dict(review["unmapped_categories"].most_common()),
        "recipes_without_servings": review["no_servings"],
    }


# --- the whole page -> records ------------------------------------------------------------------------------------------------------

def split_equipment(text: str | None) -> list[str]:
    items = []
    for chunk in re.split(r"\n", text or ""):
        parts = re.split(r",\s+(?![^()]*\))", chunk)             # commas inside brackets do not split
        items += [p.strip(" .;") for p in parts if p.strip(" .;")]
    return [i[0].lower() + i[1:] if i[0].isalpha() and not i[1:2].isupper() else i for i in items]


EQUIPMENT_LINE_RE = re.compile(r"^(?:cheesecloth|(?:kitchen|butcher'?s|cotton) twine|aluminum foil|tin foil|parchment(?: paper)?|plastic wrap|wax paper|"
                               r"toothpicks?|(?:wooden |bamboo |metal )?skewers?|paper towels?|butcher'?s paper)\b", re.I)
OVERRIDES_FILENAME = "external_line_overrides.csv"


def line_key(raw: str) -> str:
    return " ".join(raw.replace("\xa0", " ").split()).lower()


def load_overrides(library_dir: Path) -> dict:
    """external_line_overrides.csv (next to the registry): hand-written answers for one-off ingredient lines the parser
    can't read. Columns: raw_line, kind (ingredient | cookware | drop), name, match_as (the master entry), us_quantity,
    metric_quantity (blank = same as US). Several rows with the same raw_line give several ingredients."""
    path = library_dir / OVERRIDES_FILENAME
    table: dict[str, list] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                table.setdefault(line_key(row["raw_line"]), []).append(row)
    return table


@dataclass
class Context:
    resolver: Resolver
    overrides: dict = field(default_factory=dict)
    page_cookware: list = field(default_factory=list)       # equipment found among the ingredient lines of the page being aligned
    review: dict = field(default_factory=lambda: {"optional": [], "substitutions": [], "dropped_prep": [], "split_lines": [], "unknown_units": Counter(),
                                                  "unmapped_categories": Counter(), "no_servings": [], "twins": 0})


FROM_ABOVE_RE = re.compile(r"\bfrom\s+above\b|\(\s*(?:above|below)\s*\)", re.I)


def build_ingredients(parsed: dict, ctx: Context, recipe_name: str, is_drink: bool = False):
    """(US ingredients, Metric ingredients, optional-line texts, restriction ids or None).
    In a drink recipe the brand names stay in the shown ingredient names; the match is on the generic ingredient either way."""
    lines = []
    optional = []
    override_subs = []
    ctx.page_cookware = []
    for section in parsed["ingredient_sections"]:
        for item in section["items"]:
            whole = item["raw"]
            overridden = ctx.overrides.get(line_key(whole))
            if overridden:                                              # a hand-written answer for this exact line
                for row in overridden:
                    if row["kind"] == "cookware":
                        ctx.page_cookware.append(row["name"].strip())
                    elif row["kind"] == "substitution":                 # name = the ingredient, us/metric_quantity = what can replace it
                        us_text = (row["us_quantity"] or "").strip()
                        override_subs.append(Substitution(main=row["name"].strip(), alts=[(None, us_text)],
                                                          metric_text=(row.get("metric_quantity") or "").strip() or None))
                    elif row["kind"] == "note":                         # not an ingredient: goes to the notes at the top ("Optional or for serving")
                        optional.append(row["name"].strip())
                        ctx.review["optional"].append((recipe_name, row["name"].strip()))
                    elif row["kind"] == "ingredient":
                        us_text = (row["us_quantity"] or "").strip()
                        metric_text = (row.get("metric_quantity") or "").strip()
                        lines.append((Line(raw=whole, name=row["name"].strip(), fixed_us=us_text,
                                           fixed_metric=metric_text or us_text), (row["match_as"] or row["name"]).strip()))
                continue
            if re.search(r"\bfrom\s+above\b", whole, re.I):
                continue                                                # a mix made earlier on the page: its parts are listed above
            if FROM_ABOVE_RE.search(whole) and not re.match(r"^\s*\d", whole) and not item.get("quantity"):
                continue                                                # a back-reference to an earlier line
            for raw in split_plus_clause(whole):                        # "X, plus 1 tablespoon Y" -> two lines
                if re.match(r"^or\s+\S", raw, re.I) and lines:          # "or 2 medium waxy potatoes": an alternative to the line above
                    lines[-1][0].alts.append(re.sub(r"^or\s+", "", raw, flags=re.I).strip())
                    continue
                if is_optional_line(raw):
                    optional.append(raw.strip())
                    ctx.review["optional"].append((recipe_name, raw.strip()))
                    continue
                line = parse_line(raw)
                if not line.name:
                    continue
                if EQUIPMENT_LINE_RE.match(line.name):                         # "Cheesecloth", "Kitchen twine", "Aluminum foil": cookware, not food
                    ctx.page_cookware.append(line.name.strip())
                    continue
                if line.amount is None and re.search(r"\sand\s", line.name):
                    for piece in re.split(r"\s+and\s+", line.name):             # "salt and pepper" -> two lines
                        lines.append((Line(raw=raw, name=piece.strip(), prep=None), None))
                    ctx.review["split_lines"].append((recipe_name, raw))
                    continue
                if line.unit and line.unit not in ("tbsp", "tsp", "cup", "oz", "lb", "fl oz", "g", "kg", "ml", "l", "cm", "inch",
                                                   "quart", "pint", "gallon"):
                    ctx.review["unknown_units"][line.unit] += 1
                both = re.match(r"^(\S.*?)\s+and\s+(\d[\d/ ]*\s?(?:" + PROSE_UNIT + r")\b.+)$", line.name, re.I) if line.amount is not None else None
                if both:                                                    # "1 teaspoon juice and 1 teaspoon grated zest from 1 lemon" -> two lines
                    second = parse_line(both.group(2) + (", " + line.prep if line.prep else ""))
                    line.name = both.group(1)
                    lines.append((line, apply_renames(line)))
                    lines.append((second, apply_renames(second)))
                    continue
                lines.append((line, apply_renames(line)))
    us, metric, ids, subs = [], [], [], list(override_subs)
    keep_brand = (lambda n: n) if is_drink else strip_brands
    for line, match_as in lines:
        collapse_package_weight(line)
        pounds_if_large(line)                                           # plain "20 ounces ..." lines too
        prep = fold_prep(line.prep)
        if line.prep and not prep:
            ctx.review["dropped_prep"].append((recipe_name, line.prep))
        # "A or B": the first listed is the ingredient (its restrictions are the recipe's); the rest are optional substitutions
        names = [line.name] if match_as else Resolver.split_alternatives(line.name)
        first = names[0]
        alternatives = [(None, re.sub(r"^other\s+", "", keep_brand(other))) for other in names[1:]]
        for text in line.alts:                                          # "or four 8-ounce bone-in thighs": with its own amount
            alt_line = parse_line(text)
            collapse_package_weight(alt_line)
            pounds_if_large(alt_line)
            alternatives.append((alt_line, keep_brand(Resolver.split_alternatives(alt_line.name)[0])))
        generic = match_as or strip_brands(first)
        shown = first if match_as else keep_brand(first)
        ing_id = ctx.resolver.resolve(generic, recipe_name)[0]
        ids.append(ing_id)
        name = f"{shown}, {prep}" if prep else shown
        us.append({"ingredient_id": ing_id, "name": name, "quantity": render_us(line)})
        metric.append({"ingredient_id": ing_id, "name": name, "quantity": render_metric(line)})
        if alternatives:
            subs.append(Substitution(main=shown, alts=alternatives))
            ctx.review["substitutions"].append((recipe_name, subs[-1].text("US")))
    master = ctx.resolver.master
    violated: set = set()
    unknown = False
    for ing_id in set(ids):
        v = None if ing_id is None else master.violates(ing_id)
        if v is None:
            unknown = True
        else:
            violated |= v
    return us, metric, optional, subs, (None if unknown or not ids else sorted(violated))


def build_notes(parsed: dict, optional: list, subs: list, system: str) -> str | None:
    paragraphs = []
    if total_time_note(parsed):
        paragraphs.append(total_time_note(parsed))
    if optional:
        paragraphs.append("Optional or for serving: " + "; ".join(localize_text(o, system) for o in optional))
    if subs:
        paragraphs.append("Optional substitutions: " + "; ".join(s.text(system) for s in subs))
    extra = parsed.get("extra_sections") or {}
    if extra.get("Notes"):
        paragraphs.append("Notes: " + localize_text(extra["Notes"], system))
    if extra.get("Make-Ahead and Storage"):
        paragraphs.append("Make-ahead and storage: " + localize_text(extra["Make-Ahead and Storage"], system))
    return "\n\n".join(paragraphs) or None


class Aligner:
    """Turns fetched pages into library records on disk. out_dir is where families/ and the ingredient master
    are written; it is the library itself in real use, or a scratch copy while developing the rules (its master
    is then a copy of the library's, so experimental runs never touch the real one)."""

    def __init__(self, library_dir: Path, out_dir: Path, review_path: Path):
        self.out_dir, self.review_path, self.aligned = out_dir, review_path, 0
        source, self.master_path = master_path_for(library_dir), master_path_for(out_dir)
        if self.master_path != source and not self.master_path.exists():
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, self.master_path)
        self.master = IngredientMaster(load_master(self.master_path))
        self._before = json.dumps(self.master.data, sort_keys=True)
        self.ctx = Context(Resolver(self.master), overrides=load_overrides(library_dir))

    def align(self, row: dict, page: bytes) -> dict:
        """Writes the family's records. Returns {"family_dir", "image_url", "previous_url"} so the caller can fetch the
        thumbnail (a network step, kept out of here so re-aligning saved pages stays offline)."""
        parsed = parse_page(page.decode("utf-8", errors="replace"), row["url"])
        records = align_page(parsed, int(row["id"]), self.ctx, row["url"])
        fdir = self.out_dir / "families" / str(row["id"])
        fdir.mkdir(parents=True, exist_ok=True)
        previous_url = None
        for old in fdir.glob(f"{row['id']}_*.json"):
            try:
                previous_url = json.loads(old.read_text(encoding="utf-8")).get("thumbnail_image_url")
            except (OSError, ValueError):
                pass
        for rec in records:                                  # replace this family's earlier version (the slug may have changed)
            for old in fdir.glob(f"{rec['id']}_*.json"):
                old.unlink()
            (fdir / f"{rec['id']}_{rec['slug']}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        self.aligned += 1
        return {"family_dir": fdir, "image_url": parsed.get("image_url"), "previous_url": previous_url}

    def finish(self) -> dict:
        if json.dumps(self.master.data, sort_keys=True) != self._before:
            save_master(self.master.data, self.master_path)
        review = build_review(self.ctx, self.aligned)
        self.review_path.write_text(json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")
        return review


def align_page(parsed: dict, family_id: int, ctx: Context, source_url: str | None = None) -> list[dict]:
    """parse_page() output -> [US record, Metric record]."""
    name = parsed["name"]
    tag_ids = tag_ids_for(parsed["categories"], parsed["keywords"], name)
    us_ings, metric_ings, optional, subs, violated = build_ingredients(parsed, ctx, name, is_drink=bool({44, 45} & set(tag_ids)))
    servings = parsed["servings"]
    if not servings:
        servings = 4
        ctx.review["no_servings"].append(name)
    for cat in parsed["categories"] or []:
        if cat.lower() not in CATEGORY_TAGS and cat.lower() not in UNTAGGED_CATEGORIES:
            ctx.review["unmapped_categories"][cat] += 1
    rating = parsed.get("rating")
    equipment = [{"id": None, "name": e, "slug": slugify(e)}
                 for e in split_equipment((parsed.get("extra_sections") or {}).get("Special Equipment")) + ctx.page_cookware]
    slug = slugify(name) or "recipe"
    variants = []
    for position, system in enumerate(("US", "Metric")):
        variant_id = family_id if system == "US" else metric_id(family_id)
        variants.append({
            "id": variant_id,
            "recipe_id": family_id,
            "name": name,
            "slug": slug,
            "source_url": source_url or parsed["url"],
            "canonical_variant_url": None,
            "serving_count": servings,
            "cooking_minutes": active_minutes(parsed),
            "units": system,
            "group_label": "Default",
            "position": position,
            "unit_family_id": 2 if system == "US" else 1,
            "is_pro": None,
            "average_rating": round(rating[0] / 5, 3) if rating else None,
            "rating_count": rating[1] if rating else 0,
            "allowed_type_ids": [1, 3],
            "violated_restriction_ids": violated,
            "variety_tag_ids": tag_ids,
            "cuisine_tag_ids": [],
            "recipe_category_id": None,
            "kid_friendly": False,
            "cookwares": equipment,
            "ingredients": us_ings if system == "US" else metric_ings,
            "instructions": [{"id": i + 1, "primary_message": localize_text(text, system), "secondary_message": None}
                             for i, text in enumerate(parsed["steps"])],
            "nutrition": parsed["nutrition"],
            "schema_metadata": {"category": parsed["categories"], "cuisine": parsed["cuisines"], "keywords": parsed["keywords"]},
            "sibling_variant_ids": [family_id, metric_id(family_id)],
            "family_dir": f"families/{family_id}/",
            "source": SOURCE,
            "backfill_bonus_fields": None,
            "description": localize_text(parsed["description"], system) if parsed["description"] else None,
            "notes": build_notes(parsed, optional, subs, system),
            "author": parsed["authors"],
            "thumbnail_image_url": parsed["image_url"],
        })
    ctx.review["twins"] += 1
    return variants
