"""
Recipe nutrition from ingredient lines: parse each line's quantity, convert it to grams, look up
the ingredient's food in nutrition_foods.json, and sum per serving into the library's 66 fields.

Mealime recipes keep Mealime's own nutrition (never recalculated); this is for Albertsons native,
gap-fill and imported-site recipes. Running it on Mealime recipes is how the method is checked
(`validate`), since Mealime's numbers are the answer key.

GRAMS PER LINE, by quantity shape (shares are of non-Mealime lines):
  weight      "8 oz", "450 g"                 direct (7-15%)
  package     "½ (15 oz) can", "1 (227 g) block"  count x stated size (1-9%); a can matched to a
                                              "drained solids" food keeps DRAINED_YIELD of its weight
  volume      "¾ cup", "2 tbsp", "4 fl oz"    mL x the food's density from its portion data (55%);
                                              prep words in the line ("chopped", "sliced") pick the
                                              matching portion, e.g. onion: cup chopped 160 g vs sliced 115 g
  piece       "2 medium", "4 cloves", "1 head"   the food's portion for that word (12-14%)
  bare count  "1", "24 whole"                 the food's "medium" (else first per-piece) portion (7-12%)
  pinch etc.  pinch = 1/16 tsp, dash = 1/8 tsp, drop = 0.05 mL, spritz = 0.25 g
  no quantity "to taste" -- counts as zero (user rule), listed in coverage

Rules agreed with the user: "to taste" lines count as zero; salt that goes into drained cooking water
(a step listing 4+ cups of water with salt -- pasta, boiled potatoes) is subtracted from the salt line,
while salt absorbed by rice or couscous still counts; flour for dredging and marinades count in full;
added salt (a salt line with an amount)
DOES count toward sodium -- Mealime's own numbers leave it out, so `validate` drops salt lines
before comparing sodium; deep- and pan-fried items absorb oil equal to 15% of their weight, capped
at the oil listed (see apply_deep_frying for which oil lines count as frying oil);
imported-site nutrition is overwritten by the calculated values.
Kosher salt is weighed at 2.8 g/tsp (Diamond Crystal, what Serious Eats uses), not table salt's 6 g.

    python recipe_nutrition.py line "<quantity>" <ingredient id> ["<line name>"]   debug one line
    python recipe_nutrition.py build-batches        whole-batch foods for component_links targets
    python recipe_nutrition.py validate --snapshot <recipes.json>                  vs Mealime
    python recipe_nutrition.py validate --snapshot <recipes.json> --by-ingredient 15 [--field fat]
        ... plus the ingredients whose recipes lean consistently high or low
"""

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path

import nutrition_composite as nc
from library_paths import DEFAULT_LIBRARY_DIR

FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
             "⅙": 1 / 6, "⅚": 5 / 6, "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8}
NUM = r"(?:\d+(?:[.,]\d+)?(?:\s*[½¼¾⅓⅔⅛⅜⅝⅞⅙⅚⅕⅖⅗⅘])?|[½¼¾⅓⅔⅛⅜⅝⅞⅙⅚⅕⅖⅗⅘]|\d+\s*/\s*\d+|\d+\s+\d+\s*/\s*\d+)"
AMOUNT = rf"(?P<a>{NUM})(?:\s*(?:-|–|to)\s*(?P<b>{NUM}))?"

GRAMS_PER = {"g": 1.0, "kg": 1000.0, "oz": 28.3495, "lb": 453.592}
ML_PER = {"tsp": 4.92892, "tbsp": 14.7868, "cup": 236.588, "fl oz": 29.5735, "ml": 1.0, "l": 1000.0,
          "pint": 473.176, "quart": 946.353, "gallon": 3785.41}
UNIT_ALIASES = {
    "g": "g", "gram": "g", "grams": "g", "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "oz": "oz", "ounce": "oz", "ounces": "oz", "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp", "tbsp": "tbsp", "tablespoon": "tbsp",
    "tablespoons": "tbsp", "cup": "cup", "cups": "cup", "fl oz": "fl oz", "ml": "ml", "l": "l",
    "liter": "l", "liters": "l", "litre": "l", "pint": "pint", "pints": "pint", "quart": "quart",
    "quarts": "quart", "gallon": "gallon", "gallons": "gallon",
}
SIZES = {"small", "medium", "large", "extra-large"}
SIZE_FACTOR = {"small": 0.7, "medium": 1.0, "large": 1.35, "extra-large": 1.6}   # relative to a medium piece
NOUNS = {"clove", "head", "bunch", "stalk", "sprig", "slice", "ear", "leaf", "rib", "stick", "crown", "heart",
         "cap", "piece", "knob", "fillet", "strip", "stem", "wedge", "pkg", "package", "whole",
         "sheet", "packet", "envelope", "rack", "bottle", "loaf", "can", "jar", "bag", "block", "pod", "berry",
         "link", "cube", "bulb", "pack", "chunk", "serving", "portion"}
# nouns a line NAME may supply for a bare size ("2 medium" + "garlic cloves" = 2 medium cloves);
# others ("cut into wedges") describe preparation, not the unit
NAME_NOUNS = ("clove", "stalk", "sprig", "head", "leaf")
PIECES = SIZES | NOUNS
VAGUE_ML = {"pinch": ML_PER["tsp"] / 16, "dash": ML_PER["tsp"] / 8, "drop": 0.05, "splash": ML_PER["tsp"] / 2,
            "handful": ML_PER["cup"] / 4, "drizzle": ML_PER["tsp"]}
SPRITZ_G = 0.25
DRAINED_YIELD = 0.6           # a 15 oz can of beans drains to ~250 g of its 425 g
CAN_G_PER_ML = 425 / 398      # a 398 mL can holds the same 15 oz (425 g) as a US can
KOSHER_SALT_G_PER_ML = 2.8 / ML_PER["tsp"]
PREP_WORDS = ("chopped", "diced", "minced", "sliced", "grated", "shredded", "crumbled", "cubed", "mashed",
              "packed", "sifted", "whole", "halves", "pieces", "julienned", "ground")


def _num(text: str) -> float:
    text = text.strip().replace(",", ".")
    total = 0.0
    for part in re.findall(r"\d+\s*/\s*\d+|\d+(?:\.\d+)?|[½¼¾⅓⅔⅛⅜⅝⅞⅙⅚⅕⅖⅗⅘]", text):
        if "/" in part:
            n, d = part.split("/")
            total += float(n) / float(d)
        elif part in FRACTIONS:
            total += FRACTIONS[part]
        else:
            total += float(part)
    return total


def _singular(word: str) -> str:
    word = word.lower().rstrip(".")
    if word in ("leaves",):
        return "leaf"
    if word in ("berries",):
        return "berry"
    if word.endswith("es") and word[:-2] in ("bunch",):
        return word[:-2]
    return word[:-1] if word.endswith("s") and word[:-1] in PIECES else word


def parse_quantity(q: str) -> dict:
    """{"kind": weight|volume|package|piece|count|vague|spritz|size|empty|unknown, ...}"""
    q = re.sub(r"^(about|approx\.?|approximately|roughly|generous|heaping|scant)\s+", "", (q or "").strip(), flags=re.I)
    if not q:
        return {"kind": "empty"}
    m = re.match(rf"^\s*{AMOUNT}\s*(?P<rest>.*)$", q)
    if not m:
        return {"kind": "unknown", "text": q}
    amount = _num(m["a"]) if not m["b"] else (_num(m["a"]) + _num(m["b"])) / 2
    rest = m["rest"].strip()
    # package / size in parentheses: "(15 oz) can", "(2 inch) piece", "(794 g) (794 g) can"
    p = re.match(rf"^\(\s*(?P<n>{NUM})(?:\s*(?P<op>-|–|to|x|×)\s*(?P<n2>{NUM}))?\s*-?\s*(?P<u>fl\.? oz|[a-zA-Z]+)\.?\s*\)"
                 rf"\s*(?:\([^)]*\)\s*)*(?P<what>.*)$", rest)
    if p:
        unit = p["u"].lower().replace("fl. oz", "fl oz")
        inner = _num(p["n"])
        if p["n2"] and p["op"] not in ("x", "×"):        # "7 to 8 inch" -> 7.5
            inner = (inner + _num(p["n2"])) / 2
        what = p["what"].strip().lower()
        if unit in ("inch", "in", "cm"):
            return {"kind": "size", "count": amount, "size": inner, "size_unit": unit, "what": what}
        unit = UNIT_ALIASES.get(unit)
        if unit in GRAMS_PER:
            return {"kind": "package", "count": amount, "grams_each": inner * GRAMS_PER[unit], "what": what}
        if unit in ML_PER:
            return {"kind": "package", "count": amount, "ml_each": inner * ML_PER[unit], "what": what}
        return {"kind": "unknown", "text": q}
    words = rest.lower().split()
    first = words[0].rstrip(".,") if words else ""
    two = " ".join(words[:2]).rstrip(".,").replace("fl. oz", "fl oz")
    if two == "fl oz":
        return {"kind": "volume", "ml": amount * ML_PER["fl oz"]}
    unit = UNIT_ALIASES.get(first)
    if unit in GRAMS_PER:
        return {"kind": "weight", "grams": amount * GRAMS_PER[unit]}
    if unit in ML_PER:
        return {"kind": "volume", "ml": amount * ML_PER[unit], "text": rest.lower()}
    if first == "dozen":                       # "1 dozen" eggs/clams -> 12 of them
        return parse_quantity(f"{amount * 12:g} {' '.join(words[1:])}".strip())
    if first == "extra" and len(words) > 1 and words[1].startswith("large"):
        words = ["extra-large"] + words[2:]
        first = "extra-large"
    # "1 inch piece", "½ inch" written without parentheses
    if first in ("inch", "inches", "in", "cm"):
        return {"kind": "size", "count": 1.0, "size": amount, "size_unit": "cm" if first == "cm" else "inch",
                "what": " ".join(words[1:])}
    # "1 large pinch", "1 small handful", "1 generous pinch": a sized vague amount
    scale = {"small": 0.7, "tiny": 0.5, "large": 1.5, "big": 1.5, "generous": 1.5, "good": 1.5}
    if first in scale and len(words) > 1:
        inner = parse_quantity(f"1 {' '.join(words[1:])}")
        if inner.get("vague"):
            return {**inner, "ml": amount * inner["ml"] * scale[first], "vague": f"{first} {inner['vague']}"}
    base = _singular(first)
    if base in VAGUE_ML or base.rstrip("e") in VAGUE_ML or first in ("pinches", "dashes", "drops", "handfuls", "drizzles"):
        key = next(k for k in VAGUE_ML if first.startswith(k))
        return {"kind": "volume", "ml": amount * VAGUE_ML[key], "vague": key}
    if first.startswith("spritz"):
        return {"kind": "spritz", "count": amount}
    if base in PIECES:
        size = base if base in SIZES else None
        noun = base if base in NOUNS else None
        if size and len(words) > 1 and _singular(words[1].rstrip(".,")) in NOUNS:
            noun = _singular(words[1].rstrip(".,"))
        if noun == "package":
            noun = "pkg"
        return {"kind": "piece", "count": amount, "size": size, "noun": noun,
                "word": " ".join(w for w in (size, noun) if w), "text": rest.lower()}
    if not first:
        return {"kind": "count", "count": amount}
    return {"kind": "unknown", "text": q}


# --- portions -> grams ---------------------------------------------------------------------------

def _portion_ml(unit: str):
    """mL in one portion whose unit is a volume ("cup, chopped", "tbsp", "fl oz", "100ml")."""
    u = unit.lower()
    m = re.match(r"^(\d+)\s*ml", u)
    if m:
        return float(m[1])
    for word, key in (("cup", "cup"), ("tbsp", "tbsp"), ("tablespoon", "tbsp"), ("tsp", "tsp"),
                      ("teaspoon", "tsp"), ("fl oz", "fl oz"), ("pint", "pint"), ("ml", "ml")):
        if u.startswith(word):
            return ML_PER[key]
    return None


def estimated_density(food: dict):
    """g/mL from a food's composition when it has no volume portion: oils ~0.92; syrups and honey
    ~1.35; watery or alcoholic liquids ~1.0, heavier with sugar, lighter with alcohol. Thick or dry
    foods (spice blends, spreads, seeds) get no estimate -- they need a reviewed density."""
    n = food.get("per_100g", {})
    water, fat, sugars, alcohol = (n.get(k, 0.0) for k in ("water", "fat", "sugars", "alcohol"))
    if fat >= 95:
        return 0.92
    if sugars >= 60 and water >= 15:
        return 1.35
    if water + alcohol >= 55 and fat < 10:
        return round(1.0 + 0.0045 * sugars - 0.002 * alcohol, 3)
    if "water" not in n and n.get("energy", 999) <= 40 and fat < 1:
        return 1.0      # grocery labels list no water: a near-zero-calorie, fat-free item is water-like
    return None


def density(food: dict, line_text: str = ""):
    """(grams per mL, portion label) from the food's volume portions; a prep word in the line
    ("chopped", "sliced") selects the matching portion when there is one. With no volume portion,
    a liquid's density is estimated from its composition (estimated_density)."""
    vols = [(p, _portion_ml(p["unit"])) for p in food.get("portions", [])]
    vols = [(p, ml) for p, ml in vols if ml]
    if not vols:
        d = estimated_density(food)
        return (d, f"estimated density {d} g/mL") if d else (None, None)
    text = line_text.lower()
    if "whipped" not in text:   # "cup, whipped" is half the weight of a cup of liquid cream ("heavy whipping cream")
        vols = [(p, ml) for p, ml in vols
                if "whip" not in p["unit"].lower() or "fluid" in p["unit"].lower()] or vols
    for word in PREP_WORDS:
        if word in text:
            for p, ml in vols:
                if word in p["unit"].lower():
                    return p["grams"] / (p["amount"] * ml), p["unit"]
    p, ml = vols[0]
    return p["grams"] / (p["amount"] * ml), p["unit"]


def _has_word(label: str, word: str) -> bool:
    forms = {"leaf": r"lea(f|ves)"}.get(word, re.escape(word) + r"(e?s)?")
    return re.search(r"\b" + forms + r"\b", label.lower()) is not None


def piece_grams(food: dict, word: str, size: str = None):
    """Grams of one piece from the food's portions: a label containing the word ("clove",
    'head medium (5" dia)', "Potato medium"), preferring one that also names the size."""
    hits = [p for p in food.get("portions", []) if _has_word(p["unit"], word)]
    if size:
        sized = [p for p in hits if _has_word(p["unit"], size)]
        if sized:
            return sized[0]["grams"] / sized[0]["amount"], sized[0]["unit"]
    if hits:
        p = hits[0]
        labelled = any(_has_word(p["unit"], z) for z in SIZES)
        factor = SIZE_FACTOR.get(size, 1.0) if size and not labelled else 1.0
        return factor * p["grams"] / p["amount"], p["unit"] + (f" x {factor}" if factor != 1.0 else "")
    return None, None


def size_grams(food: dict, size: str):
    """Grams of one whole item of a size: a portion whose name part (before any "(" or ",") has
    the size word, e.g. "large", "medium (2-1/2\" dia)", "Potato medium" -- not "cup (4.86 large
    eggs)" or "slice, medium"."""
    hits = []
    for p in food.get("portions", []):
        head = re.split(r"[(,]", p["unit"].lower())[0].split()
        if (size in head and not _portion_ml(p["unit"]) and head[0] not in ("slice", "slices", "ring", "rings")
                and not {"extra", "jumbo"} & set(head)):
            hits.append((head != [size], p))           # a label that is just the size word wins
    if hits:
        p = min(hits, key=lambda h: h[0])[1]
        return p["grams"] / p["amount"], p["unit"]
    return None, None


def count_grams(food: dict, line_name: str = ""):
    """Grams of one item for a bare count: a portion named after the item itself ("1 tortilla"
    for "corn tortillas"), else "medium", else the first portion that is neither a volume nor a
    weight nor a serving/package label."""
    items = [p for p in food.get("portions", [])
             if not _portion_ml(p["unit"]) and not re.match(r"^(oz|lb|nlea|serving|package|container|can|cubic|\d)",
                                                            p["unit"].lower())]
    words = {w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{4,}", line_name.lower())}
    for p in items:   # the label must START with the item ("tortilla (approx 12\" dia)"), so
        first = re.findall(r"[a-z]+", p["unit"].lower())[:1]   # "wedge or slice (1/8 of one lemon)" is skipped
        if first and (first[0][:-1] if first[0].endswith("s") else first[0]) in words:
            return p["grams"] / p["amount"], p["unit"]
    g, label = size_grams(food, "medium")
    if g:
        return g, label
    return (items[0]["grams"] / items[0]["amount"], items[0]["unit"]) if items else (None, None)


def line_grams(parsed: dict, food: dict, line_name: str = "", measures: dict = None):
    """(grams, how) for one parsed line, or (None, reason). measures: the ingredient's reviewed
    overrides from nutrition_foods.json ("small bunch": 30, "inch": 6, "item": 25, "density": 1.01),
    consulted before the food's own portion data."""
    measures = measures or {}
    kind = parsed["kind"]
    if kind == "empty":
        return 0.0, "to taste (counted as zero)"
    if kind == "weight":
        return parsed["grams"], "weight"
    if kind == "package":
        # cans and jars of foods eaten drained (beans, lentils, marinated artichokes): the food is
        # a drained-solids entry, or the ingredient has a reviewed "drained" yield
        drained = measures.get("drained") or (DRAINED_YIELD if "drained" in (food.get("name") or "").lower() else None)
        if drained and re.match(r"(can|jar|tin)", parsed["what"]):
            each = parsed.get("grams_each") or parsed["ml_each"] * CAN_G_PER_ML
            if 245 <= each <= 265:       # "(255 g) can": Mealime's metric cans give the drained weight
                return parsed["count"] * each, "can, drained weight given"
            return parsed["count"] * each * drained, f"can/jar, drained ({drained:.0%} of contents)"
        if "grams_each" in parsed:
            grams = parsed["count"] * parsed["grams_each"]
            if "package yield" in measures:      # "1 (30 oz) whole rotisserie chicken": bones
                return grams * measures["package yield"], f"package weight x reviewed yield {measures['package yield']}"
            return grams, "package weight"
        d, label = (measures["density"], "reviewed density") if "density" in measures else density(food, line_name)
        return (parsed["count"] * parsed["ml_each"] * d, f"package volume @ {label}") if d else (None, "no density")
    if kind == "volume":
        if "salt" in line_name.lower() and "kosher" in line_name.lower():
            return parsed["ml"] * KOSHER_SALT_G_PER_ML, "kosher salt 2.8 g/tsp"
        if "density" in measures:
            return parsed["ml"] * measures["density"], f"volume @ reviewed density {measures['density']} g/mL"
        d, label = density(food, line_name + " " + parsed.get("text", ""))
        return (parsed["ml"] * d, f"volume @ {label}") if d else (None, "no density")
    if kind == "spritz":
        return parsed["count"] * SPRITZ_G, "spritz"
    if kind == "piece":
        size, noun = parsed["size"], parsed["noun"]
        if not noun:   # "2 medium" with the noun in the line name: "garlic cloves" -> medium cloves
            noun = next((n for n in NAME_NOUNS if _has_word(line_name, n)), None)
        for key in filter(None, (" ".join(w for w in (size, noun) if w), noun, size and "item")):
            if key in measures:
                factor = SIZE_FACTOR.get(size, 1.0) if key in (noun, "item") and size else 1.0
                return parsed["count"] * measures[key] * factor, f"reviewed '{key}'" + (f" x {factor}" if factor != 1 else "")
        if noun in (None, "whole"):
            g, label = size_grams(food, size) if size else (None, None)
            if not g:
                g, label = count_grams(food, line_name)
                if g and size:
                    g, label = g * SIZE_FACTOR[size], f"{label} x {SIZE_FACTOR[size]}"
        else:
            g, label = piece_grams(food, noun, size)
            if not g and size and parsed["noun"] is None:      # the name's noun had no portion
                g, label = size_grams(food, size)
        return (parsed["count"] * g, f"piece @ {label}") if g else (None, f"no '{size or ''} {noun or ''}' portion".replace("  ", " "))
    if kind == "count":
        if "baby item" in measures and re.search(r"\bbaby\b", line_name, re.I):   # "6 baby bok choy"
            return parsed["count"] * measures["baby item"], "reviewed per-item weight (baby)"
        if "item" in measures:
            return parsed["count"] * measures["item"], "reviewed per-item weight"
        g, label = count_grams(food, line_name)
        return (parsed["count"] * g, f"count @ {label}") if g else (None, "no per-item portion")
    if kind == "size":
        unit = "inch" if parsed["size_unit"] in ("inch", "in") else "cm"
        per = measures.get(unit) or (measures["inch"] / 2.54 if unit == "cm" and "inch" in measures else None)
        if per:
            return parsed["count"] * parsed["size"] * per, f"reviewed per-{unit} weight"
        g, label = (measures["item"], "reviewed per-item weight") if "item" in measures else count_grams(food, line_name)
        if g:
            return parsed["count"] * g, f"count (size given) @ {label}"
        return None, f"no per-{unit} weight"
    return None, f"cannot parse ({kind})"


# --- recipes -------------------------------------------------------------------------------------

# Bone-in cuts bought by weight: the line weighs bone too, while the food (meat and skin, or
# separable lean and fat) is per gram of edible meat. Edible share of the purchased weight,
# from USDA refuse figures, rounded; the first pattern that matches the line name wins.
BONE_IN_YIELDS = [
    (r"\bwings?\b|\bwingettes?\b|\bdrumettes?\b", 0.55),
    (r"\boxtails?\b|\bnecks?\b|\bfeet\b|\btrotters?\b|\bhocks?\b", 0.45),
    (r"\bshort ribs?\b", 0.55),
    (r"\b(spare|back|baby back|st\.? louis|country[- ]style)? ?ribs?\b(?! ?eye)|\brack of\b|\bracks?\b", 0.65),
    (r"\bshanks?\b|\bosso buco\b", 0.60),
    (r"\bwhole (chicken|turkey|duck|goose|hen|bird|fish|branzin\w*|trout|snapper|bass|mackerel|sardines?)|\bcornish hens?\b|\bspatchcock", 0.65),
    (r"\bdrumsticks?\b|\bleg quarters?\b|\b(chicken|duck|turkey) legs?\b", 0.72),
    (r"\b(chicken|turkey) (parts|pieces)\b", 0.72),
    (r"\bthighs?\b", 0.80),
    (r"\bbreasts?\b", 0.85),
    (r"\bchops?\b|\bsteaks?\b|\bribeye|\bt-bone|\bporterhouse|\bleg of lamb\b|\bshoulder\b|\broast\b|\bbutt\b", 0.85),
]
BONE_IN = re.compile(r"\bbone-in\b|\bon the bone\b|\bwings?\b|\bdrumsticks?\b|\bwhole (chicken|turkey|duck|goose|hen|bird|fish|"
                     r"branzin\w*|trout|snapper|bass|mackerel)|\bcornish hens?\b|\bleg quarters?\b|\b(chicken|duck|turkey) legs?\b|"
                     r"\b(spare|back|short)\s?ribs?\b|\bbaby back\b|\boxtails?\b|\bshanks?\b|\bosso buco\b|"
                     r"\b(chicken|turkey) (parts|pieces)\b", re.I)
MEAT_FOOD = re.compile(r"^(Chicken|Turkey|Duck|Goose|Pork|Beef|Lamb|Veal|Goat|Game meat|Fish|Quail|Pheasant|Cornish)", re.I)


BONELESS = re.compile(r"\bboneless\b|\bbone(s)? removed\b|\bdeboned\b|\bmeat (only|from)\b|\bpicked\b|\bshredded\b", re.I)


def bone_in_yield(line_name: str, food: dict, reviewed: float = None):
    """Edible share for a bone-in cut bought by weight, or None. "boneless" lines never match. `reviewed` is the
    ingredient's own "bone in" measure, for lines whose name doesn't say it ("4 lb chicken, cut into 8 pieces")."""
    name = line_name or ""
    if BONELESS.search(name):
        return None
    if not MEAT_FOOD.match(food.get("name") or ""):
        return None
    if not BONE_IN.search(name):
        return reviewed
    return next((y for pat, y in BONE_IN_YIELDS if re.search(pat, name, re.I)), reviewed or 0.75)


# Recipes that ARE a stock or broth (not a soup that keeps its meat): the solids are strained out.
STOCK_NAME = re.compile(r"\b(stock|broth|dashi|consomm[eé]|fumet|brodo)\b", re.I)
NOT_STOCK = re.compile(r"\b(tonkotsu|paitan|creamy|soup|noodles?|udon|mazemen|mussels|salmon|halibut|cod|hot pot|vinaigrette|tamago|ohitashi|"
                       r"choy|spinach|caldo|arroz|scotch broth|with|poached)\b", re.I)


def strained_stock(recipe: dict) -> bool:
    name = recipe.get("name") or ""
    steps = " ".join((s.get("primary_message") or "") if isinstance(s, dict) else (s or "") for s in recipe.get("instructions", []))
    return bool(STOCK_NAME.search(name) and not NOT_STOCK.search(name) and re.search(r"\bstrain", steps, re.I))


def apply_strained_stock(recipe: dict, lines: list, foods: dict) -> None:
    """A stock's bones, meat, vegetables and aromatics are strained out and discarded, so they count
    as zero; the liquids (water, wine, sake, soy sauce: volume lines that are mostly water) and salt
    stay in the stock."""
    if not strained_stock(recipe):
        return
    for l in lines:
        if not l.get("grams"):
            continue
        n = foods[l["ref"]]["per_100g"]
        liquid = (l.get("how") or "").startswith("volume") and n.get("water", 0) + n.get("alcohol", 0) >= 80
        if liquid or l["ingredient_id"] in SALT_IDS or "salt" in (l["name"] or "").lower():
            continue
        l["grams_listed"], l["grams"] = l["grams"], 0.0
        l["how"], l["status"] = "strained out of the stock (counted as zero)", "zero"


SALT_IDS = {"88", "1472", "1508"}   # salt, flaky sea salt, fine sea salt; Mealime's nutrition never counts these
DRAINED_WATER_ML = 4 * ML_PER["cup"]   # this much water with salt in one step = drained cooking water


BOILED_AND_DRAINED = re.compile(
    r"\b(pasta|noodles?|spaghetti|rotini|penne|macaroni|orzo|fusilli|linguine|fettuccine|gnocchi|ravioli|"
    r"tortellini|rigatoni|farfalle|ziti|udon|soba|potato(es)?|drain)\b", re.I)


def drained_water_salt(recipe: dict) -> list:
    """Salt quantities ("1 tbsp") from steps that salt a pot of cooking water that gets drained.
    Mealime-format steps list their ingredients one per line: "about 7 cups water / 1 tbsp salt /
    6 oz rotini". A step counts when it has 4+ cups of water and salt AND a sign the water is
    drained -- the water is "about" N cups (a pasta pot), the step lists pasta or potatoes, or its
    text says drain -- so a soup's 6 cups of water keeps its salt."""
    out = []
    if re.search(r"\bone[- ]pot\b", recipe.get("name") or "", re.I):   # the pasta absorbs that water
        return out
    for step in recipe.get("instructions", []):
        text = step.get("secondary_message") if isinstance(step, dict) else step
        primary = step.get("primary_message", "") if isinstance(step, dict) else ""
        lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
        water = [(l, parse_quantity(l)) for l in lines if re.search(r"\bwater\b", l, re.I)]
        big = [l for l, w in water if w.get("kind") == "volume" and w["ml"] >= DRAINED_WATER_ML]
        if big and (any(l.lower().startswith("about") for l in big) or BOILED_AND_DRAINED.search(f"{text} {primary}")):
            out += [l for l in lines if re.search(r"\bsalt\b", l, re.I)]
    return out


FRYING_OIL = re.compile(r"for (deep[- ]?)?fry|deep[- ]?fry|for frying|frying (oil|fat)", re.I)
FAT_FOOD_MIN = 90              # g fat per 100 g: oils, lard, shortening, ghee, schmaltz
FRYING_ALWAYS_G = 800          # ~1 quart of oil (about 860 g): no recipe eats that much, so it is frying oil
FRYING_WITH_STEP_G = 100       # ~1/2 cup: frying oil when the steps show it is a frying medium
FRY_MEDIUM = re.compile(r"paper towel|drain (them |it |well )?on|\b3[2-9]\d ?(°|degrees|F\b)|oil (to|reaches|registers|is) (about )?3\d\d|"
                        r"in batches|deep[- ]?fr|shallow[- ]?fr|pan[- ]?fr|until (golden|crisp)\w*,? (about )?\d+ (to \d+ )?min|"
                        r"hot (fat|oil)|wire rack|slotted spoon|spider|a (few|couple)( of)?( \w+)? at a time", re.I)
OIL_KEPT = re.compile(r"reserv\w* (the )?(frying |cooking )?oil|strain (the )?oil|pour (the )?(hot |infused |chile |chili )?oil over|"
                      r"oil (in|into|to) a (jar|heatproof|bowl|container)|infused|chili oil|chile oil", re.I)
OIL_PRODUCT_NAME = re.compile(r"\boil\b|salsa macha|vinaigrette|dressing|mayo|aioli|pesto|chimichurri|crisp\b", re.I)
FRY_STEP = re.compile(r"\b(deep[- ]?)?(fry|fries|fried|frying)\b|\boil (to|reaches|registers) 3\d\d", re.I)
SIDE_COMPONENT = re.compile(r"^\s*for (the )?[^:]{0,40}\b(sauce|dip|dipping|dressing|glaze|salsa|aioli|mayo|"
                            r"mayonnaise|topping|garnish|slaw|salad|relish|chutney|syrup|serving|pickles?)\b[^:]{0,20}:", re.I)
OIL_ABSORBED = 0.15            # user rule: deep-fried items absorb oil equal to 15% of their weight
GENERIC_WORDS = {"fresh", "freshly", "large", "small", "medium", "white", "kosher", "ground", "plus", "more",
                 "about", "sliced", "chopped", "minced", "peeled", "cold", "ice", "cut", "into", "pieces",
                 "finely", "thinly", "divided", "optional", "whole", "extra", "virgin", "dried", "packed"}


def _keywords(name: str) -> set:
    words = re.findall(r"[a-z]{4,}", re.split(r",|\(", (name or "").lower())[0])
    return {w[:-1] if w.endswith("s") else w for w in words if w not in GENERIC_WORDS}


def _mentions(step_text: str, keywords: set) -> bool:
    text = {w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{4,}", step_text.lower())}
    return bool(keywords & text)


def frying_oil_lines(lines: list, steps: list, foods: dict, recipe_name: str = "") -> list:
    """Oil/fat lines that are frying oil: worded so ("vegetable oil, for frying"), or a fat
    (>= 90% fat) of about a quart or more, or about 1/2 cup or more when the steps show the oil
    is a frying medium (drain on paper towels, heat to 350 deg, fry in batches -- deep or pan
    frying; Serious Eats drops "for frying" from its ingredient names). The 1/2-cup case is
    skipped when the oil is kept and eaten (strained and reserved, poured over, an infused or
    chili oil) or the recipe makes an oil, dressing or sauce."""
    text = " ".join(steps)
    fries = (any(FRY_STEP.search(s) for s in steps) and FRY_MEDIUM.search(text)
             and not OIL_KEPT.search(text) and not OIL_PRODUCT_NAME.search(recipe_name))
    out = []
    for l in lines:
        if not l.get("ref"):
            continue
        if FRYING_OIL.search(f"{l['name']} {l['quantity'] or ''}"):
            out.append(l)
            continue
        if re.search(r"dripping", l["name"] or "", re.I):     # pan drippings are juices + fat, not a frying medium
            continue
        fat = (foods.get(l["ref"]) or {}).get("per_100g", {}).get("fat") or 0
        grams = l.get("grams") or 0
        if fat >= FAT_FOOD_MIN and (grams >= FRYING_ALWAYS_G or (fries and grams >= FRYING_WITH_STEP_G)):
            out.append(l)
    return out


def apply_deep_frying(recipe: dict, lines: list, foods: dict) -> None:
    """Frying-oil lines (frying_oil_lines) count only the oil absorbed: 15% of the weight of what
    is fried, never more than the oil listed. What is fried is every other counted line except
    ones that appear only in side-component steps ("For the Dipping Sauce: ...") or only in steps
    after the last frying step (serving, garnish). Imported-site steps have no ingredient lists,
    so mentions are matched by name words."""
    steps = [(s.get("primary_message") or "") if isinstance(s, dict) else (s or "") for s in recipe.get("instructions", [])]
    oil = frying_oil_lines(lines, steps, foods, recipe.get("name") or "")
    if not oil:
        return
    fry_idx = [i for i, s in enumerate(steps) if FRY_STEP.search(s)]
    last_fry = fry_idx[-1] if fry_idx else len(steps) - 1
    fried_g = 0.0
    for l in lines:
        if l in oil or not l.get("grams"):
            continue
        kw = _keywords(l["name"])
        where = [i for i, s in enumerate(steps) if kw and _mentions(s, kw)]
        if where and all(SIDE_COMPONENT.search(steps[i]) or i > last_fry for i in where):
            continue
        fried_g += l["grams"]
    # cap at the oil listed -- unless a line has no amount ("vegetable oil, for frying")
    listed = sum(l.get("grams") or 0 for l in oil) if all(l.get("grams") for l in oil) else float("inf")
    absorbed = min(fried_g * OIL_ABSORBED, listed)
    for i, l in enumerate(oil):
        l["grams_listed"] = l.get("grams")
        l["grams"] = round(absorbed, 1) if i == 0 else 0.0
        l["how"] = (f"deep frying: {OIL_ABSORBED:.0%} of {fried_g:.0f} g fried"
                    + (" (capped at the oil listed)" if absorbed == listed else "")) if i == 0 \
            else "deep frying: counted on first oil line"
        l["status"] = "counted" if l["grams"] else "zero"


# --- ingredient lines left blank ------------------------------------------------------------------
# A blank line ("easy pie dough", "vegetable oil", "lime wedges") is, in order: a component another
# library recipe makes (component_links: a share of that recipe's batch); served to taste
# (served_to_taste: zero, noted); cooking oil or butter (assumed 1 tbsp); a component nobody has
# linked yet (not converted, so the recipe is partial); anything else is to taste (zero).
# A line whose amount is "½ recipe" / "1 batch" names another recipe too: it counts that many of the
# linked batch (link "per_recipe" scales a stand-in batch; default 1), and is not converted when unlinked.
COOKING_FAT = re.compile(r"^(a few tablespoons (of )?)?(extra[- ]virgin olive oil|olive oil|vegetable oil|neutral oil|canola oil|"
                         r"peanut oil|grapeseed oil|sunflower oil|rice bran oil|cooking oil|oil|unsalted butter|butter|"
                         r"clarified butter|ghee|lard)\b", re.I)
BLANK_SMALL = re.compile(r"\b(for (garnish|dusting|serving|sprinkling|decorating|topping|rolling)|to (garnish|serve|taste)|"
                         r"optional|if desired|garnish|wedges?|twists?|zest|peel|leaves|club soda|seltzer|soda water)\b", re.I)
BLANK_COMPONENT = re.compile(r"\b(dough|crust|pastry|batter|buttercream|frosting|icing|filling|stuffing|gravy|sauce|aioli|"
                             r"dressing|vinaigrette|salsa|chutney|curd|ganache|meringue|chips|shells|tortillas?|bread|"
                             r"rolls?|buns?|rice|noodles|pasta|stock|broth|pickled|pickles|jam|syrup|recipe|homemade)\b", re.I)


def component_key(name: str) -> str:
    """The lookup key for a blank component line: the name before any comma, parenthesis or
    "such as/from/for/prepared/made" clause, without a leading count ("double recipe ...")."""
    key = re.split(r",|\(| such as | from | for | prepared | made ", (name or "").lower())[0].strip()
    return re.sub(r"^(\d+|one|two|double|half|½|1/2)?\s*(batch(es)? of |recipes? |recipe )?", "", key).strip()


RECIPE_AMOUNT = re.compile(rf"^\s*{AMOUNT}\s*(recipes?|batch(es)?)\b", re.I)


def recipe_amount(quantity: str | None) -> float | None:
    """"½ recipe" / "1 batch" / "2 recipes" -> how many batches of another recipe; None otherwise."""
    m = RECIPE_AMOUNT.match(quantity or "")
    if not m:
        return None
    return _num(m["a"]) if not m["b"] else (_num(m["a"]) + _num(m["b"])) / 2


def foods_of(db: dict) -> dict:
    """Source foods plus the batch foods of linked recipes ("batch:<recipe id>")."""
    return {**db["foods"], **db.get("batches", {})}


def recipe_nutrition(recipe: dict, db: dict, skip_ids=()) -> dict:
    """Per-serving totals plus coverage: which lines were counted, zero, or left out and why.
    skip_ids: ingredient ids to leave out of the totals (validate uses SALT_IDS)."""
    totals, lines = {}, []
    foods = foods_of(db)
    drained_salt = drained_water_salt(recipe)
    override = db.get("recipe_overrides", {}).get(str(recipe.get("id")), {})
    for index, ing in enumerate(recipe.get("ingredients", [])):
        iid = str(ing.get("ingredient_id"))
        if iid in skip_ids:
            continue
        assign = db["ingredients"].get(iid)
        quantity = ing.get("quantity")
        if str(index) in override.get("quantity", {}) and not (quantity or "").strip():
            quantity = override["quantity"][str(index)]       # reviewed amount for a line the recipe left blank
        entry = {"ingredient_id": iid, "name": ing.get("name"), "quantity": quantity, "index": index}
        if not assign:
            entry.update(status="unmatched ingredient")
            lines.append(entry)
            continue
        if assign["ref"].startswith("none:"):      # removed before eating (bouquet garni, husks)
            entry.update(grams=0.0, how="removed before eating (counted as zero)", ref=assign["ref"], status="zero")
            lines.append(entry)
            continue
        parsed = parse_quantity(quantity)
        if assign.get("removed_when_whole") and parsed["kind"] in ("count", "piece", "size"):
            # whole spices that are fished out ("2 bay leaves", "1 cinnamon stick", "2 stalks
            # lemongrass"); measured by volume or weight ("2 tbsp minced lemongrass") they count
            entry.update(grams=0.0, how="whole spice, removed before eating (counted as zero)",
                         ref=assign["ref"], status="zero")
            lines.append(entry)
            continue
        ref = assign["ref"]
        measures = db.get("measures", {}).get(iid) or {}
        if parsed["kind"] == "package" and measures.get("can ref") and re.match(r"(can|jar|tin)", parsed["what"]):
            ref = measures["can ref"]      # "1 (15 oz) can lentils": cooked, not the dry lentils the cup lines use
        # Mealime's blank lines are optional extras its own numbers leave out, so they stay "to taste"
        recipes_n = recipe_amount(quantity)
        if (parsed["kind"] == "empty" or recipes_n is not None) and recipe.get("source") not in (None, "authenticated_backfill"):
            name = ing.get("name") or ""
            key = component_key(name)
            link = db.get("component_links", {}).get(key)
            if link:
                # a library recipe's batch, or a food standing in for a recipe the library lacks ("food" + its "grams")
                bref = link["food"] if "food" in link else f"batch:{link['recipe']}"
                batch = foods.get(bref)
                if batch and "food" in link:
                    batch = {**batch, "batch_grams": link["grams"]}
                # "½ recipe X" counts half of X's batch; with no amount the link's share is the reviewed guess
                share = recipes_n * link.get("per_recipe", 1) if recipes_n is not None else link["share"]
                if batch:
                    entry.update(grams=round(batch["batch_grams"] * share, 1), ref=bref,
                                 how=f"linked recipe: {share:g} x batch of {batch['name']}", status="counted")
                else:
                    entry.update(grams=None, how="linked recipe not built yet", status="not converted")
                lines.append(entry)
                continue
            if key in db.get("served_to_taste", {}):
                entry.update(grams=0.0, ref=ref, how="served to taste (not counted)", status="zero")
                lines.append(entry)
                continue
            if recipes_n is not None:
                entry.update(grams=None, ref=ref, how=f"{quantity} of a recipe not linked yet", status="not converted")
                lines.append(entry)
                continue
            if COOKING_FAT.match(name) and not FRYING_OIL.search(name):
                parsed = parse_quantity("1 tbsp")
                entry["assumed"] = "1 tbsp"
            elif BLANK_COMPONENT.search(name) and not BLANK_SMALL.search(name):
                entry.update(grams=None, ref=ref, how="component with no amount", status="not converted")
                lines.append(entry)
                continue
        food = foods[ref]
        grams, how = line_grams(parsed, food, ing.get("name") or "", measures)
        if entry.get("assumed"):
            how = f"{how} (amount not given; assumed {entry['assumed']})"
        if grams and parsed["kind"] in ("weight", "package") and not measures.get("package yield"):
            y = bone_in_yield(ing.get("name"), food, measures.get("bone in"))
            if y:
                grams, how = grams * y, f"{how}, x {y} edible (bone-in)"
        if grams and iid in SALT_IDS and drained_salt:
            water_g = sum(line_grams(parse_quantity(q), food, q)[0] or 0 for q in drained_salt)
            grams, how = max(0.0, grams - water_g), f"{how}, minus {water_g:.1f} g for cooking water"
            drained_salt = []
        entry.update(grams=None if grams is None else round(grams, 1), how=how, ref=ref,
                     status="counted" if grams else ("zero" if grams == 0 else "not converted"))
        lines.append(entry)
    by_index = {l["index"]: l for l in lines}
    for idx, factor in override.get("scale", {}).items():      # e.g. one of five alternative glazes
        l = by_index.get(int(idx), {})
        if l.get("grams"):
            l["grams"] = round(l["grams"] * factor, 1)
            l["how"] = f"{l['how']}, x {factor:g} ({override.get('note', 'recipe override')})"
    apply_strained_stock(recipe, lines, foods)
    apply_deep_frying(recipe, lines, foods)
    for l in lines:
        if l.get("grams"):
            for field, v in foods[l["ref"]]["per_100g"].items():
                totals[field] = totals.get(field, 0.0) + v * l["grams"] / 100
    servings = recipe.get("serving_count") or recipe.get("servings") or 1
    per_serving = {f: round(v / servings, 2) for f, v in totals.items()}
    return {"per_serving": per_serving, "lines": lines,
            "complete": all(l["status"] in ("counted", "zero") for l in lines)}


def cmd_validate(args, db):
    recipes = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    mealime = [r for r in recipes if r["source"] in (None, "authenticated_backfill")
               and r["units"] == "US" and r.get("nutrition")]
    complete, fields = [], ("energy", "protein", "fat", "carbs", "fiber", "sugars", "sodium", "saturated")
    problems = {}
    for r in mealime:
        res = recipe_nutrition(r, db, skip_ids=SALT_IDS)
        for l in res["lines"]:
            if l["status"] not in ("counted", "zero"):
                key = (l["status"], l.get("how"), l["name"])
                problems[key] = problems.get(key, 0) + 1
        if res["complete"]:
            complete.append((r, res))
    print(f"Mealime US recipes: {len(mealime)}; fully converted: {len(complete)}")
    for f in fields:
        ratios = [res["per_serving"].get(f, 0) / r["nutrition"][f] for r, res in complete
                  if r["nutrition"].get(f) and r["nutrition"][f] > 0.5]
        if ratios:
            q = statistics.quantiles(ratios, n=10)
            within = sum(0.85 <= x <= 1.15 for x in ratios) / len(ratios)
            high = sum(x > 1.15 for x in ratios) / len(ratios)
            off50 = sum(x > 1.5 or x < 1 / 1.5 for x in ratios) / len(ratios)
            print(f"  {f:<10} ours/Mealime median {statistics.median(ratios):.2f}  "
                  f"p10 {q[0]:.2f}  p90 {q[-1]:.2f}  within ±15%: {within:.0%}  "
                  f"(high {high:.0%}, low {1 - within - high:.0%}; >50% off {off50:.1%})")
    print("\nmost common lines not converted:")
    for (status, how, name), n in sorted(problems.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"  {n:>6}  {status:<22} {how or '':<28} {name}")
    if args.by_ingredient:
        by_ingredient_report(complete, db, args.field, args.by_ingredient)
    return complete


def by_ingredient_report(complete: list, db: dict, field: str = "energy", top: int = 15) -> None:
    """Which ingredients pull recipes away from Mealime: for each recipe, log(ours/Mealime) for the
    field, credited to each ingredient by its share of our total. An ingredient whose recipes all
    lean the same way (a bad per-item weight, a can counted undrained, a different food) rises to
    the top; scattered noise cancels out. Ingredients in fewer than 15 recipes are left out."""
    acc = {}
    for r, res in complete:
        theirs, ours = r["nutrition"].get(field), res["per_serving"].get(field, 0)
        if not theirs or theirs < (20 if field == "sodium" else 0.5) or ours <= 0:
            continue
        err = math.log(ours / theirs)
        total = ours * (r.get("serving_count") or r.get("servings") or 1)
        for l in res["lines"]:
            if not l.get("grams"):
                continue
            share = foods_of(db)[l["ref"]]["per_100g"].get(field, 0) * l["grams"] / 100 / total
            if share <= 0:
                continue
            a = acc.setdefault(l["ingredient_id"], [0.0, 0.0, 0, l["name"], l["ref"], l.get("how") or ""])
            a[0] += share * err
            a[1] += share
            a[2] += 1
    rows = [a for a in acc.values() if a[2] >= 15]
    for title, key in (("pulling HIGH (ours > Mealime)", lambda a: -a[0]), ("pulling LOW (ours < Mealime)", lambda a: a[0])):
        print(f"\n{field}: ingredients {title}, by total impact")
        for impact, share, n, name, ref, how in sorted(rows, key=key)[:top]:
            print(f"  {name[:32]:<32} in {n:>4} recipes, avg share {share / n:4.0%}, those recipes {math.exp(impact / share):.2f}x"
                  f" | {foods_of(db)[ref]['name'][:34]:<34} | {how.split(' @')[0][:30]}")


def cmd_build_batches(db: dict, library_dir: Path) -> None:
    """Batch foods for the recipes that component_links point to: the linked recipe's whole-batch
    totals as a food ("batch:<id>") with its batch weight, so a blank line can count a share of
    it. Two passes, so a linked recipe that itself links another one picks up that batch."""
    ids = sorted({link["recipe"] for link in db.get("component_links", {}).values() if "recipe" in link})
    paths = {}
    for rid in ids:
        hits = list(library_dir.glob(f"families/*/{rid}_*.json"))
        if hits:
            paths[rid] = hits[0]
    db.setdefault("batches", {})
    for _ in range(2):
        for rid, path in paths.items():
            recipe = json.loads(path.read_text(encoding="utf-8"))
            res = recipe_nutrition(recipe, db)
            grams = sum(l.get("grams") or 0 for l in res["lines"])
            servings = recipe.get("serving_count") or 1
            if grams <= 0:
                continue
            per_100g = {f: round(v * servings / grams * 100, 4) for f, v in res["per_serving"].items()}
            db["batches"][f"batch:{rid}"] = {"name": recipe.get("name"), "source": "recipe", "tier": nc.TIERS["recipe"],
                                            "per_100g": per_100g, "quality": {f: "calculated" for f in per_100g},
                                            "portions": [], "batch_grams": round(grams, 1), "complete": res["complete"]}
    missing = [rid for rid in ids if rid not in paths]
    print(f"built {len(paths)} batch foods" + (f"; recipes not found: {missing}" if missing else ""), file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recipes-dir", default=str(DEFAULT_LIBRARY_DIR))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("line"); p.add_argument("quantity"); p.add_argument("ingredient_id"); p.add_argument("name", nargs="?", default="")
    sub.add_parser("build-batches")
    p = sub.add_parser("validate"); p.add_argument("--snapshot", required=True); p.add_argument("--top", type=int, default=40)
    p.add_argument("--by-ingredient", type=int, default=0, metavar="N",
                   help="also list the N ingredients that pull recipes furthest high and low")
    p.add_argument("--field", default="energy", help="field for --by-ingredient (energy, fat, carbs, protein, ...)")
    args = parser.parse_args()
    db = nc.load_db(Path(args.recipes_dir))
    if args.cmd == "line":
        parsed = parse_quantity(args.quantity)
        food = db["foods"][db["ingredients"][args.ingredient_id]["ref"]]
        print(parsed, "->", line_grams(parsed, food, args.name, db.get("measures", {}).get(args.ingredient_id)),
              "|", food["name"])
    elif args.cmd == "build-batches":
        cmd_build_batches(db, Path(args.recipes_dir))
        nc.save_db(db, Path(args.recipes_dir))
    elif args.cmd == "validate":
        cmd_validate(args, db)


if __name__ == "__main__":
    main()
