"""
Master lookup tables for Mealime's numeric category ids, for use alongside
mealime_json_scrape.py's saved records ("allowed_type_ids" / "violated_restriction_ids").

MENU_TYPES is exact -- pulled directly from the <select> on mealime.com/recipes.

RESTRICTIONS was originally reverse-engineered from frequency analysis + targeted
ingredient searches (a plain edamame salad isolating Soy, tahini-sauce dishes isolating
Sesame, a single "Herbed Salmon" recipe with restrictions=[3] alone isolating Fish), which
got 10 of the 12 right but had MUSTARD (13) and SULFITE (14) backwards -- that pass never
found a clean example isolating either one. Since confirmed authoritative: it's the exact
"recipe_restrictions" id/name list embedded in my.mealime.com's own JS bundle (config blob
inside main.<hash>.js, fetched via an authenticated get_user/get_builder_data session), and
independently cross-validated against known recipe data (e.g. the Chicken Parmesan Meatballs
family's violated_restriction_ids matched exactly). Treat this table as ground truth now.

Ids 7 and 8 never appear anywhere in the public catalog -- unused/reserved on Mealime's
side, not a gap in this reference.

VARIETY_TAGS (Mealime's own variety_tag_id -- a dish-type tag, e.g. "Curries",
"Stir fries") has Mealime's complete 21-value space seen anywhere in this library (ids 1-33),
found via two tiers of evidence (ids 35-43 are our own additions -- see below):

  - 10 ids (2, 4, 6, 11, 12, 14, 24, 25, 32, 33) were found by cross-referencing every
    genuine Mealime recipe's own variety_tag_ids against which Albertsons cookbook(s) it
    appeared in (Albertsons mirrors this exact taxonomy, being built on Mealime's
    infrastructure): each showed 75-95% purity across hundreds of recipes (i.e. of every
    recipe carrying that variety_tag_id, 75-95% were also in the one matching Albertsons
    cookbook slug -- see COOKBOOK_SLUG_TO_VARIETY_TAG_ID).

  - The other 11 (1, 7, 8, 15, 18, 20, 22, 26, 27, 29, 30) have no Albertsons cookbook
    counterpart at all -- they're app-only groupings (e.g. "Frittatas", "Chops",
    "Main-course salads") that never surfaced as an Albertsons cookbook slug, so they
    couldn't be found the same statistical way. Confirmed instead by direct sampling:
    for each, several real recipes known (from the user checking the actual Mealime app)
    to belong to that app section were looked up in this library, and their own
    variety_tag_ids read directly -- e.g. every "Frittata"-named recipe checked shares
    id 7, every checked recipe from the app's "Chops" section shares id 15, etc. Several
    of these (id 20 "Main-course salads", id 26 "Stuffed vegetables", id 8 "Fritters &
    cakes") turned out to group by DISH CONCEPT rather than by name -- e.g. id 26 covers
    any dish where a vegetable is used as an edible filled/topped vessel (stuffed
    portobellos, but also "Portobello Mushroom Tuna Melts", "...Egg Bakes", and
    "...Pizzas", none of which say "stuffed" in their own name) -- confirmed across a
    dozen+ directly-checked recipes per id, not just a couple.

Two related candidate fields did NOT hold up under the same analysis and are
intentionally NOT included anywhere in this project: recipe_category_id is dominated by
just 2-3 values everywhere (no real correlation to any specific category), and
cuisine_tag_ids' top value showed up as the "top" pick across totally unrelated
categories too (a generic/default tag, not a real cuisine).

COOKBOOK_SLUG_TO_VARIETY_TAG_ID is the same mapping keyed by Albertsons' own cookbook
slug instead of the numeric id -- used to backfill variety_tag_ids on Albertsons-native
recipes, whose raw v2/recipe API response never includes this field at all (unlike
genuine Mealime data, which gets it straight from the site). It ALSO covers the Albertsons
cookbooks that have no Mealime counterpart (dessert, breakfast, snacks, ...): those get ids of
our own, 35-43 (ALBERTSONS_VARIETY_TAGS), so a cookbook label and a variety tag are a
single system. albertsons_cookbook_slugs_sync.py applies this mapping to EVERY recipe carrying
cookbook_slugs -- genuine Mealime ones too, adding any variety id the cookbook implies that the
record lacks (it never removes one); recipe_export.py then exports variety_tag_ids as Norish tags.

"kids-recipe-hub" is an Albertsons-side curation flag (recipes it's chosen to surface as
kid-friendly), not one of Mealime's own variety_tag_ids. It now gets an id of our own, 43
("Kid friendly"), like the other Albertsons-only cookbooks, so it exports as a variety tag too.
The older kid_friendly boolean is kept alongside it (is_kid_friendly() checks cookbook_slugs
membership directly), and albertsons_cookbook_slugs_sync.py adds 43 for either signal.
"""

MENU_TYPES = {
    -1: "All Menus",
    1: "Classic",
    2: "Vegetarian",
    3: "Flexitarian",
    4: "Pescatarian",
    5: "Paleo",
    6: "Vegan",
    7: "Low Carb",
    8: "Keto",
}

RESTRICTIONS = {
    1: "Gluten",
    2: "Dairy",
    3: "Fish",
    4: "Shellfish",
    5: "Peanut",
    6: "Tree Nut",
    9: "Soy",
    10: "Nightshade",
    11: "Egg",
    12: "Sesame",
    13: "Mustard",
    14: "Sulfite",
}


# Ids 1..33 are Mealime's own (see the module docstring); 34 is unused. Ids 35..43 are NOT Mealime's:
# they're ours, for Albertsons cookbooks that go beyond Mealime's taxonomy (dessert, breakfast,
# ...), so a cookbook label and a variety tag are one thing and export the same way. Mealime isn't
# expected to add any more variety tags, so they simply continue its numbering.
ALBERTSONS_VARIETY_TAGS = {
    35: "Dessert",
    36: "Breakfast",
    37: "Snacks",
    38: "Super simple",
    39: "Mediterranean",
    40: "Set it & forget it",
    41: "Under 15 minutes",
    42: "Sports fan favorites",
    43: "Kid friendly",   # the kids-recipe-hub cookbook; the kid_friendly boolean is kept alongside it
}

VARIETY_TAGS = {
    1: "Pasta & pizza",
    2: "Soups, stews, & chilis",
    4: "Burgers & sandwiches",
    6: "Curries",
    7: "Frittatas",
    8: "Fritters & cakes",
    11: "Stir fries",
    12: "Tacos & quesadillas",
    14: "Steaks",
    15: "Chops",
    18: "Fried rice & noodles",
    20: "Main-course salads",
    22: "BBQ & Grilling",
    24: "Baked",
    25: "Bowls",
    26: "Stuffed vegetables",
    27: "Wraps",
    29: "Low-carb \"pastas\"",
    30: "Wings & tenders",
    32: "Skillets & sautés",
    33: "Pan-fried",
    **ALBERTSONS_VARIETY_TAGS,
}

COOKBOOK_SLUG_TO_VARIETY_TAG_ID = {
    # Mealime's own variety tags (cross-referenced; see the docstring)
    "soups-stews-chilis": 2,
    "burgers-sandwiches": 4,
    "curries": 6,
    "stir-fries": 11,
    "tacos-quesadillas": 12,
    "steaks": 14,
    "baked": 24,
    "bowls": 25,
    "skillets-sautes": 32,
    "pan-fried": 33,
    # Albertsons-only cookbooks, with ids of our own (ALBERTSONS_VARIETY_TAGS). Deliberately NOT
    # mapped: popular, recently-created and meals-with-deals -- promotional lists that change over
    # time and don't describe a dish.
    "dessert": 35,
    "breakfast": 36,
    "snacks": 37,
    "super-simple": 38,
    "mediterranean-inspired": 39,
    "set-it-forget-it": 40,
    "dinners-under-15-minutes": 41,
    "sports-fan-favorites": 42,
    "kids-recipe-hub": 43,
}


def variety_tag_ids_from_cookbook_slugs(cookbook_slugs):
    return sorted({
        COOKBOOK_SLUG_TO_VARIETY_TAG_ID[slug]
        for slug in cookbook_slugs
        if slug in COOKBOOK_SLUG_TO_VARIETY_TAG_ID
    })


KIDS_RECIPE_HUB_SLUG = "kids-recipe-hub"


def is_kid_friendly(cookbook_slugs) -> bool:
    return KIDS_RECIPE_HUB_SLUG in cookbook_slugs


def decode_types(type_ids):
    return [MENU_TYPES.get(i, f"unknown({i})") for i in type_ids]


def decode_restrictions(restriction_ids):
    return [RESTRICTIONS.get(i, f"unknown({i})") for i in restriction_ids]
