"""
USDA FoodData Central (FDC) client: search, cached food fetches, and conversion of an FDC food
into the library's nutrition shape (the same 66 field names Mealime recipes carry).

WHY: non-Mealime recipes (Albertsons native, gap-fill, Serious Eats) have no or only basic
nutrition. Each master ingredient gets an FDC match; recipe nutrition is then summed from
per-100 g values and gram weights. Mealime's own numbers are never recalculated -- they are
the answer key for checking this method.

KEY: the api.data.gov key sits in fdc_api_key.txt next to the scripts (git-ignored), or in the
FDC_API_KEY environment variable. Limit: 3,600 requests/hour.

DATA TYPES: SR Legacy first -- it carries ~120 nutrients and household portion weights
("1 medium onion = 110 g"). Foundation has far fewer nutrients (32 for yellow onion), so it is
only a fallback. Branded (label nutrients only, ~15) never meets the standard.

CACHE: every fetched food is stored as raw JSON in fdc_cache/<fdcId>.json (git-ignored), so a
re-run costs no requests. SR Legacy is frozen, so cached entries never go stale.
"""

import json
import os
import re
import time
from pathlib import Path

import requests

API_BASE = "https://api.nal.usda.gov/fdc/v1"
KEY_FILENAME = "fdc_api_key.txt"
CACHE_DIR = Path(__file__).with_name("fdc_cache")
GENERIC_TYPES = ("SR Legacy", "Foundation")

# Library nutrition field -> FDC nutrient number(s). A tuple sums its parts; the first number of
# a list that the food actually reports wins (alternatives for the same quantity).
# Units follow FDC: g for macros/amino acids, mg for most minerals/vitamins, ug for A, D, K,
# B12, folate, selenium. Energy is kcal.
NUTRIENTS = {
    "energy": ["208"],
    "alcohol": ["221"],
    "ash": ["207"],
    "caffeine": ["262"],
    "water": ["255"],
    "carbs": ["205"],
    "fiber": ["291"],
    "starch": ["209"],
    "sugars": ["269", "269.3"],
    "fructose": ["212"],
    "galactose": ["287"],
    "glucose": ["211"],
    "lactose": ["213"],
    "maltose": ["214"],
    "sucrose": ["210"],
    "sugar_alcohol": ["299"],                 # not in SR Legacy; stays absent (= 0)
    "fat": ["204"],
    "monounsaturated": ["645"],
    "polyunsaturated": ["646"],
    # n-3: ALA (851 when split, else undifferentiated 18:3 = 619) + EPA + DPA + DHA
    "omega_3": [("851", "629", "631", "621"), ("619", "629", "631", "621")],
    # n-6: linoleic (675 when split, else 18:2 = 618) + arachidonic (20:4 = 620)
    "omega_6": [("675", "620"), ("618", "620")],
    "saturated": ["606"],
    "transfats": ["605"],
    "cholesterol": ["601"],
    "protein": ["203"],
    "alanine": ["513"],
    "arginine": ["511"],
    "aspartic_acid": ["514"],
    "cystine": ["507"],
    "glutamic_acid": ["515"],
    "glycine": ["516"],
    "histidine": ["512"],
    "isoleucine": ["503"],
    "leucine": ["504"],
    "lysine": ["505"],
    "methionine": ["506"],
    "phenylalanine": ["508"],
    "proline": ["517"],
    "serine": ["518"],
    "threonine": ["502"],
    "tryptophan": ["501"],
    "tyrosine": ["509"],
    "valine": ["510"],
    "b1_thiamine": ["404"],
    "b2_riboflavin": ["405"],
    "b3_niacin": ["406"],
    "b5_pantothenic_acid": ["410"],
    "b6_pyridoxine": ["415"],
    "b12_cobalamin": ["418"],
    "choline": ["421"],
    "folate": ["435", "417"],                 # DFE first; to be confirmed against Mealime
    "vitamin_a": ["320"],                     # RAE ug; to be confirmed against Mealime
    "vitamin_c": ["401"],
    "vitamin_d": ["328"],                     # ug; to be confirmed against Mealime
    "vitamin_e": ["323"],
    "vitamin_k": ["430"],
    "calcium": ["301"],
    "copper": ["312"],
    "iron": ["303"],
    "magnesium": ["304"],
    "manganese": ["315"],
    "phosphorus": ["305"],
    "potassium": ["306"],
    "selenium": ["317"],
    "sodium": ["307"],
    "zinc": ["309"],
}


def api_key() -> str:
    key = os.environ.get("FDC_API_KEY")
    if not key:
        path = Path(__file__).with_name(KEY_FILENAME)
        key = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if not key:
        raise SystemExit(f"No FDC API key: put it in tools/{KEY_FILENAME} or set FDC_API_KEY.")
    return key


def _request(method: str, path: str, **kwargs):
    """One API call with the key attached; retries 429/5xx with backoff."""
    params = dict(kwargs.pop("params", {}), api_key=api_key())
    for attempt in range(5):
        resp = requests.request(method, f"{API_BASE}{path}", params=params, timeout=60, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def search(query: str, data_types=GENERIC_TYPES, page_size: int = 10, pages: int = 1) -> list[dict]:
    """Search hits as [{fdcId, dataType, description}], in FDC's ranking order. Branded queries
    match tens of thousands of products and the exact-name ones are scattered through the
    ranking, so pass pages > 1 (page_size max 200) to see more than the top slice."""
    hits = []
    for page in range(1, pages + 1):
        data = _request("GET", "/foods/search",
                        params={"query": query, "dataType": list(data_types),
                                "pageSize": page_size, "pageNumber": page})
        foods = data.get("foods", [])
        hits += [{"fdcId": f["fdcId"], "dataType": f["dataType"], "description": f["description"]}
                 for f in foods]
        if len(foods) < page_size:
            break
    return hits


def fetch_foods(fdc_ids) -> dict[int, dict]:
    """Full FDC records by id, from the cache where possible (20 ids per request otherwise)."""
    CACHE_DIR.mkdir(exist_ok=True)
    out, missing = {}, []
    for fid in dict.fromkeys(int(i) for i in fdc_ids):
        cached = CACHE_DIR / f"{fid}.json"
        if cached.exists():
            out[fid] = json.loads(cached.read_text(encoding="utf-8"))
        else:
            missing.append(fid)
    for start in range(0, len(missing), 20):
        for food in _request("POST", "/foods", json={"fdcIds": missing[start:start + 20]}):
            fid = int(food["fdcId"])
            (CACHE_DIR / f"{fid}.json").write_text(json.dumps(food), encoding="utf-8")
            out[fid] = food
    return out


def _amounts(food: dict) -> dict[str, float]:
    """FDC nutrient number -> amount per 100 g."""
    amounts = {}
    for n in food.get("foodNutrients", []):
        number, amount = (n.get("nutrient") or {}).get("number"), n.get("amount")
        if number and amount is not None:
            amounts[str(number)] = float(amount)
    return amounts


def nutrition_per_100g(food: dict) -> dict[str, float]:
    """The food's nutrients under the library's field names. Fields the food doesn't report are
    left out (not zeroed), so a recipe total can say which fields are incomplete."""
    amounts = _amounts(food)
    out = {}
    for field, options in NUTRIENTS.items():
        for option in options:
            parts = option if isinstance(option, tuple) else (option,)
            # a sum needs its first part (the main component); the minor ones may be missing
            if parts[0] in amounts:
                out[field] = round(sum(amounts.get(p, 0.0) for p in parts), 4)
                break
    return out


def portions(food: dict) -> list[dict]:
    """Household measures as [{"amount", "unit", "grams"}], e.g. 1 "medium (2-1/2" dia)" = 110 g.
    SR Legacy puts the measure in `modifier`; Foundation uses measureUnit / portionDescription."""
    out = []
    for p in food.get("foodPortions", []):
        unit_name = (p.get("measureUnit") or {}).get("name")
        unit = p.get("modifier") if unit_name in (None, "undetermined") else unit_name
        if p.get("portionDescription") and p["portionDescription"] != "Quantity not specified":
            unit = unit or p["portionDescription"]
        if unit and p.get("gramWeight"):
            out.append({"amount": p.get("amount") or 1.0, "unit": unit.strip(),
                        "grams": float(p["gramWeight"])})
    return out


# Detail fields that belong to a label macro. When a Branded label replaces the macro, the
# stand-in's detail fields are scaled by the same ratio, so the parts still add up to the whole
# (mascarpone's fatty acids follow its real fat, not cream cheese's).
SCALED_WITH = {
    "protein": ("alanine", "arginine", "aspartic_acid", "cystine", "glutamic_acid", "glycine",
                "histidine", "isoleucine", "leucine", "lysine", "methionine", "phenylalanine",
                "proline", "serine", "threonine", "tryptophan", "tyrosine", "valine"),
    "fat": ("monounsaturated", "polyunsaturated", "omega_3", "omega_6"),
    "sugars": ("fructose", "galactose", "glucose", "lactose", "maltose", "sucrose"),
    "carbs": ("starch",),
}


LABEL_CORE = ("energy", "fat", "carbs", "protein", "sodium")
MIN_SERVING = 10.0      # g or ml; labels round each serving to whole grams, so per-100 g error is
                        # up to 50/serving g -- a 5 g mustard serving can be off by 10 g per 100 g


def label_problems(label_food: dict) -> list[str]:
    """Why a Branded product's label is unfit to override the stand-in ([] = usable). Labels
    round tiny per-serving amounts (a 1-tsp serving of chili sauce reads 0 kcal), so a product
    with a very small serving, calories that don't match its own macros, or nothing but zeros
    is rejected."""
    label = nutrition_per_100g(label_food)
    problems = [f"no {f}" for f in LABEL_CORE if f not in label]
    serving = label_food.get("servingSize")
    if serving is not None and serving < MIN_SERVING:
        problems.append(f"serving only {serving:g} {label_food.get('servingSizeUnit', '')}, too coarse")
    impossible = [f for f in ("fat", "carbs", "protein", "sugars") if label.get(f, 0) > 100]
    if impossible or label.get("energy", 0) > 900:
        problems.append("impossible per-100 g values (" + ", ".join(impossible or ["energy"]) + ")")
    if not problems and all(label[f] == 0 for f in ("energy", "fat", "carbs", "protein")):
        problems.append("all macros 0 (rounded label)")
    elif not problems:
        atwater = 4 * label["protein"] + 4 * label["carbs"] + 9 * label["fat"]
        gap = label["energy"] - atwater
        # labels don't list alcohol or acetic acid, so an alcoholic product's or a vinegar's
        # extra calories are its ethanol / acetic acid, not an error
        explained = gap > 0 and (is_alcoholic(label_food) or is_vinegar(label_food))
        if (label["energy"] == 0 and not is_vinegar(label_food)) or (
                abs(gap) > max(25.0, 0.25 * label["energy"]) and not explained):
            problems.append(f"kcal {label['energy']:g} doesn't fit its macros (~{atwater:.0f})")
    return problems


# matched against description + ingredients; an alcoholic product lists its alcohol or spirit
# (a "triple sec syrup" mixer or "bitters & soda" does not, so it isn't counted)
ALCOHOL_WORDS = ("alcohol", "wine", "liqueur", "rum", "vodka", "whiskey", "whisky", "brandy",
                 "proof", "spirits?", "vermouth")


def is_vinegar(food: dict) -> bool:
    return "vinegar" in (food.get("description") or "").lower()


def is_alcoholic(food: dict) -> bool:
    if is_vinegar(food):                # wine vinegar has no ethanol left
        return False
    text = f"{food.get('description', '')} {food.get('ingredients', '')}".lower()
    return any(re.search(rf"\b{w}\b", text) for w in ALCOHOL_WORDS)


def label_alcohol(label_food: dict) -> float | None:
    """Ethanol g/100 g implied by an alcoholic product's label: calories its macros don't
    explain, at 7 kcal/g (labels don't list alcohol themselves). None if not alcoholic."""
    if not is_alcoholic(label_food):
        return None
    n = nutrition_per_100g(label_food)
    if "energy" not in n:
        return None
    gap = n["energy"] - (4 * n.get("protein", 0) + 4 * n.get("carbs", 0) + 9 * n.get("fat", 0))
    return round(max(0.0, gap) / 7, 2)


def blend_with_label(base: dict[str, float], label: dict[str, float]) -> dict[str, float]:
    """HYBRID nutrition for an ingredient SR Legacy lacks: the Branded product's label values
    (about 13 fields: energy, fat, saturated, trans, cholesterol, sodium, carbs, fiber, sugars,
    protein, calcium, iron, potassium) win; every other field comes from the SR stand-in, with
    detail fields scaled to the label's macros (SCALED_WITH). A detail field whose stand-in
    macro is 0 can't be scaled and is kept as is."""
    out = dict(base)
    for field, value in label.items():
        out[field] = value
    for macro, details in SCALED_WITH.items():
        if macro in label and base.get(macro):
            ratio = label[macro] / base[macro]
            for detail in details:
                if detail in base and detail not in label:
                    out[detail] = round(base[detail] * ratio, 4)
    return out


def summarize(food: dict, label_food: dict | None = None) -> dict:
    """The record kept in the library's fdc_foods.json for one FDC food -- or, with label_food
    (a Branded product), the hybrid of the two (see blend_with_label)."""
    per_100g = nutrition_per_100g(food)
    record = {
        "description": food.get("description"),
        "data_type": food.get("dataType"),
        "per_100g": per_100g,
        "portions": portions(food),
    }
    if label_food:
        record["per_100g"] = blend_with_label(per_100g, nutrition_per_100g(label_food))
        alcohol = label_alcohol(label_food)
        if alcohol is not None:
            record["per_100g"]["alcohol"] = alcohol
        record["label"] = {"fdc_id": label_food.get("fdcId"),
                           "brand": label_food.get("brandName") or label_food.get("brandOwner"),
                           "description": label_food.get("description")}
    return record
