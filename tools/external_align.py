#!/usr/bin/env python3
"""
Aligns a recipe parsed from another site (external_recipes.parse_page) with THIS library's record
format, so it enters the library looking like every other recipe and the exporters, menu selection,
restriction filters and web page need no special cases. It runs during the fetch (seriouseats_scraper.py
--align); nothing downstream has to know where a recipe came from.

WHAT IT PRODUCES  one family = two records (the library's usual US/Metric pair):
    US      id = family id (the registry id)        units "US"      unit_family_id 2   position 0
    Metric  id = family id * 10 + 1                 units "Metric"  unit_family_id 1   position 1
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
    with violates null for you to decide; "A or B" lines get their own entry whose restrictions are the
    UNION of both sides, so an alternative can never hide an allergen);
  * section headings are dropped (the directions carry labels such as "Make the Broth:").

Everything that needed a judgement call is recorded in the review report (external_review_report.json).
"""

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from external_recipes import parse_page, slugify
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
SIZE_WORDS = {"small", "medium", "large", "extra-large", "big"}   # "whole" is handled separately: "2 whole heads" vs "whole peeled tomatoes"
# (singular, plural) the library's way: "1 clove", "2 cloves"
COUNT_FORMS = {}
for _s, _p in [("clove", "cloves"), ("head", "heads"), ("sprig", "sprigs"), ("stalk", "stalks"), ("slice", "slices"),
               ("piece", "pieces"), ("leaf", "leaves"), ("strip", "strips"), ("can", "cans"), ("bunch", "bunches"),
               ("pinch", "pinches"), ("dash", "dashes"), ("knob", "knobs"), ("segment", "segments"), ("rib", "ribs"),
               ("sheet", "sheets"), ("stick", "sticks"), ("stem", "stems"), ("wedge", "wedges"), ("ear", "ears"),
               ("fillet", "fillets"), ("pkg", "pkgs"), ("package", "pkgs"), ("jar", "jars"), ("bulb", "bulbs"),
               ("round", "rounds"), ("handful", "handfuls"), ("bottle", "bottles"), ("bag", "bags"), ("box", "boxes"),
               ("container", "containers"), ("cube", "cubes"), ("drop", "drops"), ("serving", "servings")]:
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
CROSS_REF_RE = re.compile(r"\(\s*(?:see|below|above)[^)]*\)|\b(?:see|noted?)\s+(?:note|notes|below|above|recipe)\b|\((?:above|below)\)", re.I)


def is_optional_line(raw: str) -> bool:
    if OPTIONAL_RE.search(raw):
        return True
    return bool(SERVING_RE.search(raw)) and not PLUS_MORE_RE.search(raw)


@dataclass
class Line:
    raw: str
    amount: float | None = None
    amount_hi: float | None = None
    unit: str | None = None
    parens: list = field(default_factory=list)        # bracket texts that follow the amount, in order
    tail: list = field(default_factory=list)          # size / count words ("medium", "cloves")
    name: str = ""                                    # clean name, no prep
    prep: str | None = None


def _is_prefix_word(tok: str) -> bool:
    bare = tok.strip(".,;:").lower()
    return bare in ADVERB_OR_PREP_WORDS or bare.endswith("ly")


def parse_line(raw: str) -> Line:
    s = " ".join(raw.replace(" ", " ").split())
    line = Line(raw=s)
    pinch = A_PINCH_RE.match(s)
    if pinch:
        line.amount, line.tail = 1.0, [pinch.group(1).lower()]
        s = s[pinch.end():]
        tokens = TOKEN_RE.findall(s)
        i = 0
    else:
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
            if bare in SIZE_WORDS:
                line.tail.append(bare); i += 1; continue
            if bare == "whole" and i + 1 < len(tokens) and tokens[i + 1].strip(".,;:").lower() in COUNT_FORMS:
                line.tail.append(bare); i += 1; continue
            if bare in COUNT_FORMS and i + 1 < len(tokens):
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
    text = " ".join(prefix + [t for t in tokens[i:] if not t.startswith("(")])
    text = re.sub(r"^[\s.,;:)\]]+", "", text)
    head, _, prep = text.partition(",")
    line.name = re.sub(r"\s+", " ", head).strip(" .,;:")
    line.prep = prep.strip() or None
    return line


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


def _paren_us(text: str) -> str | None:
    imp = imperial_part(text)
    if imp and imp[1] in ("oz", "fl oz", "lb", "inch"):        # package / piece sizes: "(15 oz)", "(4 inch)"
        return f"({fmt_number(imp[0])} {imp[1]})"
    return None                       # metric-only or free-text brackets are dropped from a US quantity


def render_us(line: Line) -> str:
    if line.amount is None:
        return ""
    top = line.amount_hi if line.amount_hi is not None else line.amount
    parts = [_amount_text(line)]
    if line.unit:
        parts.append("cups" if line.unit == "cup" and top > 1.01 else line.unit)
    parts += [p for p in (_paren_us(t) for t in line.parens) if p]
    parts += [_pluralize(w, top, line.unit) if w in COUNT_FORMS else w for w in line.tail]
    return " ".join(parts)


def _author_metric_for(line: Line) -> tuple[float, str] | None:
    for text in line.parens:
        mp = metric_part(text)
        if mp:
            return mp
    return None


def render_metric(line: Line) -> str:
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
        imp = imperial_part(text)
        mp = metric_part(text)
        if mp:
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
    prep = re.sub(r"^(?:or|and)\s+", "", prep)
    if not prep or len(prep) > 40 or "(" in prep or "optional" in prep.lower():
        return None
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

DESCRIPTORS = ("skin-on", "very thinly sliced", "thinly sliced", "roughly chopped", "finely chopped", "chopped", "grated",
               "minced", "diced", "sliced", "fresh", "ripe", "large", "small", "medium", "toasted", "drained", "peeled",
               "cooked", "raw", "freshly ground", "whole", "packed", "old fashioned", "reduced sodium", "low sodium",
               "tap", "filtered", "boiling", "lukewarm", "boneless skinless", "boneless", "skinless")
MODIFIERS = ("boneless skinless", "unsalted", "salted", "boneless", "skinless", "ground", "dried", "frozen", "smoked",
             "light", "dark", "reduced sodium", "low sodium")


def squash(text: str) -> str:
    text = re.sub(r"\bfilets?\b", lambda m: m.group(0).replace("filet", "fillet"), text.lower())   # filet / fillet
    return re.sub(r"\s+", " ", text.replace("-", " ")).strip()


class Resolver:
    def __init__(self, master: IngredientMaster):
        self.master = master
        self.lookup: dict[str, int] = {}
        self.new: dict[int, str] = {}                  # ids created this run -> first recipe name
        self.cleaned: Counter = Counter()              # (page name, matched master name) of non-direct matches
        self.unions: list = []
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
        out, cur = [n], n
        changed = True
        while changed:
            changed = False
            for d in sorted(DESCRIPTORS, key=len, reverse=True):
                d = squash(d)
                if cur.startswith(d + " "):
                    cur = cur[len(d) + 1:]
                    out.append(cur)
                    changed = True
                    break
        for c in list(out):
            for mod in MODIFIERS:
                if c.startswith(mod + " "):
                    out.append(f"{c[len(mod) + 1:]}, {mod}")
        return list(dict.fromkeys(x for x in out if x))

    def _lookup(self, name: str):
        wanted = protected_qualifiers(name)
        for i, cand in enumerate(self.candidates(name)):
            for key in (cand, name_key(cand)):
                ing_id = self.lookup.get(key)
                if ing_id is not None and self.master._qualifiers_ok(ing_id, wanted):
                    return ing_id, ("direct" if i == 0 else "cleaned")
        return None, "new"

    def resolve(self, name: str, recipe_name: str = "") -> tuple[int, str]:
        ing_id, how = self._lookup(name)
        if ing_id is not None:
            if how == "cleaned":
                self.cleaned[(squash(name), self.master.display_name(ing_id))] += 1
            return ing_id, how
        clean = " ".join(name.split()).strip(" ,.;:()")
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
        parts = [p.strip() for p in re.split(r"\s+or\s+", n) if p.strip()]
        if len(parts) < 2:
            return [n]
        last = parts[-1].split()
        out = []
        for p in parts[:-1]:
            pw = p.split()
            out.append(f"{p} {' '.join(last[1:])}" if len(pw) == 1 and len(last) >= 2 else p)
        return out + [parts[-1]]

    def resolve_line(self, name: str, recipe_name: str = "") -> int:
        alts = self.split_alternatives(name)
        if len(alts) == 1:
            return self.resolve(alts[0], recipe_name)[0]
        ids = [self.resolve(a, recipe_name)[0] for a in alts]
        if len(set(ids)) == 1:
            return ids[0]
        verdicts = [self.master.violates(i) for i in ids]
        if all(v is not None for v in verdicts) and len(set(verdicts)) == 1:
            return ids[0]               # every alternative violates exactly the same things: no combined entry needed
        # a combined entry whose restrictions are the union of every alternative
        phrase = " or ".join(alts)
        combo_id, how = self.resolve(phrase, recipe_name)
        entry = self.master.entries[str(combo_id)]
        decided = [self.master.violates(i) for i in ids]
        if entry.get("violates") is None and all(d is not None for d in decided):
            entry["violates"] = sorted(set().union(*decided))
            entry["note"] = f"union of: {', '.join(self.master.display_name(i) or str(i) for i in ids)}"
        elif entry.get("violates") is None:
            entry["note"] = f"union of ids {ids} once all are decided"
        if (phrase, ids) not in self.unions:
            self.unions.append((phrase, ids))
        return combo_id


# --- suggestions for the review report ---------------------------------------------------------------------------------------

SUGGEST = [
    (2, "dairy", r"\b(milk|cream|butter|cheese|yogh?urt|ghee|whey|buttermilk|mozzarella|parmesan|ricotta|cr[eè]me fra[iî]che|custard|kefir|paneer|mascarpone|gruy[eè]re|cotija|feta|cheddar)\b"),
    (1, "gluten", r"\b(flour|bread|pasta|noodles?|couscous|barley|rye|wheat|seitan|panko|breadcrumbs?|tortilla|pita|cracker|soy sauce|beer|ramen|orzo|bulgur|farro|semolina)\b"),
    (3, "fish", r"\b(fish|salmon|tuna|cod|anchov(?:y|ies)|sardines?|trout|halibut|mackerel|worcestershire|bonito|snapper|tilapia|haddock)\b"),
    (4, "shellfish", r"\b(shrimp|prawns?|crab|lobster|clams?|mussels?|oysters?|scallops?|crawfish)\b"),
    (5, "peanut", r"\bpeanuts?\b"),
    (6, "tree nut", r"\b(almonds?|walnuts?|pecans?|cashews?|pistachios?|hazelnuts?|macadamias?|pine nuts?|amaretto|marzipan)\b"),
    (9, "soy", r"\b(soy|tofu|tempeh|edamame|miso)\b"),
    (10, "nightshade", r"\b(tomato(?:es)?|potato(?:es)?|eggplants?|paprika|cayenne|chil(?:i|e|li)|bell peppers?|jalape[nñ]os?|peppers?|tomatillo|goji)\b"),
    (11, "egg", r"\b(eggs?|mayonnaise|mayo|aioli|meringue)\b"),
    (12, "sesame", r"\b(sesame|tahini)\b"),
    (13, "mustard", r"\bmustard\b"),
    (14, "sulfite", r"\b(wine|dried (?:apricots?|fruit)|sherry|vermouth)\b"),
]


def suggest_restrictions(name: str) -> list[dict]:
    low = name.lower()
    found = []
    for rid, label, pattern in SUGGEST:
        m = re.search(pattern, low)
        if m and not (rid == 10 and re.search(r"black pepper|peppercorn", low) and m.group(0) in ("pepper", "peppers")):
            found.append({"id": rid, "restriction": label, "because": m.group(0)})
    return found


# --- tags ---------------------------------------------------------------------------------------------------------------------------

CATEGORY_TAGS = {
    "dessert": 35, "desserts": 35, "breakfast": 36, "breakfast and brunch": 36, "brunch": 36,
    "snacks": 37, "snack": 37, "appetizers and hors d'oeuvres": 37, "appetizer": 37,
    "soups and stews": 2, "soup": 2, "stew": 2, "chili": 2,
    "pasta": 1, "pizza": 1, "pasta and pizza": 1,
    "sandwiches": 4, "burgers": 4, "sandwich": 4,
    "curry": 6, "curries": 6, "stir fry": 11, "stir-fry": 11,
    "grilling": 22, "barbecue": 22, "bbq": 22,
    "cocktail": 45, "cocktails": 45, "drinks": 44, "drink": 44, "beverages": 44, "beverage": 44, "smoothies": 44, "smoothie": 44,
}


def tag_ids_for(categories: list, keywords, name: str) -> list[int]:
    found = set()
    words = [c.lower() for c in categories or []]
    if isinstance(keywords, str):
        words += [k.strip().lower() for k in keywords.split(",")]
    for w in words:
        if w in CATEGORY_TAGS:
            found.add(CATEGORY_TAGS[w])
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
        "union_entries": [{"phrase": phrase, "component_ids": ids} for phrase, ids in resolver.unions],
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
        parts = [chunk] if "(" in chunk else re.split(r",\s+(?![^()]*\))", chunk)
        items += [p.strip(" .;") for p in parts if p.strip(" .;")]
    return [i[0].lower() + i[1:] if i[0].isalpha() and not i[1:2].isupper() else i for i in items]


@dataclass
class Context:
    resolver: Resolver
    review: dict = field(default_factory=lambda: {"optional": [], "dropped_prep": [], "split_lines": [], "unknown_units": Counter(),
                                                  "unmapped_categories": Counter(), "no_servings": [], "twins": 0})


def build_ingredients(parsed: dict, ctx: Context, recipe_name: str):
    """(US ingredients, Metric ingredients, optional-line texts, restriction ids or None)."""
    lines = []
    optional = []
    for section in parsed["ingredient_sections"]:
        for item in section["items"]:
            raw = item["raw"]
            if re.search(r"\((?:above|below)\)", raw, re.I) and not re.match(r"^\s*\d", raw) and not item.get("quantity"):
                continue                                                # "(above)" back-reference to an earlier line
            if is_optional_line(raw):
                optional.append(raw.strip())
                ctx.review["optional"].append((recipe_name, raw.strip()))
                continue
            line = parse_line(raw)
            if not line.name:
                continue
            if line.amount is None and re.search(r"\sand\s", line.name) and "," not in line.raw:
                for piece in re.split(r"\s+and\s+", line.name):                 # "salt and pepper" -> two lines
                    sub = Line(raw=raw, name=piece.strip(), prep=None)
                    lines.append(sub)
                ctx.review["split_lines"].append((recipe_name, raw))
                continue
            if line.unit and line.unit not in ("tbsp", "tsp", "cup", "oz", "lb", "fl oz", "g", "kg", "ml", "l", "cm", "inch",
                                               "quart", "pint", "gallon"):
                ctx.review["unknown_units"][line.unit] += 1
            lines.append(line)
    us, metric, ids = [], [], []
    for line in lines:
        prep = fold_prep(line.prep)
        if line.prep and not prep:
            ctx.review["dropped_prep"].append((recipe_name, line.prep))
        ing_id = ctx.resolver.resolve_line(line.name, recipe_name)
        ids.append(ing_id)
        name = f"{line.name}, {prep}" if prep else line.name
        us.append({"ingredient_id": ing_id, "name": name, "quantity": render_us(line)})
        metric.append({"ingredient_id": ing_id, "name": name, "quantity": render_metric(line)})
    master = ctx.resolver.master
    violated: set = set()
    unknown = False
    for ing_id in set(ids):
        v = master.violates(ing_id)
        if v is None:
            unknown = True
        else:
            violated |= v
    return us, metric, optional, (None if unknown or not ids else sorted(violated))


def build_notes(parsed: dict, optional: list, system: str) -> str | None:
    paragraphs = []
    if optional:
        paragraphs.append("Optional or for serving: " + "; ".join(localize_text(o, system) for o in optional))
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
        self.ctx = Context(Resolver(self.master))

    def align(self, row: dict, page: bytes) -> None:
        parsed = parse_page(page.decode("utf-8", errors="replace"), row["url"])
        records = align_page(parsed, int(row["id"]), self.ctx, row["url"])
        fdir = self.out_dir / "families" / str(row["id"])
        fdir.mkdir(parents=True, exist_ok=True)
        for rec in records:                                  # replace this family's earlier version (the slug may have changed)
            for old in fdir.glob(f"{rec['id']}_*.json"):
                old.unlink()
            (fdir / f"{rec['id']}_{rec['slug']}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        self.aligned += 1

    def finish(self) -> dict:
        if json.dumps(self.master.data, sort_keys=True) != self._before:
            save_master(self.master.data, self.master_path)
        review = build_review(self.ctx, self.aligned)
        self.review_path.write_text(json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")
        return review


def align_page(parsed: dict, family_id: int, ctx: Context, source_url: str | None = None) -> list[dict]:
    """parse_page() output -> [US record, Metric record]."""
    name = parsed["name"]
    us_ings, metric_ings, optional, violated = build_ingredients(parsed, ctx, name)
    servings = parsed["servings"]
    if not servings:
        servings = 4
        ctx.review["no_servings"].append(name)
    tag_ids = tag_ids_for(parsed["categories"], parsed["keywords"], name)
    for cat in parsed["categories"] or []:
        if cat.lower() not in CATEGORY_TAGS:
            ctx.review["unmapped_categories"][cat] += 1
    rating = parsed.get("rating")
    equipment = [{"id": None, "name": e, "slug": slugify(e)} for e in split_equipment((parsed.get("extra_sections") or {}).get("Special Equipment"))]
    slug = slugify(name) or "recipe"
    variants = []
    for position, system in enumerate(("US", "Metric")):
        variant_id = family_id if system == "US" else family_id * 10 + 1
        variants.append({
            "id": variant_id,
            "recipe_id": family_id,
            "name": name,
            "slug": slug,
            "source_url": source_url or parsed["url"],
            "canonical_variant_url": None,
            "serving_count": servings,
            "cooking_minutes": parsed["total_minutes"],
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
            "sibling_variant_ids": [family_id, family_id * 10 + 1],
            "family_dir": f"families/{family_id}/",
            "source": SOURCE,
            "backfill_bonus_fields": None,
            "description": localize_text(parsed["description"], system) if parsed["description"] else None,
            "notes": build_notes(parsed, optional, system),
            "author": parsed["authors"],
            "thumbnail_image_url": parsed["image_url"],
        })
    ctx.review["twins"] += 1
    return variants
