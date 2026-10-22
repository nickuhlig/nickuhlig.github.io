"""
The food-composition sources behind the composite nutrition database, each read into one
shape: the library's 66 nutrition fields (Mealime's names and units) per 100 g, a quality tag on
every value, and household portions where the source has them.

PRIORITY (agreed with the user; nutrition_composite.py applies it):
  1. sr     USDA SR Legacy, via fdc_client (API + fdc_cache/).        Public domain.
  2. cnf    Canadian Nutrient File 2015 (Health Canada CSVs).          Open Government Licence - Canada.
     frida  Frida 5.5, Danish Food Composition Database (DTU, xlsx).   CC BY 4.0.
     cofid  CoFID 2021, UK McCance and Widdowson's (xlsx).             Open Government Licence v3.0.
     ciqual Ciqual 2025, French ANSES table (XML).                     Licence Ouverte / Etalab 2.0.
     mext   Standard Tables of Food Composition in Japan, 8th ed. 2023 (MEXT, xlsx, Japanese names).
     afcd   Australian Food Composition Database, Release 3 (FSANZ, xlsx).
     nevo   NEVO-online 2025 v9.0, Dutch Food Composition Database (RIVM, CSV).
     ifct   Indian Food Composition Tables 2017 (NIN-ICMR), via github.com/nodef/ifct2017 (CSV).
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

IMPORT: CNF, Frida, CoFID, Ciqual and OpenNutrition are parsed once from the downloaded files into compact
gzipped JSON in nutrition_data/ (git-ignored), so later runs (and the Raspberry Pi) never re-read
the 280 MB TSV or the xlsx:

    python nutrition_sources.py import --cnf <dir with CNF CSVs> --frida <Frida_5.5_Dataset.xlsx>
                                       --opennutrition <opennutrition_foods.tsv>
                                       --cofid <CoFID_2021.xlsx> [<CoFID_oldFoods.xlsx>] --ciqual <xml dir>

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


# --- CoFID (UK, McCance and Widdowson's Composition of Foods Integrated Dataset 2021) --------------

# UK starch and sugars are monosaccharide equivalents: starch x 1.10, disaccharides x 1.05 of their
# weight. The library's fields are weights, and its carbs are by difference (fibre included), so
# carbs = starch + sugars + oligosaccharides + fibre, each as a weight.
COFID_SIMPLE = {
    "energy": "KCALS", "water": "WATER", "protein": "PROT", "fat": "FAT", "alcohol": "ALCO",
    "fructose": "FRUCT", "galactose": "GALACT", "glucose": "GLUC",
    "saturated": "SATFOD", "monounsaturated": "MONOFOD", "polyunsaturated": "POLYFOD", "transfats": "FODTRANS",
    "cholesterol": "CHOL", "sodium": "NA", "potassium": "K", "calcium": "CA", "magnesium": "MG",
    "phosphorus": "P", "iron": "FE", "copper": "CU", "zinc": "ZN", "manganese": "MN", "selenium": "SE",
    "vitamin_d": "VITD", "vitamin_k": "VITK1", "b1_thiamine": "THIA", "b2_riboflavin": "RIBO",
    "b3_niacin": "NIAC", "b5_pantothenic_acid": "PANTO", "b6_pyridoxine": "VITB6", "b12_cobalamin": "VITB12",
    "folate": "FOLT", "vitamin_c": "VITC",
}
COFID_DISACCHARIDES = {"sucrose": "SUCR", "maltose": "MALT", "lactose": "LACT"}
COFID_SHEETS = ("1.3 Proximates", "1.4 Inorganics", "1.5 Vitamins", "1.6 Vitamin Fractions", "1.12 (PUFA per 100gFood)")


def import_cofid(*xlsx_paths: Path) -> dict:
    """CoFID 2021 (and its "old foods" file): values per 100 g food -- alcoholic drinks are per 100 ml
    in CoFID, converted with their specific gravity. "Tr" (trace) is 0; "N" (present, amount unknown)
    is left out. CoFID has no per-value codes: foods whose data references say "calculated" or
    "recipe" are tagged calculated, the rest measured."""
    import openpyxl
    out = {}
    for path in xlsx_paths:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = wb["1.2 Factors"].iter_rows(values_only=True)
        head = next(rows)
        col = {name.strip(): i for i, name in enumerate(head) if name}      # the old-foods file pads some names
        foods = {}
        for r in rows:
            if not r[0] or r[0] in ("Food Code",) or not r[1]:
                continue
            refs = " ".join(str(r[col[k]] or "") for k in ("Description", "Main data references"))
            foods[str(r[0])] = {"name": str(r[1]).strip(), "group": str(r[col["Group"]] or ""),
                                "sg": _num(r[col["Specific gravity"]]),
                                "quality": "calculated" if re.search(r"calculat|recipe", refs, re.I) else "measured",
                                "vals": {}}
        for sheet in COFID_SHEETS:
            if sheet not in wb.sheetnames:
                continue
            rows = wb[sheet].iter_rows(values_only=True)
            next(rows)
            codes = next(rows)                            # second header row: the INFOODS-style codes
            for r in rows:
                food = foods.get(str(r[0]))
                if not food:
                    continue
                for i, code in enumerate(codes):
                    if not code or i < 7 or r[i] is None:
                        continue
                    v = 0.0 if str(r[i]).strip() == "Tr" else _num(r[i])
                    if v is not None:
                        food["vals"][code] = v
        for code, food in foods.items():
            key = f"cofid:{code}"
            if key in out:                                # the 2021 dataset wins over the old-foods file
                continue
            v, q = food["vals"], food["quality"]
            scale = 1 / food["sg"] if food["group"].startswith("Q") and food["sg"] else 1.0   # drinks: per 100 ml
            per_100g = {f: v[c] for f, c in COFID_SIMPLE.items() if c in v}
            for f, c in COFID_DISACCHARIDES.items():
                if c in v:
                    per_100g[f] = v[c] / 1.05
            if "STAR" in v:
                per_100g["starch"] = v["STAR"] / 1.10
            # Total sugars as a weight: the six sugars when all are known, else CoFID's total (left in
            # monosaccharide equivalents, at most ~5% high); summing only the known ones would read a
            # food with "galactose 0, lactose 0, the rest N" as sugar-free.
            six = ("glucose", "galactose", "fructose", "sucrose", "maltose", "lactose")
            if all(k in per_100g for k in six):
                per_100g["sugars"] = sum(per_100g[k] for k in six)
            elif "TOTSUG" in v:
                per_100g["sugars"] = v["TOTSUG"]
            fibre = v.get("AOACFIB", v.get("ENGFIB"))
            if fibre is not None:
                per_100g["fiber"] = fibre
            # Carbs by difference (fibre included): from starch and sugars when both are known, else
            # from CoFID's total; when CoFID's total is unknown ("N") the field is left out.
            if "starch" in per_100g and "sugars" in per_100g:
                per_100g["carbs"] = per_100g["starch"] + per_100g["sugars"] + v.get("OLIGO", 0.0) + (fibre or 0.0)
            elif "CHO" in v:
                per_100g["carbs"] = v["CHO"] / 1.05 + (fibre or 0.0)
            if "RET" in v or "CAREQU" in v:               # RAE: retinol + beta-carotene equivalents / 12
                per_100g["vitamin_a"] = v.get("RET", 0.0) + v.get("CAREQU", 0.0) / 12
            if "ATOPH" in v or "VITE" in v:
                per_100g["vitamin_e"] = v.get("ATOPH", v.get("VITE"))
            n3 = [c for c in ("FOD18:3cn3", "FOD20:5cn3", "FOD22:5cn3", "FOD22:6cn3") if c in v]
            per_100g.update({"omega_3": sum(v[c] for c in n3)} if n3 else
                            ({"omega_3": v["TOTn3PFOD"]} if "TOTn3PFOD" in v else {}))
            n6 = [c for c in ("FOD18:2cn6", "FOD20:4cn6") if c in v]
            per_100g.update({"omega_6": sum(v[c] for c in n6)} if n6 else
                            ({"omega_6": v["TOTn6PFOD"]} if "TOTn6PFOD" in v else {}))
            per_100g = {f: round(x * scale, 4) for f, x in per_100g.items()}
            quality = {f: q for f in per_100g}
            if "fiber" in quality and "AOACFIB" not in v:
                quality["fiber"] = "imputed"              # NSP (Englyst) fibre reads lower than AOAC
            out[key] = {"key": key, "source": "cofid", "id": code, "name": food["name"],
                        "alt_names": [] if "old" not in path.name.lower() else ["CoFID old foods"],
                        "per_100g": per_100g, "quality": quality, "portions": []}
    return out


# --- Ciqual (France, ANSES 2025) -----------------------------------------------------------------

# Library field -> Ciqual constituent code(s); a tuple sums parts, a list gives alternatives.
# Ciqual carbohydrate is "available" (no fibre), so the library's by-difference carbs add the fibre.
CIQUAL_CONSTS = {
    "energy": ["328"], "water": ["400"], "ash": ["10000"], "alcohol": ["60000"], "protein": ["25000", "25003"],
    "carbs": [("31000", "34100")], "fiber": ["34100"], "starch": ["33110"], "sugars": ["32000"],
    "fructose": ["32210"], "galactose": ["32220"], "glucose": ["32250"], "lactose": ["32410"],
    "maltose": ["32430"], "sucrose": ["32480"], "sugar_alcohol": ["34000"], "fat": ["40000"],
    "saturated": ["40302"], "monounsaturated": ["40303"], "polyunsaturated": ["40304"],
    "omega_3": [("41833", "42053", "42263")], "omega_6": [("41826", "42046")], "cholesterol": ["75100"],
    "vitamin_d": ["52100"], "vitamin_e": ["71010", "53100"], "vitamin_k": [("54101", "54104")],
    "vitamin_c": ["55100"], "b1_thiamine": ["56100"], "b2_riboflavin": ["56200"], "b3_niacin": ["56310"],
    "b5_pantothenic_acid": ["56400"], "b6_pyridoxine": ["56500"], "b12_cobalamin": ["56600"],
    "folate": ["56702", "56700"], "sodium": ["10110"], "magnesium": ["10120"], "phosphorus": ["10150"],
    "potassium": ["10190"], "calcium": ["10200"], "manganese": ["10251"], "iron": ["10260"],
    "copper": ["10290"], "zinc": ["10300"], "selenium": ["10340"],
}
CIQUAL_CONFIDENCE = {"A": "measured", "B": "measured", "C": "calculated", "D": "imputed"}


def _ciqual_value(text: str):
    """'0,02' -> 0.02; 'traces' -> 0; '< 0,08' (below the detection limit) -> 0, assumed_zero; '-' -> None.
    Half the bound would put 0.125 mg iron in cheddar and 0.2 g omega-3 in carrots."""
    t = (text or "").strip().replace(",", ".")
    if not t or t == "-":
        return None, None
    if t == "traces":
        return 0.0, None
    if t.startswith("<"):
        return 0.0, "assumed_zero"
    return _num(t), None


def import_ciqual(xml_dir: Path) -> dict:
    """Ciqual 2025 XML (alim_*.xml, compo_*.xml). Each value's confidence code A-D becomes its
    quality tag; vitamin A is recomputed as RAE (retinol + beta-carotene / 12)."""
    import xml.etree.ElementTree as ET
    alim = next(xml_dir.glob("alim_2*.xml"))
    compo = next(xml_dir.glob("compo_*.xml"))
    foods = {}
    for el in ET.parse(alim).getroot():
        code = (el.findtext("alim_code") or "").strip()
        foods[code] = {"name": (el.findtext("alim_nom_eng") or "").strip(),
                       "name_fr": (el.findtext("alim_nom_fr") or "").strip(), "vals": {}}
    for ev, el in ET.iterparse(compo):
        if el.tag != "COMPO":
            continue
        food = foods.get((el.findtext("alim_code") or "").strip())
        value, forced = _ciqual_value(el.findtext("teneur"))
        if food is not None and value is not None:
            conf = (el.findtext("code_confiance") or "").strip()
            food["vals"][(el.findtext("const_code") or "").strip()] = (value, forced or CIQUAL_CONFIDENCE.get(conf, "unknown"))
        el.clear()
    out = {}
    for code, food in foods.items():
        vals, per_100g, quality = food["vals"], {}, {}
        for field, options in CIQUAL_CONSTS.items():
            for option in options:
                parts = option if isinstance(option, tuple) else (option,)
                if parts[0] in vals:
                    per_100g[field] = round(sum(vals[p][0] for p in parts if p in vals), 4)
                    quality[field] = vals[parts[0]][1]
                    break
        retinol, carotene = vals.get("51200"), vals.get("51330")
        if retinol or carotene:
            per_100g["vitamin_a"] = round((retinol[0] if retinol else 0.0) + (carotene[0] / 12 if carotene else 0.0), 4)
            quality["vitamin_a"] = (retinol or carotene)[1]
        elif "51104" in vals:
            per_100g["vitamin_a"], quality["vitamin_a"] = vals["51104"][0], vals["51104"][1]
        out[f"ciqual:{code}"] = {"key": f"ciqual:{code}", "source": "ciqual", "id": code, "name": food["name"],
                                 "alt_names": [food["name_fr"]], "per_100g": per_100g, "quality": quality,
                                 "portions": []}
    return out


# --- MEXT (Japan, Standard Tables of Food Composition, 8th ed., 2023 enlarged) -------------------

# Japanese-language Excel; columns are read by their INFOODS-style identifiers (成分識別子 row).
# "(1.2)" is an estimate (calculated), "Tr" / "(Tr)" a trace (0), "(0)" assumed zero, "-" / "*" unknown.
MEXT_MAIN = {
    "energy": "ENERC_KCAL", "water": "WATER", "protein": "PROT-", "fat": "FAT-", "carbs": "CHOCDF-", "fiber": "FIB-",
    "sugar_alcohol": "POLYL", "ash": "ASH", "alcohol": "ALC", "cholesterol": "CHOLE", "sodium": "NA", "potassium": "K",
    "calcium": "CA", "magnesium": "MG", "phosphorus": "P", "iron": "FE", "zinc": "ZN", "copper": "CU", "manganese": "MN",
    "selenium": "SE", "vitamin_a": "VITA_RAE", "vitamin_d": "VITD", "vitamin_e": "TOCPHA", "vitamin_k": "VITK",
    "b1_thiamine": "THIA", "b2_riboflavin": "RIBF", "b3_niacin": "NIA", "b5_pantothenic_acid": "PANTAC",
    "b6_pyridoxine": "VITB6A", "b12_cobalamin": "VITB12", "folate": "FOL", "vitamin_c": "VITC",
}
MEXT_CARBS = {"starch": "STARCH", "glucose": "GLUS", "fructose": "FRUS", "galactose": "GALS", "sucrose": "SUCS",
              "maltose": "MALS", "lactose": "LACS"}
MEXT_FATS = {"saturated": "FASAT", "monounsaturated": "FAMS", "polyunsaturated": "FAPU"}
MEXT_AMINO = {"isoleucine": "ILE", "leucine": "LEU", "lysine": "LYS", "methionine": "MET", "cystine": "CYS",
              "phenylalanine": "PHE", "tyrosine": "TYR", "threonine": "THR", "tryptophan": "TRP", "valine": "VAL",
              "histidine": "HIS", "arginine": "ARG", "alanine": "ALA", "aspartic_acid": "ASP", "glutamic_acid": "GLU",
              "glycine": "GLY", "proline": "PRO", "serine": "SER"}


def _mext_value(cell):
    """(value, quality) for one MEXT cell, or None when unknown."""
    t = str(cell).strip() if cell is not None else ""
    if t in ("", "-", "*", "†"):
        return None
    estimated = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    if t == "Tr":
        return 0.0, "calculated" if estimated else "measured"
    v = _num(t)
    if v is None:
        return None
    if estimated:
        return v, "assumed_zero" if v == 0 else "calculated"
    return v, "measured"


def _mext_table(path: Path) -> dict:
    """{food number: {identifier: (value, quality)}, "_names": {food number: name}} from the whole-table sheet."""
    import openpyxl
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    ids, out, names = None, {}, {}
    for r in ws.iter_rows(values_only=True):
        if r and "成分識別子" in [str(x).strip() for x in r[:5] if x is not None]:   # the label sits in column D
            ids = [str(x).strip() if x is not None else "" for x in r]
            continue
        if ids is None or len(r) < 4 or not re.fullmatch(r"\d{5}", str(r[1] or "").strip()):
            continue
        code = str(r[1]).strip()
        names[code] = re.sub(r"[　\s]+", " ", str(r[3] or "")).strip()
        out[code] = {ids[i]: v for i in range(4, len(r)) if i < len(ids) and ids[i] for v in [_mext_value(r[i])] if v}
    out["_names"] = names
    return out


def import_mext(folder: Path) -> dict:
    """MEXT 2023: main table + carbohydrate, fatty-acid and amino-acid tables (each per 100 g edible portion).
    Names stay Japanese (the tables have no English names); fatty acids and amino acids in mg become g."""
    main = _mext_table(next(folder.glob("*main*.xlsx")))
    carbs = _mext_table(next(folder.glob("*carbohydrates*.xlsx")))
    fats = _mext_table(next(folder.glob("*fatty_acids*.xlsx")))
    amino = _mext_table(next(folder.glob("*amino_acids*.xlsx")))
    out = {}
    for code, name in main["_names"].items():
        v, per_100g, quality = main[code], {}, {}
        for field, ident in MEXT_MAIN.items():
            if ident in v:
                per_100g[field], quality[field] = v[ident]
        c = carbs.get(code, {})
        for field, ident in MEXT_CARBS.items():
            if ident in c:
                per_100g[field], quality[field] = c[ident]
        sugar_parts = [c[i] for i in ("GLUS", "FRUS", "GALS", "SUCS", "MALS", "LACS", "TRES") if i in c]
        if {"GLUS", "FRUS", "SUCS"} <= set(c):
            per_100g["sugars"], quality["sugars"] = round(sum(p[0] for p in sugar_parts), 4), sugar_parts[0][1]
        f = fats.get(code, {})
        for field, ident in MEXT_FATS.items():
            if ident in f:
                per_100g[field], quality[field] = f[ident]
        n3 = [f[i] for i in ("F18D3N3", "F20D5N3", "F22D5N3", "F22D6N3") if i in f]
        if n3:
            per_100g["omega_3"], quality["omega_3"] = round(sum(x[0] for x in n3) / 1000, 4), n3[0][1]
        n6 = [f[i] for i in ("F18D2N6", "F20D4N6") if i in f]
        if n6:
            per_100g["omega_6"], quality["omega_6"] = round(sum(x[0] for x in n6) / 1000, 4), n6[0][1]
        a = amino.get(code, {})
        for field, ident in MEXT_AMINO.items():
            if ident in a:
                per_100g[field], quality[field] = round(a[ident][0] / 1000, 4), a[ident][1]
        out[f"mext:{code}"] = {"key": f"mext:{code}", "source": "mext", "id": code, "name": name, "alt_names": [],
                               "per_100g": per_100g, "quality": quality, "portions": []}
    return out


# --- AFCD (Australia, FSANZ Australian Food Composition Database, Release 3) ----------------------

AFCD_COLUMNS = {   # library field -> AFCD column name (units as in AFCD; see AFCD_SCALE)
    "water": "Moisture (water)  (g)", "protein": "Protein  (g)", "fat": "Fat, total  (g)", "ash": "Ash  (g)",
    "fiber": "Total dietary fibre  (g)", "alcohol": "Alcohol  (g)", "fructose": "Fructose  (g)", "glucose": "Glucose  (g)",
    "sucrose": "Sucrose (g)", "maltose": "Maltose  (g)", "lactose": "Lactose  (g)", "galactose": "Galactose  (g)",
    "sugars": "Total sugars (g)", "starch": "Starch  (g)", "calcium": "Calcium (Ca)  (mg)", "copper": "Copper (Cu)  (mg)",
    "iron": "Iron (Fe)  (mg)", "magnesium": "Magnesium (Mg)  (mg)", "manganese": "Manganese (Mn)  (mg)",
    "phosphorus": "Phosphorus (P)  (mg)", "potassium": "Potassium (K)  (mg)", "selenium": "Selenium (Se)  (ug)",
    "sodium": "Sodium (Na)  (mg)", "zinc": "Zinc (Zn)  (mg)", "b1_thiamine": "Thiamin (B1)  (mg)",
    "b2_riboflavin": "Riboflavin (B2)  (mg)", "b3_niacin": "Niacin (B3)  (mg)", "b5_pantothenic_acid": "Pantothenic acid (B5)  (mg)",
    "b6_pyridoxine": "Pyridoxine (B6)  (mg)", "b12_cobalamin": "Cobalamin (B12)  (ug)", "folate": "Dietary folate equivalents  (ug)",
    "vitamin_c": "Vitamin C  (mg)", "vitamin_d": "Vitamin D3 equivalents  (ug)", "vitamin_e": "Alpha tocopherol  (mg)",
    "saturated": "Total saturated fatty acids, equated  (g)", "monounsaturated": "Total monounsaturated fatty acids, equated  (g)",
    "polyunsaturated": "Total polyunsaturated fatty acids, equated  (g)", "transfats": "Total trans fatty acids, imputed  (mg)",
    "caffeine": "Caffeine  (mg)", "cholesterol": "Cholesterol  (mg)",
    **{f: f"{n}  (mg)" for f, n in (("alanine", "Alanine"), ("arginine", "Arginine"), ("aspartic_acid", "Aspartic acid"),
                                    ("cystine", "Cystine plus cysteine"), ("glutamic_acid", "Glutamic acid"), ("glycine", "Glycine"),
                                    ("histidine", "Histidine"), ("isoleucine", "Isoleucine"), ("leucine", "Leucine"),
                                    ("lysine", "Lysine"), ("methionine", "Methionine"), ("phenylalanine", "Phenylalanine"),
                                    ("proline", "Proline"), ("serine", "Serine"), ("threonine", "Threonine"),
                                    ("tyrosine", "Tyrosine"), ("tryptophan", "Tryptophan"), ("valine", "Valine"))},
}
AFCD_SCALE = {"transfats": 0.001, **{f: 0.001 for f in ("alanine", "arginine", "aspartic_acid", "cystine", "glutamic_acid",
                                                       "glycine", "histidine", "isoleucine", "leucine", "lysine", "methionine",
                                                       "phenylalanine", "proline", "serine", "threonine", "tyrosine",
                                                       "tryptophan", "valine")}}
AFCD_DERIVATION = {"Analysed": "measured", "Recipe": "calculated", "Borrowed": "borrowed", "Imputed": "imputed",
                   "Label Data": "label", "Estimated": "imputed"}


def import_afcd(xlsx: Path) -> dict:
    """AFCD Release 3, all foods per 100 g. Energy is kJ (with fibre) / 4.184; carbs by difference are the
    available carbohydrate (with sugar alcohols) plus fibre; vitamin A as RAE (retinol + beta-carotene eq. / 12)."""
    import openpyxl
    ws = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)["All solids & liquids per 100 g"]
    rows = ws.iter_rows(min_row=3, values_only=True)
    head = [re.sub(r"\s*\n\s*", " ", str(h)).strip() if h else "" for h in next(rows)]
    col = {h: i for i, h in enumerate(head)}
    norm = {re.sub(r"\s+", " ", k): k for k in col}
    def c(name):
        return col.get(norm.get(re.sub(r"\s+", " ", name)))
    out = {}
    for r in rows:
        if not r or not r[0]:
            continue
        q = AFCD_DERIVATION.get(r[col["Derivation"]], "unknown")
        val = lambda name: _num(r[c(name)]) if c(name) is not None else None
        per_100g = {}
        for field, name in AFCD_COLUMNS.items():
            v = val(name)
            if v is not None:
                per_100g[field] = round(v * AFCD_SCALE.get(field, 1.0), 4)
        kj = val("Energy with dietary fibre, equated (kJ)")
        if kj is not None:
            per_100g["energy"] = round(kj / 4.184, 1)
        avail = val("Available carbohydrate, with sugar alcohols (g)")
        if avail is not None:
            per_100g["carbs"] = round(avail + per_100g.get("fiber", 0.0), 4)
        retinol, carotene = val("Retinol (preformed vitamin A) (ug)"), val("Beta-carotene equivalents (provitamin A) (ug)")
        if retinol is not None or carotene is not None:
            per_100g["vitamin_a"] = round((retinol or 0.0) + (carotene or 0.0) / 12, 4)
        n3 = [(val("C18:3w3 (g)"), 1), (val("C20:5w3 (mg)"), 1000), (val("C22:5w3 (mg)"), 1000), (val("C22:6w3 (mg)"), 1000)]
        if any(v is not None for v, _ in n3):
            per_100g["omega_3"] = round(sum((v or 0.0) / d for v, d in n3), 4)
        n6 = [(val("C18:2w6 (g)"), 1), (val("C20:4w6 (mg)"), 1000)]
        if any(v is not None for v, _ in n6):
            per_100g["omega_6"] = round(sum((v or 0.0) / d for v, d in n6), 4)
        key = f"afcd:{r[0]}"
        out[key] = {"key": key, "source": "afcd", "id": str(r[0]), "name": str(r[col["Food Name"]]).strip(), "alt_names": [],
                    "per_100g": per_100g, "quality": {f: q for f in per_100g}, "portions": []}
    return out


# --- NEVO (Netherlands, RIVM NEVO-online 2025) ---------------------------------------------------

NEVO_CODES = {   # library field -> NEVO code (units as in NEVO; tryptophan mg -> g)
    "energy": "ENERCC", "water": "WATER", "protein": "PROT", "fat": "FAT", "fiber": "FIBT", "starch": "STARCH",
    "sugars": "SUGAR", "sugar_alcohol": "POLYL", "alcohol": "ALC", "ash": "ASH", "saturated": "FASAT",
    "monounsaturated": "FAMSCIS", "polyunsaturated": "FAPU", "transfats": "FATRS", "cholesterol": "CHORL",
    "sodium": "NA", "potassium": "K", "calcium": "CA", "phosphorus": "P", "magnesium": "MG", "iron": "FE",
    "copper": "CU", "selenium": "SE", "zinc": "ZN", "vitamin_a": "VITA_RAE", "vitamin_d": "VITD", "vitamin_k": "VITK",
    "b1_thiamine": "THIA", "b2_riboflavin": "RIBF", "b6_pyridoxine": "VITB6", "b12_cobalamin": "VITB12",
    "b3_niacin": "NIA", "folate": "FOL", "vitamin_c": "VITC", "tryptophan": "TRP",
}


def import_nevo(folder: Path) -> dict:
    """NEVO 2025 (pipe-separated, decimal commas). Carbs by difference = available carbohydrate + fibre +
    polyols. Each value's source code (Details file) sets its quality: MI* calculated, REF.* measured."""
    main = next(p for p in folder.glob("NEVO*.csv") if re.fullmatch(r"NEVO\d{4}_v[\d.]+\.csv", p.name))
    num = lambda s: _num((s or "").strip().strip('"').replace(",", "."))
    quality = {}
    details = next(folder.glob("*_Details.csv"), None)
    if details:
        with open(details, encoding="utf-8", newline="") as f:
            for row in csv.reader(f, delimiter="|"):
                if len(row) > 15 and row[3].isdigit():
                    src = row[15].strip()
                    quality[(row[3], row[9].strip())] = "calculated" if src.startswith("MI") else "measured" if src else "unknown"
    out = {}
    with open(main, encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="|")
        head = [h.strip().strip('"') for h in next(reader)]
        col = {h.split(" (")[0].split("/")[-1].strip(): i for i, h in enumerate(head)}
        for r in reader:
            if len(r) < len(head) or not r[col["NEVO-code"]].strip().isdigit():
                continue
            code = r[col["NEVO-code"]].strip()
            per_100g, qual = {}, {}
            for field, nc_ in NEVO_CODES.items():
                v = num(r[col[nc_]]) if nc_ in col else None
                if v is not None:
                    per_100g[field] = round(v / 1000, 4) if field == "tryptophan" else v
                    qual[field] = quality.get((code, nc_), "unknown")
            cho = num(r[col["CHO"]])
            if cho is not None:
                per_100g["carbs"] = round(cho + per_100g.get("fiber", 0.0) + per_100g.get("sugar_alcohol", 0.0), 4)
                qual["carbs"] = quality.get((code, "CHO"), "unknown")
            ve = num(r[col["TOCPHA"]]) if "TOCPHA" in col else None
            if ve is None and "VITE" in col:
                ve = num(r[col["VITE"]])
            if ve is not None:
                per_100g["vitamin_e"], qual["vitamin_e"] = ve, quality.get((code, "TOCPHA"), "unknown")
            n3 = [num(r[col[c]]) for c in ("F18:3CN3", "F20:5CN3", "F22:5CN3", "F22:6CN3") if c in col]
            if any(v is not None for v in n3):
                per_100g["omega_3"], qual["omega_3"] = round(sum(v or 0.0 for v in n3), 4), qual.get("polyunsaturated", "unknown")
            n6 = [num(r[col[c]]) for c in ("F18:2CN6", "F20:4CN6") if c in col]
            if any(v is not None for v in n6):
                per_100g["omega_6"], qual["omega_6"] = round(sum(v or 0.0 for v in n6), 4), qual.get("polyunsaturated", "unknown")
            key = f"nevo:{code}"
            out[key] = {"key": key, "source": "nevo", "id": code, "name": r[col["Food name"]].strip(),
                        "alt_names": [r[col["Dutch food name"]].strip(), r[col["Synoniem"]].strip()],
                        "per_100g": per_100g, "quality": qual, "portions": []}
    return out


# --- IFCT (India, Indian Food Composition Tables 2017, NIN-ICMR; data via github.com/nodef/ifct2017) ---

# Every mass in the CSV is grams per 100 g (energy kJ). Library field -> (IFCT code(s), factor to the
# library's unit); a tuple of codes is summed.
IFCT_COLUMNS = {
    "water": ("water", 1), "protein": ("protcnt", 1), "fat": ("fatce", 1), "fiber": ("fibtg", 1), "starch": ("starch", 1),
    "sugars": ("fsugar", 1), "fructose": ("frus", 1), "glucose": ("glus", 1), "sucrose": ("sucs", 1), "maltose": ("mals", 1),
    "lactose": ("lactose", 1), "ash": ("ash", 1), "saturated": ("fasat", 1), "monounsaturated": ("fams", 1),
    "polyunsaturated": ("fapu", 1), "transfats": ("fatrn", 1),
    "omega_3": (("f18d3n3", "f20d5n3", "f22d5n3", "f22d6n3"), 1), "omega_6": (("f18d2cn6", "f20d4n6"), 1),
    "cholesterol": ("cholc", 1000), "calcium": ("ca", 1000), "iron": ("fe", 1000), "magnesium": ("mg", 1000),
    "phosphorus": ("p", 1000), "potassium": ("k", 1000), "sodium": ("na", 1000), "copper": ("cu", 1000),
    "manganese": ("mn", 1000), "zinc": ("zn", 1000), "selenium": ("se", 1e6), "b1_thiamine": ("thia", 1000),
    "b2_riboflavin": ("ribf", 1000), "b3_niacin": ("nia", 1000), "b5_pantothenic_acid": ("pantac", 1000),
    "b6_pyridoxine": ("vitb6c", 1000), "folate": ("folsum", 1e6), "vitamin_c": ("vitc", 1000),
    "vitamin_e": ("tocpha", 1000), "vitamin_k": (("vitk1", "vitk2"), 1e6), "vitamin_d": (("ergcal", "chocal"), 1e6),
    **{f: (c, 1) for f, c in (("histidine", "his"), ("isoleucine", "ile"), ("leucine", "leu"), ("lysine", "lys"),
                              ("methionine", "met"), ("cystine", "cys"), ("phenylalanine", "phe"), ("threonine", "thr"),
                              ("tryptophan", "trp"), ("valine", "val"), ("alanine", "ala"), ("arginine", "arg"),
                              ("aspartic_acid", "asp"), ("glutamic_acid", "glu"), ("glycine", "gly"), ("proline", "pro"),
                              ("serine", "ser"), ("tyrosine", "tyr"))},
}
# Fields where IFCT's "0 ± 0" usually means "not analysed" (whole milk shows 0 cholesterol and 0 retinol),
# so such a zero is left out; macros and fatty acids keep their zeros.
IFCT_ZERO_UNKNOWN = {"cholesterol", "calcium", "iron", "magnesium", "phosphorus", "potassium", "sodium", "copper",
                     "manganese", "zinc", "selenium", "b1_thiamine", "b2_riboflavin", "b3_niacin", "b5_pantothenic_acid",
                     "b6_pyridoxine", "folate", "vitamin_c", "vitamin_e", "vitamin_k", "vitamin_d", "vitamin_a",
                     "histidine", "isoleucine", "leucine", "lysine", "methionine", "cystine", "phenylalanine", "threonine",
                     "tryptophan", "valine", "alanine", "arginine", "aspartic_acid", "glutamic_acid", "glycine", "proline",
                     "serine", "tyrosine"}


def import_ifct(csv_path: Path) -> dict:
    """IFCT 2017: 542 foods analysed across six regions of India (all values measured). Carbs by difference
    = available carbohydrate (choavldf) + fibre; vitamin A as RAE (retinol + beta-carotene equivalents / 12)."""
    out = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            def val(code):
                v, e = _num(r.get(code)), _num(r.get(code + "_e"))
                return v, (v == 0 and (e or 0) == 0)
            per_100g = {}
            for field, (codes, factor) in IFCT_COLUMNS.items():
                parts = [val(c) for c in (codes if isinstance(codes, tuple) else (codes,))]
                known = [(v, z) for v, z in parts if v is not None]
                if not known:
                    continue
                total = sum(v for v, _ in known)
                if total == 0 and field in IFCT_ZERO_UNKNOWN and all(z for _, z in known):
                    continue
                per_100g[field] = round(total * factor, 4)
            kj = _num(r.get("enerc"))
            if kj:
                per_100g["energy"] = round(kj / 4.184, 1)
            cho = _num(r.get("choavldf"))
            if cho is not None:
                per_100g["carbs"] = round(cho + per_100g.get("fiber", 0.0), 4)
            retinol, carot = _num(r.get("retol")), _num(r.get("cartbeq") or r.get("cartb"))
            animal = re.search(r"milk|egg|meat|poultry|fish|mollus|shellfish", r.get("grup") or "", re.I)
            retinol_unknown = animal and val("retol")[1]       # an animal food's "0 ± 0" retinol was not analysed
            if not retinol_unknown and ((retinol or 0) > 0 or (carot or 0) > 0):
                per_100g["vitamin_a"] = round(((retinol or 0.0) + (carot or 0.0) / 12) * 1e6, 4)
            key = f"ifct:{r['code']}"
            out[key] = {"key": key, "source": "ifct", "id": r["code"], "name": r["name"].strip(),
                        "alt_names": [r.get("scie") or "", r.get("lang") or ""], "per_100g": per_100g,
                        "quality": {k: "measured" for k in per_100g}, "portions": []}
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
    """Imported records of one source (cnf, frida, cofid, ciqual, mext, afcd, nevo, ifct or on); SR Legacy goes
    through sr_records()."""
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
    p.add_argument("--cofid", nargs="+", help="CoFID 2021 xlsx, then optionally the old-foods xlsx")
    p.add_argument("--ciqual", help="folder with the Ciqual 2025 XML files")
    p.add_argument("--mext", help="folder with the MEXT 2023 xlsx files (main, carbohydrates, fatty_acids, amino_acids)")
    p.add_argument("--afcd", help="AFCD Release 3 - Nutrient profiles.xlsx")
    p.add_argument("--nevo", help="folder with the NEVO 2025 CSV files")
    p.add_argument("--ifct", help="IFCT 2017 compositions CSV (github.com/nodef/ifct2017, compositions/index.csv)")
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
                                ("on", args.opennutrition, on_reader),
                                ("cofid", args.cofid, lambda paths: import_cofid(*map(Path, paths))),
                                ("ciqual", args.ciqual, import_ciqual), ("mext", args.mext, import_mext),
                                ("afcd", args.afcd, import_afcd), ("nevo", args.nevo, import_nevo),
                                ("ifct", args.ifct, import_ifct)):
        if arg:
            records = reader(arg if isinstance(arg, list) else Path(arg))
            print(f"{source}: {len(records)} foods -> {save(source, records)}", file=sys.stderr)


if __name__ == "__main__":
    main()
