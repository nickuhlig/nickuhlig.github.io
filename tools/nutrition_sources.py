"""
The four food-composition sources behind the composite nutrition database, each read into one
shape: the library's 66 nutrition fields (Mealime's names and units) per 100 g, a quality tag on
every value, and household portions where the source has them.

PRIORITY (agreed with the user; nutrition_composite.py applies it):
  1. sr     USDA SR Legacy, via fdc_client (API + fdc_cache/).        Public domain.
  2. cnf    Canadian Nutrient File 2015 (Health Canada CSVs).          Open Government Licence - Canada.
     frida  Frida 5.5, Danish Food Composition Database (DTU, xlsx).   CC BY 4.0.
  3. on     OpenNutrition 2025.1 (TSV), generic foods only.           ODbL; every value tagged "estimated".
  4. branded / fndds  USDA Branded product labels and FNDDS (USDA survey foods), via fdc_client.
  5. recipe-built from better-tier foods (simple syrup = sugar + water), then a last-resort fallback.

RECORD SHAPE (one per source food):
    {"key": "cnf:501502", "source": "cnf", "id": "501502", "name": "...", "alt_names": [...],
     "per_100g": {"energy": 250.0, ...},        fields the source doesn't report are left out
     "quality":  {"energy": "measured", ...},   see QUALITY below
     "portions": [{"amount": 1.0, "unit": "tbsp", "grams": 15.0}, ...]}

QUALITY tags, best to worst: measured (lab analysis of this food), calculated (from other analysed
values or a recipe), borrowed (copied from another country's table), imputed (from a similar
food), label (manufacturer label), assumed_zero, estimated (OpenNutrition: unsourced or
zero-filled, so nothing is known about any single value), unknown (source gives no code).

IMPORT: CNF, Frida and OpenNutrition are parsed once from the downloaded files into compact
gzipped JSON in nutrition_data/ (git-ignored), so later runs (and the Raspberry Pi) never re-read
the 280 MB TSV or the xlsx:

    python nutrition_sources.py import --cnf <dir with CNF CSVs> --frida <Frida_5.5_Dataset.xlsx>
                                       --opennutrition <opennutrition_foods.tsv>

Reading the xlsx needs openpyxl (import step only).
"""

import argparse
import csv
import gzip
import json
import re
import sys
from pathlib import Path

import fdc_client

DATA_DIR = Path(__file__).with_name("nutrition_data")
FIELDS = list(fdc_client.NUTRIENTS)          # the library's 66 field names, in Mealime's order
QUALITY_ORDER = ["measured", "calculated", "borrowed", "imputed", "label", "assumed_zero", "estimated", "unknown"]

# --- SR Legacy -----------------------------------------------------------------------------------

SR_DERIVATION = {
    "A": "measured", "AR": "measured", "MA": "measured", "DA": "measured", "PAK": "measured",
    "AS": "measured", "AI": "measured",
    "NC": "calculated", "NR": "calculated", "RA": "calculated", "RC": "calculated", "RF": "calculated",
    "RK": "calculated", "RP": "calculated", "RPA": "calculated", "RPI": "calculated", "JA": "calculated",
    "JO": "calculated", "PIK": "calculated", "BD": "calculated",
    "T": "borrowed",
    "LC": "label", "MC": "label", "MI": "label",
    "Z": "assumed_zero",
}


def _sr_quality(code):
    if not code:
        return "unknown"
    if code in SR_DERIVATION:
        return SR_DERIVATION[code]
    if code.startswith(("BF", "BN", "FL", "O")):
        return "imputed"
    return "unknown"


def sr_records(fdc_ids) -> dict:
    """SR Legacy foods (fetched once, then cached by fdc_client) as normalized records."""
    return fdc_records(fdc_ids, "sr")


def fdc_records(fdc_ids, source: str) -> dict:
    """FoodData Central foods as normalized records. source: "sr" (SR Legacy; per-value derivation
    codes), "branded" (a product's label, ~15 nutrients, all tagged "label") or "fndds" (USDA's
    survey database: recipe-calculated values, tagged "calculated")."""
    out = {}
    for fid, food in fdc_client.fetch_foods(fdc_ids).items():
        codes = {}
        for n in food.get("foodNutrients", []):
            number = (n.get("nutrient") or {}).get("number")
            if number:
                codes[str(number)] = (n.get("foodNutrientDerivation") or {}).get("code")
        per_100g, quality = {}, {}
        amounts = fdc_client._amounts(food)
        for field, options in fdc_client.NUTRIENTS.items():
            for option in options:
                parts = option if isinstance(option, tuple) else (option,)
                if parts[0] in amounts:
                    per_100g[field] = round(sum(amounts.get(p, 0.0) for p in parts), 4)
                    quality[field] = {"sr": _sr_quality(codes.get(parts[0])), "branded": "label",
                                      "fndds": "calculated"}[source]
                    break
        out[f"{source}:{fid}"] = {"key": f"{source}:{fid}", "source": source, "id": str(fid),
                            "name": food.get("description"), "alt_names": [],
                            "per_100g": per_100g, "quality": quality,
                            "portions": fdc_client.portions(food)}
    return out


# --- Canadian Nutrient File ----------------------------------------------------------------------

# Same idea as fdc_client.NUTRIENTS (CNF mostly reuses USDA numbers): a list gives alternatives,
# a tuple sums parts. Where CNF differs: starch 810, vitamin A RAE 814, folate DFE 815,
# vitamin D ug 339, choline 862, n-3/n-6 totals 868/869, ALA 831, LA 825, AA 855.
CNF_NUTRIENTS = dict(fdc_client.NUTRIENTS, **{
    "starch": ["810", "209"],
    "sugar_alcohol": [("261", "260")],
    "omega_3": ["868", ("831", "629", "631", "621"), ("619", "629", "631", "621")],
    "omega_6": ["869", ("825", "855"), ("618", "620")],
    "choline": ["862", "421"],
    "folate": ["815", "435", "417"],
    "vitamin_a": ["814", "320"],
    "vitamin_d": ["339", "328"],
})
CNF_SOURCE_QUALITY = {
    "0": "borrowed", "1": "borrowed", "82": "borrowed", "83": "borrowed", "8": "borrowed", "10": "borrowed",
    "2": "calculated", "4": "calculated", "102": "calculated", "106": "calculated", "107": "calculated",
    "3": "measured", "7": "measured",
    "5": "imputed", "15": "imputed", "108": "imputed", "14": "imputed",
    "6": "label", "9": "label",
    "12": "assumed_zero",
}


def import_cnf(csv_dir: Path) -> dict:
    def rows(name):
        with open(csv_dir / f"{name}.csv", encoding="cp1252", newline="") as f:
            yield from csv.DictReader(f)

    values = {}
    for r in rows("NUTRIENT AMOUNT"):
        values.setdefault(r["FoodID"], {})[r["NutrientID"]] = (float(r["NutrientValue"]), r["NutrientSourceID"])
    measures = {r["MeasureID"]: r["MeasureDescription"].strip() for r in rows("MEASURE NAME")}
    portions = {}
    for r in rows("CONVERSION FACTOR"):
        # the factor converts per-100 g values to per-measure values, i.e. measure grams / 100
        desc = measures.get(r["MeasureID"])
        if desc:
            portions.setdefault(r["FoodID"], []).append(
                {"amount": 1.0, "unit": desc, "grams": round(float(r["ConversionFactorValue"]) * 100, 2)})
    out = {}
    for food in rows("FOOD NAME"):
        fid = food["FoodID"]
        vals = values.get(fid, {})
        per_100g, quality = {}, {}
        for field, options in CNF_NUTRIENTS.items():
            for option in options:
                parts = option if isinstance(option, tuple) else (option,)
                if parts[0] in vals:
                    per_100g[field] = round(sum(vals[p][0] for p in parts if p in vals), 4)
                    quality[field] = CNF_SOURCE_QUALITY.get(vals[parts[0]][1], "unknown")
                    break
        out[f"cnf:{fid}"] = {"key": f"cnf:{fid}", "source": "cnf", "id": fid,
                             "name": food["FoodDescription"], "alt_names": [],
                             "per_100g": per_100g, "quality": quality, "portions": portions.get(fid, [])}
    return out


# --- Frida ---------------------------------------------------------------------------------------

# Library field -> Frida English parameter name(s); the first one the food has wins.
# Amino acids are mg/100 g in Frida (g in the library); see FRIDA_SCALE.
FRIDA_PARAMS = {
    "energy": ["Energy (kcal)"], "alcohol": ["Alcohol"], "ash": ["Ash"], "caffeine": ["Caffeine"],
    "water": ["Water"], "carbs": ["Carbohydrate by difference"], "fiber": ["Dietary fibre"],
    "starch": ["Starch/Glycogen"], "sugars": ["Sum sugars"], "fructose": ["Fructose"],
    "galactose": ["Galactose"], "glucose": ["Glucose"], "lactose": ["Lactose"], "maltose": ["Maltose"],
    "sucrose": ["Sucrose"], "sugar_alcohol": ["Sum sugar alcohols"], "fat": ["Fat"],
    "monounsaturated": ["Sum monounsaturated fatty acids"],
    "polyunsaturated": ["Sum polyunsaturated fatty acids"],
    "omega_3": ["Sum n-3 fatty acids"], "omega_6": ["Sum n-6 fatty acids"],
    "saturated": ["Sum saturated fatty acids"], "transfats": ["Sum trans fatty acids"],
    "cholesterol": ["Cholesterol"], "protein": ["Protein"],
    "alanine": ["Alanine"], "arginine": ["Arginine"], "aspartic_acid": ["Aspartic acid"],
    "cystine": ["Cystine"], "glutamic_acid": ["Glutamic acid"], "glycine": ["Glycine"],
    "histidine": ["Histidine"], "isoleucine": ["Isoleucine"], "leucine": ["Leucine"],
    "lysine": ["Lysine"], "methionine": ["Methionine"], "phenylalanine": ["Phenylalanine"],
    "proline": ["Proline"], "serine": ["Serine"], "threonine": ["Threonine"],
    "tryptophan": ["Tryptophan"], "tyrosine": ["Tyrosine"], "valine": ["Valine"],
    "b1_thiamine": ["Thiamin (Vitamin B1)"], "b2_riboflavin": ["Riboflavin (Vitamin B2)"],
    "b3_niacin": ["Niacin"], "b5_pantothenic_acid": ["Pantothenic acid"], "b6_pyridoxine": ["Vitamin B6"],
    "b12_cobalamin": ["Vitamin B12"], "choline": ["Choline"], "folate": ["Folate"],
    "vitamin_a": [],                    # computed as RAE, see _frida_vitamin_a
    "vitamin_c": ["Vitamin C"], "vitamin_d": ["Vitamin D"],
    "vitamin_e": ["alpha-Tocopherol", "Vitamin E"], "vitamin_k": ["Vitamin K", "Vitamin K1"],
    "calcium": ["Calcium"], "copper": ["Copper"], "iron": ["Iron"], "magnesium": ["Magnesium"],
    "manganese": ["Manganese"], "phosphorus": ["Phosphorus"], "potassium": ["Potassium"],
    "selenium": ["Selenium"], "sodium": ["Sodium"], "zinc": ["Zinc"],
}
FRIDA_SCALE = {f: 0.001 for f in ("alanine", "arginine", "aspartic_acid", "cystine", "glutamic_acid",
                                  "glycine", "histidine", "isoleucine", "leucine", "lysine", "methionine",
                                  "phenylalanine", "proline", "serine", "threonine", "tryptophan",
                                  "tyrosine", "valine")}
# Frida source ids with fixed meanings; other ids are publications, typed by EurofirRefType.
FRIDA_SOURCE_QUALITY = {"1001": "imputed", "1002": "imputed", "1003": "calculated", "1004": "calculated",
                        "1655": "assumed_zero"}
FRIDA_REFTYPE_QUALITY = {"R": "measured", "AJ": "measured", "P": "measured", "X": "measured",
                         "PA": "measured", "AB": "measured", "B": "borrowed", "F": "borrowed",
                         "WW": "borrowed", "L": "label", "E": "imputed"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _frida_vitamin_a(vals):
    """Frida gives vitamin A as RE (retinol + beta-carotene / 6); the library uses RAE
    (retinol + beta-carotene / 12). Recompute when both parts are there, else keep RE."""
    retinol, carotene = vals.get("Retinol"), vals.get("beta-Carotene")
    if retinol is not None or carotene is not None:
        value = (retinol[0] if retinol else 0.0) + (carotene[0] / 12 if carotene else 0.0)
        return value, (retinol or carotene)[1]
    return vals.get("Vitamin A")


def import_frida(xlsx: Path) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ref_type = {str(r[0]): r[1] for r in list(wb["Source"].iter_rows(values_only=True))[1:]}

    def quality_of(source_id):
        sid = str(source_id)
        if sid in FRIDA_SOURCE_QUALITY:
            return FRIDA_SOURCE_QUALITY[sid]
        return FRIDA_REFTYPE_QUALITY.get(ref_type.get(sid), "unknown")

    rows = wb["Data_Normalised"].iter_rows(values_only=True)
    header = next(rows)
    col = {name: i for i, name in enumerate(header)}
    foods = {}
    for r in rows:
        value = _num(r[col["ResVal"]])
        if value is None:
            continue
        fid = str(r[col["FoodID"]])
        food = foods.setdefault(fid, {"name": r[col["FoodName"]], "name_da": r[col["FødevareNavn"]], "vals": {}})
        food["vals"][r[col["ParameterName"]]] = (value, quality_of(r[col["Source"]]))
    out = {}
    for fid, food in foods.items():
        vals = food["vals"]
        per_100g, quality = {}, {}
        for field, names in FRIDA_PARAMS.items():
            hit = _frida_vitamin_a(vals) if field == "vitamin_a" else next(
                (vals[n] for n in names if n in vals), None)
            if hit:
                per_100g[field] = round(hit[0] * FRIDA_SCALE.get(field, 1.0), 4)
                quality[field] = hit[1]
        out[f"frida:{fid}"] = {"key": f"frida:{fid}", "source": "frida", "id": fid, "name": food["name"],
                               "alt_names": [food["name_da"]], "per_100g": per_100g, "quality": quality,
                               "portions": []}
    return out


# --- OpenNutrition -------------------------------------------------------------------------------

# Library field -> OpenNutrition key (same units). It has no ash, starch or individual sugars.
ON_KEYS = {
    "energy": "calories", "alcohol": "ethyl_alcohol", "caffeine": "caffeine", "water": "water",
    "carbs": "carbohydrates", "fiber": "dietary_fiber", "sugars": "total_sugars",
    "sugar_alcohol": "sugar_alcohols", "fat": "total_fat", "monounsaturated": "monounsaturated_fats",
    "polyunsaturated": "polyunsaturated_fats", "omega_3": "omega_3", "omega_6": "omega_6",
    "saturated": "saturated_fats", "transfats": "trans_fats", "cholesterol": "cholesterol",
    "protein": "protein", "b1_thiamine": "thiamin", "b2_riboflavin": "riboflavin", "b3_niacin": "niacin",
    "b5_pantothenic_acid": "pantothenic_acid", "b6_pyridoxine": "vitamin_b6",
    "b12_cobalamin": "vitamin_b12", "choline": "choline", "folate": "folate_dfe",
    **{f: f for f in ("alanine", "arginine", "aspartic_acid", "cystine", "glutamic_acid", "glycine",
                      "histidine", "isoleucine", "leucine", "lysine", "methionine", "phenylalanine",
                      "proline", "serine", "threonine", "tryptophan", "tyrosine", "valine",
                      "vitamin_a", "vitamin_c", "vitamin_d", "vitamin_e", "vitamin_k", "calcium",
                      "copper", "iron", "magnesium", "manganese", "phosphorus", "potassium",
                      "selenium", "sodium", "zinc")},
}
ON_TYPES = ("everyday", "prepared")     # generic foods; "grocery"/"restaurant" are branded items


def import_opennutrition(tsv: Path, extra_ids=()) -> dict:
    """Generic foods, plus any grocery (branded) products named in extra_ids -- importing all
    313k grocery rows would bloat nutrition_data/, and only reviewed picks are needed."""
    csv.field_size_limit(10 ** 9)
    extra_ids = set(extra_ids)
    out = {}
    with open(tsv, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if row["type"] not in ON_TYPES and row["id"] not in extra_ids:
                continue
            n = json.loads(row["nutrition_100g"] or "{}")
            per_100g = {field: float(n[k]) for field, k in ON_KEYS.items() if n.get(k) is not None}
            serving = (json.loads(row["serving"] or "{}") or {})
            common, metric = serving.get("common") or {}, serving.get("metric") or {}
            portions = []
            if common.get("unit") and metric.get("unit") == "g" and metric.get("quantity"):
                portions.append({"amount": float(common.get("quantity") or 1), "unit": common["unit"],
                                 "grams": float(metric["quantity"])})
            sources = [{"db": s.get("database"), "id": str(s.get("id")), "name": s.get("name")}
                       for s in json.loads(row["source"] or "[]")]
            key = f"on:{row['id']}"
            out[key] = {"key": key, "source": "on", "id": row["id"], "name": row["name"],
                        "alt_names": json.loads(row["alternate_names"] or "[]"),
                        "per_100g": per_100g, "quality": {k: "estimated" for k in per_100g},
                        "portions": portions, "cites": sources}
    return out


def grocery_base_name(name: str) -> str:
    """'Aleppo Pepper by Morton & Bassett Spices' -> 'aleppo pepper' (normalized, singularized)."""
    base = re.split(r"\s+by\s+", name or "", maxsplit=1)[0]
    base = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", base.lower().replace("-", " "))).strip()
    return " ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in base.split())


ZERO_CALORIE_WORDS = re.compile(r"\b(water|vinegar|tea|coffee|seltzer|club soda|extract|bitters|spray|"
                                r"salt|sweetener|stevia|sucralose|zero|diet)\b", re.I)
ALCOHOL_WORDS = re.compile(r"\b(wine|liqueur|rum|vodka|whiske?y|brandy|gin|tequila|vermouth|sake|mirin|"
                           r"beer|cider|spirit|aperitivo|amaro|bitters)\b", re.I)


def label_problem(name: str, kcal, protein, fat, carbs, sodium=None):
    """Why a grocery product's label numbers can't be trusted (None = plausible). Labels round
    tiny servings, so a 1-tsp spice reads 0 kcal; the same test fdc_client.label_problems applies
    to USDA Branded labels: all-zero macros, or calories that don't fit 4/4/9 kcal per gram."""
    if kcal is None or protein is None or fat is None or carbs is None:
        return "missing calories or macros"
    if max(protein, fat, carbs) > 100 or kcal > 900:
        return "impossible per-100 g values"
    if sodium is not None and sodium > 40000:        # more than pure salt (~38,800 mg)
        return f"impossible sodium ({sodium:g} mg)"
    if kcal == 0 and protein == 0 and fat == 0 and carbs == 0:
        return None if ZERO_CALORIE_WORDS.search(name or "") else "all zero (rounded label)"
    atwater = 4 * protein + 4 * carbs + 9 * fat
    gap = kcal - atwater
    if gap > 0 and (ALCOHOL_WORDS.search(name or "") or "vinegar" in (name or "").lower()):
        return None        # labels leave out alcohol and acetic acid calories
    if kcal == 0 or abs(gap) > max(25.0, 0.25 * kcal):
        return f"{kcal:g} kcal doesn't fit its macros (~{atwater:.0f})"
    return None


def import_on_grocery_index(tsv: Path) -> dict:
    """Names of OpenNutrition's ~313k grocery (branded) products, keyed by base name, so the
    matcher can find exact product matches without importing every product's nutrients. A chosen
    product is then imported in full with --on-extra-ids."""
    csv.field_size_limit(10 ** 9)
    index = {}
    with open(tsv, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if row["type"] != "grocery":
                continue
            n = json.loads(row["nutrition_100g"] or "{}")
            fields = sum(v is not None for v in n.values())
            index.setdefault(grocery_base_name(row["name"]), []).append(
                [row["id"], row["name"], fields, n.get("calories"), n.get("protein"), n.get("total_fat"),
                 n.get("carbohydrates"), n.get("sodium")])
    return index


# --- storage -------------------------------------------------------------------------------------

def save(source: str, records: dict) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{source}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    return path


def load(source: str) -> dict:
    """Imported records of one source (cnf, frida or on); SR Legacy goes through sr_records()."""
    path = DATA_DIR / f"{source}.json.gz"
    if not path.exists():
        raise SystemExit(f"{path} is missing -- run: python nutrition_sources.py import ...")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def search(records: dict, *patterns: str) -> list:
    """Records whose name or alternate names match every regex pattern (case-insensitive)."""
    out = []
    for rec in records.values():
        text = " | ".join([rec["name"] or ""] + [a or "" for a in rec["alt_names"]])
        if all(re.search(p, text, re.I) for p in patterns):
            out.append(rec)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="parse downloaded source files into nutrition_data/")
    p.add_argument("--cnf", help="folder with the CNF 2015 CSV files")
    p.add_argument("--frida", help="Frida_5.5_Dataset.xlsx")
    p.add_argument("--opennutrition", help="opennutrition_foods.tsv")
    p.add_argument("--on-grocery-index", help="opennutrition_foods.tsv: write the grocery product name index")
    p.add_argument("--on-extra-ids", default="",
                   help="comma-separated OpenNutrition grocery ids to import besides the generic foods")
    args = parser.parse_args()
    if args.on_grocery_index:
        index = import_on_grocery_index(Path(args.on_grocery_index))
        print(f"on_grocery_index: {len(index)} names -> {save('on_grocery_index', index)}", file=sys.stderr)
    # grocery products already chosen in nutrition_foods.json are always kept, so a re-import can't
    # silently drop one that was picked earlier
    from library_paths import DEFAULT_LIBRARY_DIR
    chosen = DEFAULT_LIBRARY_DIR / "nutrition_foods.json"
    extra = {i for i in args.on_extra_ids.split(",") if i}
    if chosen.exists():
        refs = [a["ref"] for a in json.loads(chosen.read_text(encoding="utf-8"))["ingredients"].values()]
        extra |= {r[3:] for r in refs if r.startswith("on:")}
    on_reader = lambda tsv: import_opennutrition(tsv, extra)
    for source, arg, reader in (("cnf", args.cnf, import_cnf), ("frida", args.frida, import_frida),
                                ("on", args.opennutrition, on_reader)):
        if arg:
            records = reader(Path(arg))
            print(f"{source}: {len(records)} foods -> {save(source, records)}", file=sys.stderr)


if __name__ == "__main__":
    main()
