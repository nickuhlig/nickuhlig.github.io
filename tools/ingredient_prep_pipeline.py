"""Prep split at import: the Serious Eats prep rounds 1-3 as one pass over a new US/Metric record pair, so imported recipes
come out like the processed library (the one-off scripts that ran the rounds over the library are in
serious_eats_work/prep_rounds/).

    prep_records(us, metric, master) -> (us, metric)

  round 1  comma tails that are prep or state ("onion, finely chopped", "butter, at room temperature") -> "prep"
  round 2  lemon/lime juice -> a fruit count + weigh_parts (3 tbsp per lemon, 2 per lime, rounded up to at least ½);
           zest with a count -> fruit count, merged into the juice line of the same fruit; leading prep words
           ("finely grated Parmigiano-Reggiano" -> "Parmesan cheese" + "finely grated"); hard-boiled eggs; fresh herbs
           de-stemmed in the sentence
  round 3  prep with no comma ("garlic chopped fine", "fresh peeled ginger"); standard names ginger root / fresh mint /
           avocado; peeled-and-deveined shrimp are bought that way ("raw peeled shrimp, fresh or frozen")
then one Prep step first: a sentence per line with prep, and "qty name" callouts.
Decisions come from the US record; the Metric twin (same lines, same order) gets the same changes, keeping its own
amounts in weigh_parts. Lines without an ingredient id (sub-recipes) are left alone. master: the ingredient_master
"ingredients" dict, for the "sold that way" check (crumbled feta cheese keeps "crumbled")."""

import collections
import copy
import math
import re

import ingredient_lead_prep as lp
import ingredient_prep as ip

FRUIT_ID = {"lemon": 192, "lime": 228, "orange": 232, "grapefruit": 222}
JUICE_ID = {1480: "lemon", 1421: "lime"}
ZEST_ID = 2346
PARMESAN, EGGS, GINGER, MINT, AVOCADO, SHRIMP, COOKED_SHRIMP = 120, 121, 221, 243, 183, 1811, 1128
YIELD = {"lemon": 3.0, "lime": 2.0}                      # tbsp of juice per fruit
FR = {"½": .5, "¼": .25, "¾": .75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": .125}
TBSP = {"tbsp": 1, "tsp": 1 / 3, "cup": 16, "cups": 16, "oz": 2, "fl oz": 2, "dash": 1 / 48, "dashes": 1 / 48}
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "a": 1, "an": 1, "half a": .5, "half an": .5, "half of a": .5,
           "half of an": .5}
SIZE = r"(?:whole |medium |large |small |juicy |ripe )*"
EGG = re.compile(r"^(?:large\s+)?(hard|soft)[- ]?(boiled|cooked|steamed)\s+(eggs?)\b(.*)$", re.I)
EGG_VERB = {"hard-boiled": "Hard-boil", "soft-boiled": "Soft-boil", "hard-cooked": "Hard-cook", "soft-cooked": "Soft-cook",
            "hard-steamed": "Hard-steam", "soft-steamed": "Soft-steam"}

# round 3: prep written with no comma
NC_MOVE = {"chopped", "minced", "grated", "sliced", "diced", "peeled", "picked", "crumbled", "melted", "shredded", "drained",
           "rinsed", "julienned", "cubed", "torn", "halved", "quartered", "trimmed", "mashed", "beaten", "softened", "cored",
           "stemmed", "seeded", "smashed", "pureed", "shaved", "deveined", "cleaned", "scrubbed", "crushed", "cooled", "thawed",
           "separated", "washed", "warmed", "soaked", "zested", "removed", "skinned", "ground"}
NC_STATE = {"toasted", "roasted", "cooked", "boiled", "chilled", "reserved", "discarded", "pressed", "sifted", "blanched",
            "salted", "cut", "squeezed", "pitted", "shelled"}
NC_ADV = {"finely", "thinly", "roughly", "coarsely", "lightly", "very", "freshly", "well", "gently", "fully"}
NC_AFTER = {"fine", "thin", "thinly", "slightly", "lightly", "well", "finely", "roughly"}
NC_AFTER_FIX = {"fine": "finely", "thin": "thinly", "thinly": "thinly", "lightly": "lightly"}
NC_PREPOS = {"in", "into", "with", "of", "until", "for", "through", "on", "from", "to"}
NC_PARTS = {"cores", "core", "ends", "stems", "skin", "tails"}
NC_KEEP_BEFORE = {"frozen", "canned", "jarred", "packaged", "thick", "hot", "unsweetened", "toasted", "plain", "soft", "leftover",
                  "black", "oil-cured", "ground", "white", "sliced", "warm", "chinese", "barspoon"}
NC_PRODUCT = [("sliced", "almonds"), ("grated", "coconut"), ("shredded", "coconut"), ("crushed", "crumbs"), ("ground", ""),
              ("peeled", "tomatoes"), ("picked", "meat"), ("crushed", "paste"), ("shredded", "skin"), ("cleaned", "snapper"),
              ("separated", "joints"), ("peeled", "chestnuts")]
NC_NUTS = re.compile(r"pistachio|peanut|almond|walnut|\bnuts?\b|hazelnut|olive|cherr|prune|date|apricot", re.I)
# bought that way (the user's rulings on the Serious Eats lines): deli meats, nuts, olives, chile powder, frozen edamame
NC_KEEP = re.compile(r"\b(sliced|cooked)\b.*\b(ham|turkey)\b|\b(skinned|pitted|shelled|toasted|roasted)\b.*\b(almonds?|hazelnuts?|"
                     r"olives?|pistachios?|peanuts?|edamame)\b|\b(almonds?|hazelnuts?|olives?)\b.*\b(skinned|pitted)\b|"
                     r"\bcrushed\b.*\b(chile|chili|pepper)\s+(powder|flakes)\b|\breserved from\b", re.I)
NC_HEAD = re.compile(r"\s(" + "|".join(sorted(NC_MOVE | NC_STATE | {"patted", "pushed"}, key=len, reverse=True)) + r")\b", re.I)
SHRIMP_SIZES = r"(?:small|medium|large|extra-large|jumbo|colossal|\d+/\d+)"


# --- small helpers -----------------------------------------------------------------------------------------------------

def _num(s):
    s = s.strip().lower()
    if s in WORDNUM:
        return WORDNUM[s]
    tot = 0
    for p in s.split():
        if p in FR:
            tot += FR[p]
        elif re.fullmatch(r"\d+/\d+", p):
            a, b = p.split("/")
            tot += int(a) / int(b)
        elif re.fullmatch(r"\d+(\.\d+)?", p):
            tot += float(p)
        elif p[:-1].isdigit() and p[-1] in FR:
            tot += int(p[:-1]) + FR[p[-1]]
        else:
            return None
    return tot or None


def _vol_tbsp(q):
    q = re.sub(r"\(.*?\)", "", q or "").strip()
    m = re.fullmatch(r"([\d½¼¾⅓⅔⅛/ .]+?)(?:\s*to\s*([\d½¼¾⅓⅔⅛/ .]+?))?\s*(tbsp|tsp|cups?|fl oz|oz|dashes|dash)", q)
    if not m:
        return None
    hi = _num(m.group(2) or m.group(1))
    return hi * TBSP[m.group(3)] if hi else None


def _round_up(n):
    """House rounding for fruit you buy: never below ½, halves up to 4, whole fruit after."""
    return max(.5, math.ceil(n * 2 - 1e-9) / 2) if n <= 4 else math.ceil(n - 1e-9)


def _fmt(x):
    w, f = int(x), x - int(x)
    return (f"{w} ½" if f else str(w)) if w else "½"


def _from_count(text, fruit):
    """'from about 3 lemons' / 'from 1/2 of a large lemon' / 'from 5 to 6 limes' -> 3 / .5 / 6 (the upper end)."""
    m = re.search(rf"\bfrom (?:about |roughly |approximately )?((?:half (?:of )?an?)|one|two|three|four|an?|[\d½¼¾⅓⅔]+(?:/\d+)?)"
                  rf"(?:\s*(?:to|-|–)\s*([\d½¼¾⅓⅔]+(?:/\d+)?))?\s*(?:of an? |of )?{SIZE}([\w-]+ )?{fruit}s?\b", text, re.I)
    return _num(m.group(2) or m.group(1)) if m else None


def _fruit_name(fruit, n):
    return fruit + ("s" if n > 1 else "")


def _keep_tails(tail_text):
    """Usage/brand tails stay ('plus more to taste', 'divided'); 'from N lemons' and 'freshly squeezed' go."""
    out = []
    for p in ip.split_top("x" + tail_text)[1:]:
        if re.match(r"^(from|squeezed from|juiced from|zested from|finely grated from|grated from)\b|"
                    r"^(freshly |fresh )?squeezed$|^fresh$|^finely grated$|^grated$", p, re.I):
            continue
        out.append(re.sub(r"\s*\bfrom (?:about )?\S+ (?:\w+ )?(lemon|lime|orange)s?\b", "", p, flags=re.I).strip())
    return [p for p in out if p]


def _cap_like(src, s):
    return s[:1].upper() + s[1:] if src[:1].isupper() else s


def _skip(g):
    return g.get("ingredient_id") is None or g.get("sub_recipe_id") is not None


def _sentence(name, prep):
    """The Prep-step sentence for a line's name + prep."""
    s = lp.line_sentence(name, prep.replace(", and ", " and ").replace(", ", " and "))
    if not s:
        item, groups = ip.prep_groups(f"{name}, {prep}")
        s = " ".join(x for x in (ip.to_sentence(item, t) for k, t, o in groups if k in ("prep", "state")) if x) or None
    return s.replace("the ginger root", "the ginger") if s else s


def _join_prep(*parts):
    return ", ".join(p for p in parts if p) or None


# --- round 1: comma tails ----------------------------------------------------------------------------------------------

def _round1(g):
    """-> (name, prep) with prep/state comma tails moved out of the name."""
    item, groups = ip.prep_groups(g["name"])
    moving = [(t, o) for k, t, o in groups if k in ("prep", "state")]
    if not moving or not all(ip.to_sentence(item, t) for t, _ in moving):
        return g["name"], g.get("prep")
    name = ", ".join([item] + [o for k, t, o in groups if k not in ("prep", "state")])
    return name, _join_prep(g.get("prep"), ", ".join(o for _, o in moving))


# --- round 2: citrus, leading words, eggs, herbs, Parmesan -------------------------------------------------------------

def _citrus(ings):
    """-> {index: change}, merged-away indexes."""
    plan, gone, juice_lines = {}, set(), collections.defaultdict(list)
    for i, g in enumerate(ings):
        m = re.match(r"^(?:freshly[- ]squeezed |fresh[- ]squeezed |fresh |freshly )?(lemon|lime) juice\b(.*)$", g["name"], re.I)
        if _skip(g) or not m or g["ingredient_id"] not in JUICE_ID or "wedges" in (g.get("prep") or ""):
            continue
        fruit, tail = m.group(1).lower(), m.group(2)
        q = (g.get("quantity") or "").strip()
        v = _vol_tbsp(q)
        if v is not None:
            n = _round_up(v / YIELD[fruit])
        elif not q:
            n = _round_up(_from_count(f"{g['name']} {g.get('prep') or ''}", fruit) or 1)
        else:
            continue
        tails = _keep_tails(tail)
        if re.search(r"\bto taste\b", tail, re.I) and not any("taste" in t for t in tails):
            tails.append("to taste")
        plan[i] = dict(kind="juice", fruit=fruit, n=n, tails=tails, zest_here=bool(re.search(r"\bzest\b", tail, re.I)))
        juice_lines[fruit].append(i)
    for i, g in enumerate(ings):
        full = g["name"] + (f", {g['prep']}" if g.get("prep") else "")
        m = re.search(r"\b(lemon|lime|orange|grapefruit)s?\b", full, re.I)
        if _skip(g) or g["ingredient_id"] != ZEST_ID or not m or not re.search(r"\bzest\b", full, re.I):
            continue
        fruit = m.group(1).lower()
        own = _from_count(full, fruit)
        jl = juice_lines.get(fruit, [])
        jfrom = [_from_count(f"{ings[j]['name']} {ings[j].get('prep') or ''}", fruit) for j in jl]
        if jl:
            if own is None and not any(jfrom):
                continue                                    # no count anywhere: the zest line stays as it is
            k = next((k for k, c in enumerate(jfrom) if c), 0)
            p = plan[jl[k]]
            p["n"] = max(p["n"], _round_up(own or 0), _round_up(jfrom[k] or 0))
            p.setdefault("zest_from", []).append(i)          # a recipe can zest the same lemons in two places
            gone.add(i)
        elif own is not None:
            plan[i] = dict(kind="zest", fruit=fruit, n=_round_up(own), tails=_keep_tails(re.sub(r"^.*?\bzest\b", "", g["name"], flags=re.I)))
    out = {}
    for i, p in plan.items():
        g = ings[i]
        fruit_n = _fruit_name(p["fruit"], p["n"])
        if p["kind"] == "juice":
            zested = "zest_from" in p or p["zest_here"]
            prep = "zested and juiced" if zested else "juiced"
            sent = f"{'Zest and juice' if zested else 'Juice'} the {fruit_n}."
            parts = [(g["ingredient_id"], g["name"], i)] + [(ZEST_ID, ings[z]["name"], z) for z in p.get("zest_from", [])]
        else:
            prep, sent, parts = "zested", f"Zest the {fruit_n}.", [(ZEST_ID, g["name"], i)]
        out[i] = dict(name=_cap_like(g["name"], ", ".join([fruit_n] + p["tails"])), prep=prep, sentence=sent,
                      id=FRUIT_ID[p["fruit"]], quantity=_fmt(p["n"]), parts=parts)
    return out, gone


def _round2_line(g, master):
    """-> change or None for one line (not citrus)."""
    name, old = g["name"], g.get("prep") or ""
    frag = re.sub(r",\s*(and\s+)?", " and ", old)          # 'trimmed, peeled' (split off by the aligner) -> one fragment
    if (name, old) in lp.SPECIAL:
        n2, prep, sent = lp.SPECIAL[(name, old)]
        return dict(name=n2, prep=prep, sentence=sent)
    iid = g["ingredient_id"]
    if iid == PARMESAN:
        name = lp.standard_name(name)
    lead_words = None
    item, lead = (name, None) if NC_KEEP.search(ip.split_top(name)[0]) else lp.lead_split(name, master.get(str(iid), {}).get("name", ""))
    if lead:
        name, lead_words = item, lead
        if iid == PARMESAN:
            name = _cap_like(g["name"], lp.standard_name(name))
    e = EGG.match(lp.PACKED.sub("", name)) if iid == EGGS else None
    if e:                                                  # boiling comes first: "Hard-boil the eggs, then peel and halve them."
        how = f"{e.group(1).lower()}-{e.group(2).lower()}"
        name = _cap_like(g["name"], e.group(3).lower() + e.group(4))
        obj = e.group(3).lower()
        rest = lp.combine(lead_words or "", frag)
        then = ip.to_sentence(obj, rest) if rest else None
        if rest and not then:
            return None
        sent = f"{EGG_VERB[how]} the {obj}" + (", then " + then[0].lower() + then[1:].replace(f" the {obj}", " them" if obj.endswith("s") else " it", 1) if then else ".")
        return dict(name=name, prep=_join_prep(how, lead_words, old), sentence=sent)
    if lead_words:
        sent = lp.line_sentence(name, lp.combine(lead_words, frag))
        return dict(name=name, prep=_join_prep(lead_words, old), sentence=sent) if sent else None
    if old and lp.herb_of(lp.core(name)):
        sent = lp.line_sentence(name, frag)
        if sent and sent.startswith("Pick"):
            return dict(name=name, prep=old, sentence=sent)
    if name != g["name"]:
        return dict(name=name, prep=old or None, sentence=None)
    return None


# --- round 3: no comma, standard names, shrimp -------------------------------------------------------------------------

def _isp(w):
    return w.lower() in NC_MOVE or w.lower() in NC_STATE


def _fix_order(pp):
    """'chopped fine' -> 'finely chopped', 'beaten lightly with water' -> 'lightly beaten with water'."""
    m = re.match(r"^(\w+ed)\s+(fine|thin|thinly|lightly)\b(.*)$", pp)
    return f"{NC_AFTER_FIX[m.group(2)]} {m.group(1)}{m.group(3)}" if m else pp


def _nocomma(name, prep, master_name):
    """-> (name, prep) with prep moved out of the name part before any comma, or None."""
    head = ip.split_top(name)[0]
    mm = NC_HEAD.search(head)
    if not mm or not head[:mm.start()].strip() or re.search(rf"\b{mm.group(1)}\b", master_name, re.I) or NC_KEEP.search(head):
        return None
    tail = name[len(head):]
    t = head.split()
    if t and t[0].lower() == "and" and len(t) > 2 and _isp(t[1]) and prep:      # 'and deveined shrimp' | 'peeled'
        first, _, rest = prep.partition(", ")
        return " ".join(t[2:]) + tail, f"{first} and {t[1]}" + (f", {rest}" if rest else "")
    k = next((i for i, w in enumerate(t) if _isp(w) or (w.lower() in NC_ADV and i + 1 < len(t) and _isp(t[i + 1]))), None)
    if k is None:
        return None
    j = k
    while j < len(t) and (_isp(t[j]) or t[j].lower() in NC_ADV or (t[j].lower() == "and" and j + 1 < len(t) and (_isp(t[j + 1]) or t[j + 1].lower() in NC_ADV))):
        j += 1
    run, before, after = t[k:j], t[:k], t[j:]
    parts = [w.lower() for w in run if _isp(w)]
    if before and (not after or after[0].lower() in NC_PREPOS or after[0].lower() in NC_AFTER):   # 'garlic finely chopped'
        if before[-1].lower() in NC_PARTS:                                                         # 'red apples cores removed'
            run = [before.pop()] + run
        pp = _fix_order(" ".join(run + after))
        if prep and prep.lower() != pp.lower():
            pp = f"{pp}, {prep}"
        return " ".join(before) + tail, pp
    if not after:
        return None
    noun = " ".join(after).lower()
    movers = set(NC_MOVE) | ({"shelled", "pitted"} if not NC_NUTS.search(noun) else set())
    if len(run) > 2 and run[0].lower() in ("cooked", "blanched") and run[1].lower() == "and":
        movers |= {run[0].lower()}                                                                  # 'cooked and drained tapioca'
    if (not any(p in movers for p in parts) or (before and before[-1].lower() in NC_KEEP_BEFORE)
            or any(p == a and (n == "" or n in noun) for a, n in NC_PRODUCT for p in parts)
            or re.search(r"\b(plus|slices|rounds|can)\b", head, re.I)):
        return None
    keep_states = [w for w in run if w.lower() in NC_STATE and w.lower() not in movers]          # 'toasted and chopped almonds'
    lead = " ".join(w for w in run if w not in keep_states and (w.lower() in movers or w.lower() in NC_ADV or w.lower() == "and"))
    lead = re.sub(r"^(and\s+)+|(\s+and)+$", "", re.sub(r"\band(\s+and)+\b", "and", lead)).strip()
    if not lead:
        return None
    return re.sub(r"\s+,", ",", " ".join(before + keep_states + after) + tail).strip(), _join_prep(lead, prep)


def _standard_name(iid, name, prep):
    """ginger root / fresh mint / avocado for the item part of the name -> (name, prep)."""
    head = ip.split_top(name)[0]
    tail, low = name[len(head):], head.lower()
    if iid == GINGER and re.search(r"\bginger\b", low) and not re.search(r"juice|paste|powder|ground|candied|pickled", low):
        rest = [w for w in head.split() if w.lower() not in ("fresh", "peeled", "ginger", "root", "knob", "piece", "pieces", "of")]
        if re.search(r"\bpeeled\b", low) and not re.search(r"\bpeel", prep or "", re.I):
            prep = _join_prep("peeled", prep)
        return _cap_like(name, " ".join(rest + ["ginger root"]) + tail), prep
    if iid == MINT and re.search(r"\bmint\b", low) and not re.search(r"chocolate|spearmint|sprigs?|peppermint", low):
        packed = lp.PACKED.match(head)
        if re.search(r"\bchiffonade\b", low):
            prep = _join_prep("cut into chiffonade", prep)
        elif re.search(r"\bleaves\b", low) and not prep:
            prep = "leaves picked"
        return _cap_like(name, (packed.group(0).lower() if packed else "") + "fresh mint" + tail), prep
    if iid == AVOCADO and re.search(r"\bavocados?\b", low):
        rest = [w for w in head.split() if w.lower() not in ("hass", "haas", "whole", "avocado", "avocados")]
        return _cap_like(name, " ".join(rest + ["avocados" if re.search(r"avocados\b", low) else "avocado"]) + tail), prep
    return name, prep


def _shrimp(iid, name, prep):
    """Peeled-and-deveined shrimp are bought that way -> (name, prep) or None."""
    full = f"{name}, {prep or ''}".lower()
    if re.match(r"^(raw|cooked) peeled\b", name, re.I):
        return None
    if iid == COOKED_SHRIMP and re.search(r"peeled|shelled", full):
        return _cap_like(name, "cooked peeled shrimp"), None
    if iid != SHRIMP or not re.search(r"\b(peeled|shelled|cleaned)\b", full) or re.search(r"head-on|shell-on|shells? (reserved|removed and reserved)", full):
        return None
    size = re.search(rf"\b{SHRIMP_SIZES}\b", name, re.I)
    left = re.sub(r"\b(peeled|shelled|cleaned|deveined|de-veined|if necessary|tails? (left )?on|tail on)\b", "", prep or "", flags=re.I)
    left = re.sub(r"^and\s+", "", re.sub(r"^[\s,and]+|[\s,]+$", "", re.sub(r"(,\s*)+", ", ", re.sub(r"\band\s*(,|$)", r"\1", left))).strip())
    new = ("raw peeled " + (size.group(0).lower() + " " if size else "") + "shrimp, fresh or frozen"
           + (", tail on" if re.search(r"\btails? (on|left on)\b", full) else ""))
    return _cap_like(name, new), left or None


# --- the pass ----------------------------------------------------------------------------------------------------------

def _apply(rec, plan, gone):
    """Apply {index: change} to rec (US or Metric twin, same line order). A change's own sentence rides along in the
    line's "_sentence" until the Prep step is built; a later change without one drops it."""
    ings = rec["ingredients"]
    out = []
    for i, g in enumerate(ings):
        if i in gone:
            continue
        c = plan.get(i)
        if c:
            g = dict(g)
            if "parts" in c:
                g["weigh_parts"] = [{"ingredient_id": pid, "quantity": ings[pi].get("weigh_as") or ings[pi].get("quantity") or "",
                                     "name": pname} for pid, pname, pi in c["parts"]]
                g.pop("weigh_as", None)
            if "id" in c:
                g["ingredient_id"] = c["id"]
            if "quantity" in c:
                g["quantity"] = c["quantity"]
            g["name"] = c["name"]
            if c.get("prep"):
                g["prep"] = c["prep"]
            else:
                g.pop("prep", None)
            g.pop("_sentence", None)
            if c.get("sentence"):
                g["_sentence"] = c["sentence"]
        out.append(g)
    rec["ingredients"] = out


def _plan_all(us, master, line_fn):
    plan = {}
    for i, g in enumerate(us["ingredients"]):
        if not _skip(g):
            c = line_fn(g, master)
            if c:
                plan[i] = c
    return plan


def prep_records(us: dict, metric: dict | None, master: dict):
    """Rounds 1-3 on a US record and its Metric twin (copies are returned; the inputs are left as they are)."""
    us = copy.deepcopy(us)
    metric = copy.deepcopy(metric) if metric else None
    for v in master.values():                                         # proper nouns keep their capital in sentences
        lp.PROPER.update(w for w in re.findall(r"\b[A-Z][\w-]+", v.get("name") or ""))
    recs = [r for r in (us, metric) if r]
    if metric and len(metric["ingredients"]) != len(us["ingredients"]):
        raise ValueError(f"{us.get('id')}: US and Metric records list different ingredients")
    def run(plan, gone=frozenset()):
        for rec in recs:
            _apply(rec, plan, gone)

    def r1(g, _):
        name, prep = _round1(g)
        return dict(name=name, prep=prep, sentence=None) if (name, prep) != (g["name"], g.get("prep")) else None

    def r3(g, master):
        name, prep, iid = g["name"], g.get("prep"), g["ingredient_id"]
        sentence = None
        nc = _nocomma(name, prep, master.get(str(iid), {}).get("name", ""))
        if nc:
            name, prep = nc
        name, prep = _standard_name(iid, name, prep)
        s = _shrimp(iid, name, prep)
        if s:
            name, prep = s
        if (name, prep) == (g["name"], g.get("prep")):
            return None
        return dict(name=name, prep=prep, sentence=sentence)

    run(_plan_all(us, master, r1))
    citrus, gone = _citrus(us["ingredients"])
    rest = {i: c for i, c in _plan_all(us, master, _round2_line).items() if i not in citrus and i not in gone}
    run({**citrus, **rest}, gone)
    run(_plan_all(us, master, r3))
    for rec in recs:
        lines = [g for g in rec["ingredients"] if g.get("prep") and not _skip(g)]
        steps = [s for s in rec.get("instructions") or [] if not (isinstance(s, dict) and s.get("kind") == "prep")]
        sents = [g.pop("_sentence", None) or _sentence(g["name"], g["prep"]) for g in lines]
        for g in rec["ingredients"]:
            g.pop("_sentence", None)
        if lines:
            text = " ".join(x for x in sents if x)
            calls = "\n".join(" ".join(x for x in ((g.get("quantity") or "").strip(), g["name"]) if x) for g in lines)
            steps = [{"id": 0, "kind": "prep", "primary_message": text, "secondary_message": calls}] + steps
        rec["instructions"] = steps
    return us, metric
