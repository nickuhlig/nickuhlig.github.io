"""Compound amounts from imported pages ("1 tbsp plus 2 tsp", "¼ cup minus 1 tsp", "1 cup (8 oz) plus 2 tbsp") as one
amount, by the user's rules (these lines only):
  under 1 cup   a tsp part -> tsp (5 tsp, 11 tsp), but over ¼ cup -> tbsp to the nearest ½;
                cup + tbsp only -> a clean ⅛ cup when it is one, else tbsp to the nearest ½
  over 1 cup    US: liquids in fl oz to the nearest ½, everything else cups to the nearest ⅛;
                Metric: mL to the nearest 5
single_amount() says whether the result is exact; when it isn't, the caller keeps the page's amount in "weigh_as"
so nutrition stays exact (recipe_nutrition adds the parts of a compound amount)."""

import re
from fractions import Fraction as F

GLYPH = {F(1, 8): "⅛", F(1, 4): "¼", F(1, 3): "⅓", F(3, 8): "⅜", F(1, 2): "½", F(5, 8): "⅝", F(2, 3): "⅔",
         F(3, 4): "¾", F(7, 8): "⅞"}
GLYPH_VALUE = {v: k for k, v in GLYPH.items()}
TSP = {"cup": 48, "cups": 48, "tbsp": 3, "tsp": 1}
TSP_ML = 4.92892
NUM = r"(\d+\s+[½¼¾⅓⅔⅛⅜⅝⅞]|\d+/\d+|\d+|[½¼¾⅓⅔⅛⅜⅝⅞])"
FIRST = re.compile(rf"^{NUM}\s*(cups?|tbsp|tsp)(?:\s*\([^)]*\))?", re.I)
MORE = re.compile(rf"^(plus|minus)\s+{NUM}\s*(cups?|tbsp|tsp)\b", re.I)
COMPOUND = re.compile(r"\s(plus|minus)\s+[\d½¼¾⅓⅔⅛⅜⅝⅞]", re.I)
LIQUID = re.compile(r"\b(water|milk|buttermilk|cream|half-and-half|oil|wine|stock|broth|juice|vinegar|sake|mirin|beer|"
                    r"liqueur|rum|vodka|gin|tequila|brandy|bourbon|whiskey|coffee|tea|soy sauce|fish sauce|coconut milk|"
                    r"dashi|syrup|honey|molasses|egg whites)\b", re.I)
# page amounts the rules can't read, ruled by the user (each recipe's steps already give the portions separately)
SPECIAL = {"4 oz plus 1 tbsp": ("9 tbsp", True), "1 ½ quart plus 2 cups": ("2 quarts", True),
           "⅓ tsp plus ½ tsp": ("1 tsp", False), "1 cup minus 1 1/2 tbsp": ("⅞ cup", False)}


def _num(s):
    tot = F(0)
    for p in s.split():
        if p in GLYPH_VALUE:
            tot += GLYPH_VALUE[p]
        elif "/" in p:
            a, b = p.split("/")
            tot += F(int(a), int(b))
        else:
            tot += int(p)
    return tot


def total_tsp(q):
    """Total of a cup/tbsp/tsp compound amount in tsp, or None."""
    s = q.strip()
    m = FIRST.match(s)
    if not m:
        return None
    total = _num(m.group(1)) * TSP[m.group(2).lower()]
    s = s[m.end():].strip()
    while s:
        m = MORE.match(s)
        if not m:
            return None
        v = _num(m.group(2)) * TSP[m.group(3).lower()]
        total += v if m.group(1).lower() == "plus" else -v
        s = s[m.end():].strip()
    return total


def fmt(x):
    w = x.numerator // x.denominator
    f = x - w
    if f == 0:
        return str(w)
    return f"{w} {GLYPH[f]}" if w else GLYPH[f]


def _nearest(x, step):
    return F(round(x / step)) * step


def single_amount(q, name, units):
    """-> (quantity, exact) for a compound amount, or None when q isn't one / can't be read."""
    if not COMPOUND.search(q or ""):
        return None
    if q in SPECIAL:
        return SPECIAL[q]
    tot = total_tsp(q)
    if tot is None:
        return None
    if tot < 48:                                                   # under a cup
        if re.search(r"\btsp\b", q):
            if tot <= 12:
                x = _nearest(tot, F(1, 8))
                return f"{fmt(x)} tsp", x == tot
            x = _nearest(tot / 3, F(1, 2))
            return f"{fmt(x)} tbsp", x * 3 == tot
        if (tot / 6).denominator == 1:                             # a whole number of ⅛ cups
            return f"{fmt(tot / 48)} cup", True
        x = _nearest(tot / 3, F(1, 2))                             # '⅓ cup plus 2 tbsp' -> 7 ½ tbsp, not 7 ⅓
        return f"{fmt(x)} tbsp", x * 3 == tot
    if units == "Metric":
        return f"{5 * round(float(tot) * TSP_ML / 5)} ml", False
    if LIQUID.search(name or ""):
        x = _nearest(tot / 6, F(1, 2))                             # 6 tsp to the fl oz
        return f"{fmt(x)} fl oz", x * 6 == tot
    x = _nearest(tot / 48, F(1, 8))
    return f"{fmt(x)} {'cups' if x > 1 else 'cup'}", x * 48 == tot
