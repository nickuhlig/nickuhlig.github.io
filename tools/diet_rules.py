#!/usr/bin/env python3
"""
Which menu types a recipe can appear in, worked out from its ingredients.

Mealime's menu types (ids): 1 Classic, 2 Vegetarian, 3 Flexitarian, 4 Pescatarian, 5 Paleo, 6 Vegan, 7 Low Carb, 8 Keto.
Measured on the library's own labels, they nest:  vegan  ->  vegetarian + pescatarian  ->  classic + flexitarian, so a
recipe's list is built up from the base [1, 3]:
    no meat / poultry / gelatin                   +4  pescatarian        (fish and shellfish are fine)
    ... and no fish or shellfish                  +2  vegetarian         (+4, as above)
    ... and no dairy, egg, honey                  +6  vegan              (+2, +4)
Paleo, Low Carb and Keto follow the user's definitions (Mealime's own labels are used only as a rough cross-check -- a
blanket label is shared by all variants of a recipe family, so they are noisy):
    5  paleo       no ingredient flagged "not_paleo" and no dairy.  Forbidden: grains (wheat, rye, barley, oats, rice, corn, spelt,
                   quinoa, buckwheat, amaranth), legumes (beans, lentils, peas, chickpeas, peanuts, soy, tofu, edamame), dairy
                   (milk, cheese, yogurt, butter, ghee, cream), white / brown sugar, HFCS, agave, candy, artificial sweeteners,
                   canola / soybean / corn / sunflower / safflower oil, trans fats, canned meals, boxed snacks, deli meats with
                   nitrates, frozen dinners, soda, fruit juice, sweetened tea, beer, wine, sweetened coffee drinks.
                   Honey, maple syrup and dates are allowed.
    7  low carb    digestible carbohydrate (carbs minus fibre) <= LOW_CARB_SHARE of the calories  (Mealime: 20 %)
    8  keto        digestible carbohydrate <= KETO_SHARE of the calories                           (about 10 %; also low carb)
    The share is a ratio, so it is the same whether the source's nutrition is per serving or for the whole recipe.

Each ingredient has a set of animal-product flags, stored on its entry in ingredient_master.json as "diet_flags"
(the master is the single source of truth -- fix a flag there and every recipe follows):
  * when an entry is decided without flags, fill_missing_flags() suggests them (the importer does this on every run):
    fish / shellfish / dairy / egg from its restrictions (violates 3 / 4 / 2 / 11), meat and honey from its NAME
    (the regexes below), plus NAME_OVERRIDES for ingredients whose name hides what is in them;
  * the review report shows the suggested flags next to the suggested restrictions for every new ingredient;
  * `diet_rules.py --check <library> --audit` lists the entries whose stored flags differ from their restrictions
    (deliberate exceptions: Thai curry pastes carry shellfish, chocolate chips carry dairy).
An ingredient the master hasn't decided yet makes the whole recipe "unknown" -> only the base [1, 3]: a recipe is never
called vegetarian on a guess.

Checked against the 27,000 Mealime / Albertsons recipes that carry real labels (diet_rules.py --check <library dir>):
about 98% agreement for vegetarian, 96% pescatarian, 99% vegan; the leftovers are Mealime's own quirks.
"""

import re
from pathlib import Path

BASE_TYPES = [1, 3]
ANIMAL_FLAGS = ("meat", "fish", "shellfish", "dairy", "egg", "honey")
DIET_FLAGS = ANIMAL_FLAGS + ("not_paleo",)
# share of the calories that comes from digestible carbohydrate (4 kcal per gram of carbs minus fibre)
LOW_CARB_SHARE = 0.20
KETO_SHARE = 0.10

# words that say "this is the plant-based version", which switch the meat words off
PLANT = re.compile(r"\b(veggie|vegan|plant[- ]based|meatless|vegetarian|imitation|faux|tofu|seitan|tempeh|jackfruit|mushrooms?|impossible|beyond|"
                   r"vegetables?|veg|dairy[- ]free|non[- ]dairy|coconut)\b", re.I)
MEAT = re.compile(
    r"\b(beef|pork|chicken|turkey|duck|goose|lamb|mutton|veal|venison|bison|rabbit|bacon|ham|prosciutto|pancetta|guanciale|salami|pepperoni|"
    r"soppressata|sausages?|chorizo|bratwurst|kielbasa|andouille|hot dogs?|frankfurters?|meatballs?|meatloaf|ribs?|brisket|steaks?|tenderloin|"
    r"oxtails?|tripe|liver|giblets?|gizzards?|bones?\b(?!-in)|marrow|lard|tallow|schmaltz|suet|pastrami|mortadella|jerky|corned beef|pot roast|roast beef|"
    r"drumsticks?|wings?|thighs?|breasts?|cutlets?|chops?|sirloin|chuck|flank|shank|tongue|trotters?|pig|pigs|cow foot|bologna|spam|hock|jowl|"
    r"top round|bottom round|eye of round|eye round|round roast|rump|hanger|skirt|"
    r"capicola|coppa|speck|nduja|chicharr[oó]n|pork rinds|meat|poultry|squab|quail|pheasant|ostrich|carne|al pastor|foie gras|blood|"
    r"gelatin|marshmallows?|gummy|gummies|gummi|jell-?o|candy corn|gumdrops?|jelly beans?)\b", re.I)
MEAT_BROTH = re.compile(r"\b(chicken|beef|pork|bone|meat|turkey|lamb|duck|veal|ham)\b.*\b(broth|stock|bouillon|consomm[eé]|base|gravy|soup)\b|"
                        r"\b(broth|stock|bouillon|gravy)\b.*\b(chicken|beef|pork|bone|meat|turkey)\b", re.I)
NOT_MEAT = re.compile(r"\b(hamburger buns?|buns?|graham|chicken[- ]flavor(ed)? (seasoning|ramen)|buffalo|duck sauce|lamb'?s lettuce|ribs? of celery|"
                      r"celery ribs?|bonito|hot dog buns?|hot dog rolls?|rolls?|eggplants?|gummy fish|"
                      r"chicken of the woods|ham radio)\b", re.I)
HONEY = re.compile(r"\b(honey|beeswax|royal jelly|bee pollen)\b", re.I)
# --- Paleo (strict; dairy is handled by the "dairy" flag, honey / maple syrup / dates are allowed) ---------------------------
NOT_PALEO = re.compile(
    # grains and what is made of them
    r"\b(flour|breads?|buns?|rolls?|pitas?|naan|flatbreads?|tortillas?|wraps?|bagels?|baguettes?|ciabatta|focaccia|croutons?|crackers?|pretzels?|"
    r"pancake mix|biscuits?|cookies?|cake|brownies?|muffins?|pie crust|pastry|puff pastry|phyllo|filo|pizza (dough|crust)|"
    r"pasta|spaghetti|penne|rotini|fusilli|linguine|fettuccine|tagliatelle|pappardelle|orzo|macaroni|lasagna|lasagne|rigatoni|farfalle|"
    r"noodles?|ramen|udon|soba|vermicelli|gnocchi|ravioli|tortellini|dumplings?|wontons?|couscous|"
    r"rice|quinoa|oats?|oatmeal|granola|cereal|cornmeal|polenta|grits|barley|farro|bulgur|millet|sorghum|semolina|spelt|rye|wheat|"
    r"buckwheat|amaranth|teff|panko|bread ?crumbs?|cornstarch|corn ?starch|corn|corn ?flakes|popcorn|masa|hominy|seitan|stuffing|"
    # legumes and soy
    r"beans?|chickpeas?|garbanzo|lentils?|split peas?|black[- ]eyed peas?|peas|edamame|tofu|tempeh|soy|soya|miso|hummus|falafel|"
    r"peanuts?|peanut butter|natto|"
    # potatoes (the user's list does not name them; Mealime's Paleo excludes them)
    r"potato(es)?|hash browns?|french fries|tater tots?|fries|"
    # sugars, sweeteners, candy
    r"sugar|corn syrup|high[- ]fructose|agave|simple syrup|golden syrup|pancake syrup|sweetened condensed milk|xylitol|"
    r"aspartame|sucralose|saccharin|acesulfame|splenda|artificial sweeteners?|sugar substitute|"
    r"candy|candies|lollipops?|gummy|gummies|marshmallows?|licorice|caramels?|toffee|sprinkles|jelly beans?|frosting|icing|"
    r"jams?|jellies|jelly|preserves|marmalade|ice cream|chocolate chips?|"
    # seed oils and trans fats
    r"canola|rapeseed|soybean oil|vegetable oil|corn oil|sunflower oil|safflower oil|shortening|margarine|hydrogenated|cooking spray|"
    # processed, canned, boxed, frozen, cured with nitrates
    r"baking powder|baking soda|nutritional yeast|ketchup|catsup|hoisin|teriyaki|barbecue sauce|bbq sauce|stir[- ]fry sauce|marinara|alfredo|"
    r"tzatziki|pesto|dressing|vinaigrette|mayonnaise|vegan mayo|veggie burger|veggie ground|plant[- ]based|vegan (sausage|cheese|butter|burger)|"
    r"protein powder|taco seasoning|"
    r"canned (soup|chili|stew|ravioli|pasta|meals?)|condensed soup|cream of [a-z]+ soup|frozen (dinners?|meals?|pizza|waffles?)|"
    r"nuggets|fish sticks|instant (noodles|rice|potatoes|oatmeal)|cake mix|brownie mix|cornbread mix|mac(aroni)? and cheese|"
    r"tortilla chips|potato chips|corn chips|cheese puffs|"
    r"deli|lunch meats?|cold cuts?|salami|pepperoni|bologna|mortadella|hot dogs?|frankfurters?|ham|bacon|prosciutto|pancetta|pastrami|corned beef|"
    r"jerky|spam|"
    # drinks
    r"soda|cola|ginger ale|ginger beer|root beer|tonic water|lemonade|(apple|orange|grape|cranberry|pineapple|pomegranate|cherry|prune|mango) juice|"
    r"fruit juice|juice blend|sweet(ened)? tea|iced tea|bubble tea|boba|chai latte|frappuccino|coffee syrup|flavored syrup|"
    r"beer|ale|lager|stout|porter|ipa|wine(?! vinegar)|champagne(?! vinegar)|prosecco|sake|mirin|marsala|sherry(?! vinegar)|vermouth|hard cider)\b", re.I)
# the words above that still leave the ingredient paleo
PALEO_OK = re.compile(
    r"\b(green beans?|string beans?|snap peas?|snow peas?|sugar snap|vanilla beans?|coffee beans?|cocoa beans?|castor|"
    r"cauliflower rice|cauliflower|riced|rice vinegar|rice wine vinegar|sweet potato(es)?|yams?|"
    r"almond (meal|flour)|almond meal/flour|coconut flour|cassava flour|tapioca flour|arrowroot|nut flour|"
    r"zucchini noodles?|zoodles?|kelp noodles?|shirataki|spaghetti squash|sweet potato noodles?|"
    r"avocado oil mayo(nnaise)?|paleo|sugar[- ]free|no sugar|unsweetened|coconut sugar|date sugar|maple sugar|"
    r"corn on the cob|baby corn|lettuce wraps?|collard wraps?|egg wraps?|"
    r"mushrooms?|bean sprouts?|beansprouts?|sprouts?|peppercorns?|soy[- ]free|coconut aminos|"
    r"soda water|club soda|seltzer|uncured|nitrate[- ]free|nitrite[- ]free|no nitrates?|no added nitrates?|ham hocks?|hamhock|"
    r"salad dressing mix|oil and vinegar)\b", re.I)

# ingredients whose NAME does not show what is in them (verified against Mealime's labels): pattern -> flags
NAME_OVERRIDES = [
    (re.compile(r"\bcurry paste\b", re.I), {"shellfish"}),                    # Thai pastes contain shrimp paste
    (re.compile(r"\bpeanut sauce\b", re.I), {"honey"}),                       # Mealime does not call it vegan
    (re.compile(r"\bchocolate chips?\b", re.I), {"dairy"}),                   # the user's rule: chocolate chips = dairy
    (re.compile(r"\bNigerian[- ](style )?(broth|stock)\b", re.I), {"meat"}),  # a beef / chicken stock whose name does not say so
    (re.compile(r"\btamale filling\b|\bimperial rolls?\b", re.I), {"meat"}),  # pork / chicken fillings that are named only by their dish
]


def ingredient_flags(entry: dict) -> set:
    """Animal-product flags of one ingredient-master entry: the ones stored on it (the master is the source of truth);
    for an entry that has none yet, the suggestion below."""
    explicit = entry.get("diet_flags")
    if explicit is not None:
        return set(explicit)
    return suggest_flags(entry)


def fill_missing_flags(master) -> int:
    """Gives every DECIDED entry that has no stored diet_flags the suggested ones. Returns how many were filled in."""
    filled = 0
    for entry in master.entries.values():
        if entry.get("violates") is not None and entry.get("diet_flags") is None:
            entry["diet_flags"] = sorted(suggest_flags(entry))
            filled += 1
    return filled


def backfill_not_paleo(master) -> int:
    """One-off: adds the suggested "not_paleo" flag to decided entries whose stored diet_flags predate it. Returns how many were flagged."""
    flagged = 0
    for entry in master.entries.values():
        if entry.get("violates") is not None and entry.get("diet_flags") is not None and "not_paleo" in suggest_flags(entry) \
                and "not_paleo" not in entry["diet_flags"]:
            entry["diet_flags"] = sorted(set(entry["diet_flags"]) | {"not_paleo"})
            flagged += 1
    return flagged


def suggest_flags(entry: dict) -> set:
    """What the restrictions and the NAME say about an ingredient (used to fill diet_flags in, and to suggest them for new ones)."""
    name = entry.get("name") or ""
    violates = set(entry.get("violates") or [])
    flags = set()
    if 3 in violates:
        flags.add("fish")
    if 4 in violates:
        flags.add("shellfish")
    if 2 in violates:
        flags.add("dairy")
    if 11 in violates:
        flags.add("egg")
    if HONEY.search(name):
        flags.add("honey")
    if not PLANT.search(name) and ((MEAT.search(name) and not NOT_MEAT.search(name)) or MEAT_BROTH.search(name)):
        flags.add("meat")
    for pattern, extra in NAME_OVERRIDES:
        if pattern.search(name):
            flags |= extra
    if violates & {1, 5, 9} or (NOT_PALEO.search(name) and not PALEO_OK.search(name)):
        flags.add("not_paleo")
    return flags


def carb_share(nutrition) -> float | None:
    """Share of the calories that is digestible carbohydrate (4 kcal / g of carbs MINUS fibre: fibre is not digested, so it adds no
    calories), or None when the source gave no usable nutrition."""
    if not isinstance(nutrition, dict):
        return None
    carbs, energy, fiber = nutrition.get("carbs"), nutrition.get("energy"), nutrition.get("fiber")
    if not isinstance(carbs, (int, float)) or not isinstance(energy, (int, float)) or energy <= 0:
        return None
    return max(carbs - (fiber if isinstance(fiber, (int, float)) else 0), 0) * 4 / energy


def carb_types(nutrition) -> list:
    """7 Low Carb / 8 Keto from the carbohydrate share of the calories (independent of which ingredients are decided)."""
    share = carb_share(nutrition)
    if share is None:
        return []
    return [t for t, limit in ((7, LOW_CARB_SHARE), (8, KETO_SHARE)) if share <= limit]


def menu_types_for(flag_sets: list, nutrition=None) -> list:
    """allowed_type_ids from the flag sets of a recipe's ingredients (None in the list = an undecided ingredient) and its nutrition."""
    types = list(BASE_TYPES) + carb_types(nutrition)
    if not flag_sets or any(f is None for f in flag_sets):
        return sorted(types)
    flags = set().union(*flag_sets)
    if "not_paleo" not in flags and "dairy" not in flags:
        types.append(5)
    if "meat" not in flags:
        types.append(4)
        if not flags & {"fish", "shellfish"}:
            types.append(2)
            if not flags & {"dairy", "egg", "honey"}:
                types.append(6)
    return sorted(types)


def recipe_menu_types(ingredient_ids: list, master, nutrition=None) -> list:
    """Menu types for a recipe given its ingredient ids, an IngredientMaster (undecided / missing id -> no animal / paleo types)
    and, optionally, its per-serving nutrition (adds Low Carb / Keto)."""
    sets = []
    for ing_id in ingredient_ids:
        entry = master.entries.get(str(ing_id)) if ing_id is not None else None
        sets.append(None if entry is None or (entry.get("violates") is None and entry.get("diet_flags") is None) else ingredient_flags(entry))
    return menu_types_for(sets, nutrition)


def main() -> int:
    import argparse
    import collections
    import json
    import sys
    from ingredient_master import IngredientMaster, load_master, master_path_for
    parser = argparse.ArgumentParser(description="Compare the derived menu types with the labels of the library's real recipes.")
    parser.add_argument("--check", type=Path, required=True, help="library root (holds ingredient_master.json and families/)")
    parser.add_argument("--audit", action="store_true",
                        help="only list ingredients whose stored diet_flags differ from what their restrictions say (deliberate exceptions show here)")
    args = parser.parse_args()
    master = IngredientMaster(load_master(master_path_for(args.check)))
    if args.audit:
        wanted = {3: "fish", 4: "shellfish", 2: "dairy", 11: "egg"}
        for key, entry in master.entries.items():
            if entry.get("violates") is None or entry.get("diet_flags") is None:
                continue
            implied = {label for rid, label in wanted.items() if rid in entry["violates"]}
            stored = set(entry["diet_flags"]) & set(wanted.values())
            if implied != stored:
                print(f"{int(key):5} {entry['name']!r:60} restrictions imply {sorted(implied)} but diet_flags has {sorted(stored)}")
        return 0
    stats = collections.defaultdict(collections.Counter)
    for path in (args.check / "families").glob("*/*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or "ingredients" not in record or record.get("source") not in (None, "albertsons_native", "albertsons_native_gap_fill"):
            continue
        ids = [i.get("ingredient_id") for i in record["ingredients"] if isinstance(i, dict)]
        if not record.get("allowed_type_ids") or not ids or None in ids:
            continue
        derived = set(recipe_menu_types(ids, master, record.get("nutrition")))
        for label, tid in (("vegetarian", 2), ("pescatarian", 4), ("vegan", 6), ("paleo", 5), ("low carb", 7), ("keto", 8)):
            if tid in (7, 8) and carb_share(record.get("nutrition")) is None:
                continue                       # Albertsons records have labels but no nutrition to measure
            stats[label][(tid in record["allowed_type_ids"], tid in derived)] += 1
    for label, c in stats.items():
        tp, fp, fn, tn = c[(True, True)], c[(False, True)], c[(True, False)], c[(False, False)]
        print(f"{label:12} recipes {tp + fp + fn + tn:6}  agree {100 * (tp + tn) / (tp + fp + fn + tn):5.1f}%  "
              f"called {label} but Mealime did not: {fp:5}   Mealime did but I did not: {fn:5}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
