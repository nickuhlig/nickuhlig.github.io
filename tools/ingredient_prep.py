"""
Prep wording in imported ingredient names. Serious Eats writes the knife work and the state an ingredient
should be in into the ingredient line ("red onion, peeled and cut into 1-inch chunks", "eggs, at room
temperature"); Mealime keeps names plain and puts that in a step. This module splits a name into the item and
its comma fragments, classifies each fragment, and turns the prep/state ones into instructions:

    split_line("red bell peppers, stemmed, seeded, and roughly chopped")
        -> ("red bell peppers", [("prep", "stemmed and seeded and roughly chopped")])
    to_sentence("red bell peppers", "stemmed and seeded and roughly chopped")
        -> "Stem, seed, and roughly chop the red bell peppers."

Fragment kinds: prep and state (move to the recipe's Prep step); usage ("divided", "plus more to taste",
"for frying"), brand ("such as Aroy-D"), descriptor ("small", "canned in adobo", "not strained"), part ("green
parts only"), alternative ("or ghee"), source ("from 1 lemon") stay in the name. Anything it can't read returns
None, so nothing is guessed. Part phrases follow the cook: after removing or trimming a part the next verb is
about the item ("ends trimmed, cut into segments"), after picking or separating one it is about the part
("leaves picked and minced" -> "Pick and mince the leaves from the thyme").
"""
import re

ADVERBS = {"finely", "thinly", "roughly", "coarsely", "lightly", "very", "well", "freshly", "slightly", "gently", "firmly",
           "loosely", "evenly", "thickly", "neatly", "partially", "completely", "fully", "cleanly", "barely", "just",
           "deeply", "briefly", "quickly", "carefully", "tightly", "fine", "rough", "thin", "thick", "coarse", "then",
           "also", "each", "individually", "separately", "halfway", "lightly", "generously", "evenly", "fully", "crisply"}
ADV_FIX = {"fine": "finely", "rough": "roughly", "thin": "thinly", "thick": "thickly", "coarse": "coarsely"}
IRREGULAR = {"cut": "cut", "torn": "tear", "ground": "grind", "split": "split", "beaten": "beat", "shaken": "shake",
             "slit": "slit", "spun": "spin", "left": "leave", "brought": "bring", "kept": "keep", "frozen": "freeze",
             "shredded": "shred", "chopped": "chop", "trimmed": "trim", "stemmed": "stem", "patted": "pat", "scrubbed": "scrub",
             "pitted": "pit", "skinned": "skin", "snipped": "snip", "slivered": "sliver", "butterflied": "butterfly",
             "dried": "dry", "fried": "fry", "shelled": "shell", "julienned": "julienne", "zested": "zest", "broken": "break",
             "hard-boiled": "hard-boil", "soft-boiled": "soft-boil", "wrapped": "wrap", "rubbed": "rub", "scraped": "scrape",
             "dipped": "dip", "flipped": "flip", "stripped": "strip", "clipped": "clip", "chipped": "chip", "dotted": "dot",
             "knotted": "knot", "slit": "slit", "set": "set", "laid": "lay", "spread": "spread", "shed": "shed",
             "hulled": "hull", "tied": "tie", "untied": "untie", "boned": "bone", "deboned": "debone", "unpeeled": "leave unpeeled"}
VERBS_E = {"mince", "slice", "dice", "grate", "rinse", "juice", "squeeze", "crumble", "score", "shave", "cube", "halve",
           "separate", "core", "remove", "reserve", "discard", "pierce", "prick", "scrape", "cure", "pickle", "pare",
           "debone", "bone", "degrease", "julienne", "puree", "purée", "crumble", "dredge", "smoke", "bake", "massage", "drizzle",
           "squeeze", "seize", "temper", "use", "save", "measure", "size", "trace", "ice", "brine", "fine", "glaze",
           "segregate", "carve", "shape", "wedge", "devein", "tease", "toss", "loosen", "sauté", "saute", "charge",
           "hydrate", "rehydrate", "marinate", "macerate", "caramelize", "dissolve", "sliver", "pulse", "chiffonade",
           "rough-chop", "release", "rinse", "scale", "stone", "flake", "grease", "wipe"}


VOWELS = "aeiou"
UNSTRESSED = ("en", "er", "or", "on", "el", "it", "et", "al", "om", "ol", "ow", "ain", "ail", "eal", "ear", "oil", "ook",
              "ush", "ash", "ish", "ch", "sh", "th", "ss", "ck", "rk", "lk", "nk", "nt", "rt", "st", "ft", "lt", "pt", "ct",
              "xt", "nd", "rd", "ld", "mp", "lp", "sp", "rm", "rn", "wn", "ng", "lm", "ll", "ff", "rl", "x", "w", "y")
# product descriptions that look like prep: "chipotles, canned in adobo", "peanuts, roasted", "chilis, dried"
DESCRIPTIVE = {"canned", "jarred", "bottled", "packed", "roasted", "dried", "salted", "smoked", "cured", "aged", "dry-aged",
               "enriched", "bleached", "sweetened", "pasteurized", "ultra-pasteurized", "homogenized", "assorted",
               "preferred", "refined", "fortified", "flavored", "clarified", "twisted", "attached", "mixed", "prepared",
               "cooked", "pickled", "fermented", "sprouted", "candied", "condensed", "evaporated", "processed", "iodized",
               "unpeeled", "undrained", "uncooked", "unsalted", "unsweetened", "unbleached", "untrimmed", "unseasoned",
               "unflavored", "un-sliced", "unsliced", "unrefined", "unfiltered", "low-temperature-pasteurized",
               "non-homogenized", "herb-packed", "reduced-fat", "thinned", "cultured", "whipped"}
UN_STATE = {"unpeeled": "Leave the {o} unpeeled.", "undrained": "Don't drain the {o}.", "untrimmed": "Leave the {o} untrimmed.",
            "un-sliced": "Leave the {o} unsliced.", "unsliced": "Leave the {o} unsliced."}


def base_verb(w: str):
    w = w.lower().strip(".")
    if w in IRREGULAR:
        return IRREGULAR[w]
    if not w.endswith("ed") or len(w) < 5:
        return None
    if w[:-1] in VERBS_E:                      # minced -> mince
        return w[:-1]
    if w.endswith("ied"):
        return w[:-3] + "y"
    stem = w[:-2]
    if len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in "lszf":
        return stem[:-1]                        # chopped -> chop
    if stem.endswith(("c", "v", "z", "rg", "dg", "ns", "rs", "ls", "ps", "us", "ui", "ys")) or stem[-2:] in ("ui",) or \
            (stem.endswith("s") and len(stem) > 2 and stem[-2] in "ui"):
        return stem + "e"                       # sliced, microwaved, pulverized, purged, rinsed, bruised
    # one vowel then one consonant, not doubled in the -ed form: a silent e (wiped, poked, scaled, prepared,
    # refrigerated), unless the last syllable is unstressed (opened, softened, tempered, flavored, visited)
    if len(stem) >= 3 and stem[-1] not in VOWELS and stem[-2] in VOWELS and stem[-3] not in VOWELS \
            and not stem.endswith(UNSTRESSED):
        return stem + "e"
    if stem.endswith(("at", "it")) and len(stem) > 5 and stem[-3] not in VOWELS:
        return stem + "e"                       # separated, hydrated (but visited, limited stay)
    return stem


USAGE = re.compile(r"^(divided|divide$|plus\b|as needed|to taste|if needed|if necessary|more to taste|more as needed|for |"
                   r"optional|if desired|or more|more or less|if using|if making|reserved|to serve|to finish|to adjust|"
                   r"to mist|as desired|per |up to|use |enough for|depending|a pinch more|a little more)", re.I)
PART_ONLY = re.compile(r"(\bonly$|^tender stems$|^leaves and tender stems|^leaves$|^peel only|^bottom\b|"
                       r"^(pale )?(white|green|light green|dark green)\b.*\bparts?\b)", re.I)
ON_ITEM = re.compile(r"^(skin[- ]on|tails?[- ]on|head[- ]on|shell[- ]on|on the bone|in (their|its|the) (husks?|skins?|shells?|package)|"
                     r"still in\b|each about|a mixture|whole$)", re.I)
BRAND = re.compile(r"^(such as|like|preferably|ideally|we like|i like|any |homemade or|store-?bought or|recipe follows|"
                   r"see note|see notes|from (a |one )?(jar|can|box|bottle|package|tube))", re.I)
ALT = re.compile(r"^or\b", re.I)
SOURCE = re.compile(r"^(?:from|squeezed from|zested from|juiced from)\s+(.+)$", re.I)
DESCRIPTOR = re.compile(r"^(small|medium|large|extra[- ]large|jumbo|whole|fresh|lean|hot|cold-pressed|thin|thick|skin-on|"
                        r"skinless|bone-in|boneless|ripe|unripe|firm|soft|solid|creamy|canola|unsweetened|sweetened|"
                        r"salted|unsalted|full-fat|low-fat|plain|dry|wet|raw|smoked|unsmoked|mild|spicy|fine|coarse|"
                        r"not\b|at least|about|approximately|roughly \d|between|around|no more|with\b|including|without|"
                        r"in (a |large|small|chunks|pieces|one)|\d|1/|½|¼|¾|made with|packed in|in oil|in water|"
                        r"in brine|in syrup|in juice|in adobo|canned|jarred|bottled|organic|wild|farmed|dark|light|"
                        r"white|red|yellow|green|black|other|mixed|assorted|any color|any kind|any variety|if possible|"
                        r"number will vary|weight will vary|amount will vary|about \d|scant|heaping|generous)", re.I)
STATE = [  # (pattern, template); {o} object, {p} pronoun, {1} captured tail
    (r"^(?:left|let (?:sit|stand|rest)|sitting|set out|standing) at room temperature( for .+)?$", "Let the {o} sit at room temperature{1}."),
    (r"^(?:at |brought to |warmed to |cooled to |)room temperature$", "Bring the {o} to room temperature."),
    (r"^(?:straight from the fridge|cold|well[- ]chilled|chilled|very cold|ice[- ]cold|refrigerator[- ]cold|fridge[- ]cold)$", "Keep the {o} cold until you use {p}."),
    (r"^(?:kept )?frozen$", "Keep the {o} frozen until you use {p}."),
    (r"^(?:softened|soft|very soft|slightly softened)$", "Soften the {o}."),
    (r"^warm(?:ed)?$", "Warm the {o}."),
    (r"^(?:warm|warmed|heated|hot)\s*(?:to )?(?:about |approximately )?(\d.*°[FC].*)$", "Warm the {o} to {1}."),
    (r"^(?:about |approximately )?(\d+\s*°\s*[FC].*)$", "Bring the {o} to {1}."),
    (r"^crisply cooked$", "Cook the {o} until crisp."),
    (r"^(?:at )?(?:about |approximately )?(\d[\d\s\-to]*\s*[°º]\s*[FC].*)$", "Bring the {o} to {1}."),
    (r"^(?:lukewarm|tepid|barely warm)$", "Warm the {o} until lukewarm."),
    (r"^(?:pliable|soft|softened) but (?:still )?(?:cool|cold|firm)$", "Let the {o} soften slightly but keep {p} cool."),
    (r"^cold from the fridge$", "Keep the {o} cold until you use {p}."),
    (r"^well[- ]shaken$", "Shake the {o} well."),
    (r"^slightly cooler than room temperature$", "Let the {o} warm until just cooler than room temperature."),
    (r"^soft but (?:still )?cool$", "Let the {o} soften but keep {p} cool."),
]


def split_top(s: str, sep=","):
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch == sep and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [x for x in out if x]


def pronoun(noun: str) -> str:
    last = noun.split()[-1].lower() if noun else ""
    return "them" if last.endswith("s") and not last.endswith(("ss", "us", "molasses", "hummus", "couscous", "asparagus")) else "it"


def action(text: str, obj: str):
    """One action -> (verb phrase, object phrase, rest) or None.
    'thinly sliced on the bias' -> ('thinly slice', obj, 'on the bias');
    'stems removed' -> ('remove', 'the stems from the <obj>', ''); 'each cored' -> ('core', 'each <obj>', '')."""
    words = text.split()
    if not words:
        return None
    each = False
    if words[0].lower() == "each":
        each = True; words = words[1:]
    adv = []
    while words and words[0].lower() in ADVERBS and words[0].lower() != "each":
        w = words.pop(0).lower()
        if w not in ("then", "also"):
            adv.append(ADV_FIX.get(w, w))
    if not words:
        return None
    v = base_verb(words[0])
    if v:
        objp = (f"each {obj.rstrip('s') if obj.endswith('s') and not obj.endswith('ss') else obj}" if each else f"the {obj}")
        return (" ".join(adv + [v]), objp, " ".join(words[1:]))
    # "<part> <participle> ...": "stems removed", "leaves picked", "ends trimmed", "caps quartered", "skin removed and reserved"
    for i in range(1, min(7, len(words))):
        v = base_verb(words[i])
        if v and not any(base_verb(w) for w in words[:i]):
            pw = words[:i]
            while pw and pw[-1].lower() in ADVERBS:          # "caps finely chopped": the adverb goes with the verb
                adv.append(ADV_FIX.get(pw.pop().lower(), pw and "" or ""))
            adv = [a for a in adv if a] + [w.lower() for w in words[len(pw):i] if w.lower() in ADVERBS and w.lower() not in adv]
            if not pw:
                return None
            part = " ".join(pw)
            prep = "from" if v in ("remove", "discard", "pick", "strip", "scrape", "cut off", "peel") else "of"
            return (" ".join(adv + [v]), f"the {part} {prep} the {obj}", " ".join(words[i + 1:]))
    return None


def state_clause(item: str, frag: str):
    pr = pronoun(item)
    for pat, tmpl in STATE:
        m = re.match(pat, frag.strip(), re.I)
        if m:
            extra = (m.group(1) or "") if m.lastindex else ""
            return tmpl.replace("{o}", item).replace("{p}", pr).replace("{1}", extra.rstrip(".")).rstrip(".")
    return None


def is_action(text: str, obj: str) -> bool:
    return action(text, obj) is not None


def split_actions(frag: str, obj: str):
    """Split on 'and'/'then' only where a new action starts: 'cut at joints into drumettes and flats' stays whole,
    'husks and silk removed' becomes one part phrase, 'ends trimmed cut into ...' (a missing 'and') splits."""
    raw = [p for p in re.split(r"\s*,?\s*\b(and|then)\b\s*", frag) if p]
    pieces, joins = [raw[0]], []
    for i in range(1, len(raw), 2):
        pieces.append(raw[i + 1] if i + 1 < len(raw) else ""); joins.append(raw[i])
    out = [pieces[0]]
    for j, p in zip(joins, pieces[1:]):
        if not p:
            continue
        starts_action = is_action(p, obj) or state_clause(obj, p) is not None
        if starts_action and (is_action(out[-1], obj) or state_clause(obj, out[-1]) is not None):
            out.append(p)
        else:
            out[-1] = f"{out[-1]} {j} {p}"          # 'drumettes and flats', 'husks and silk removed'
    # a missing 'and': "ends trimmed cut into 1-inch segments"
    fixed = []
    for p in out:
        words = p.split()
        cut = next((k for k in range(2, len(words)) if base_verb(words[k]) and base_verb(words[k - 1])
                    and is_action(" ".join(words[k:]), obj)), None)
        fixed.extend([" ".join(words[:cut]), " ".join(words[cut:])] if cut else [p])
    return fixed


def to_sentence(item: str, fragment: str):
    """A prep/state fragment -> one sentence, or None when it can't be read."""
    obj, pr = item, pronoun(item)
    obj = re.sub(r"^the\s+", "", obj, flags=re.I)
    frag = re.sub(r"^((and|then)\s+)+", "", fragment.strip(), flags=re.I)
    frag = re.sub(r"\b(hard|soft)[ ]?boiled\b", r"\1-boiled", frag, flags=re.I)
    if frag.lower() in UN_STATE:
        return UN_STATE[frag.lower()].replace("{o}", obj)
    if re.search(r"\bor\b", frag):                    # "very finely minced or pureed" -> "Very finely mince or purée the garlic"
        sides = [x.strip() for x in re.split(r"\s*,?\s*\bor\b\s*", frag) if x.strip()]
        acts_or = [action(x, obj) for x in sides]
        if len(sides) == 2 and all(acts_or) and acts_or[0][1] == acts_or[1][1] and not acts_or[0][2]:
            (v1, o1, _), (v2, _, r2) = acts_or
            out = f"{v1} or {v2} {o1}{' ' + r2 if r2 else ''}"
            return out[:1].upper() + out[1:] + "."
        return None
    s = state_clause(item, frag)
    if s:
        return s + "."
    m = re.match(r"^(\d+)\s+((?:\w+ly\s+)?\w+ed)\s+and\s+(\d+)\s+((?:\w+ly\s+)?\w+ed)$", frag, re.I)   # "4 minced and 2 crushed"
    if m and action(m.group(2), obj) and action(m.group(4), obj):
        a, b = action(m.group(2), obj)[0], action(m.group(4), obj)[0]
        return f"{a[:1].upper() + a[1:]} {m.group(1)} of the {obj} and {b} the other {m.group(3)}."
    pieces = split_actions(frag, obj)
    states = [state_clause(item, p) for p in pieces]
    acts = [action(p, obj) if st is None else None for p, st in zip(pieces, states)]
    if any(a is None and st is None for a, st in zip(acts, states)):
        return None
    # targets: ("item",) / ("each",) / ("part", noun, prep).  After removing/trimming a part, the next verb is about
    # the item again ("ends trimmed, cut into segments"); after picking/separating one, or to reserve it, about the part.
    REMOVAL = {"remove", "discard", "trim", "cut off", "peel", "strip", "scrape", "snip"}
    KEEP_PART = {"reserve", "save", "keep", "set aside"}
    clauses, prev = [], None                 # [target, [verbs], rest]
    cool = None
    for a, st in zip(acts, states):
        if st is not None:
            continue
        vp, objp, rest = a
        verb = vp.split()[-1]
        m = re.match(r"the (.+?) (of|from) the ", objp)
        if m:
            tgt = ("part", m.group(1), m.group(2))
        elif objp.startswith("each "):
            tgt = ("each",)
        elif prev and prev[0] == "part" and (verb in KEEP_PART or prev_verb not in REMOVAL):
            tgt = prev
        else:
            tgt = ("item",) if not (prev and prev[0] == "each") else prev
        if verb == "cool" and not rest and clauses:
            cool = vp; continue
        if clauses and clauses[-1][0] == tgt and not clauses[-1][2]:
            clauses[-1][1].append(vp); clauses[-1][2] = rest
        else:
            clauses.append([tgt, [vp], rest])
        prev, prev_verb = tgt, verb
    mentioned, out = set(), []
    def noun(t):
        if t[0] == "part":
            if t in mentioned:
                return pronoun(t[1])
            mentioned.add(t)
            if ("item",) in mentioned or ("each",) in mentioned or any(x[0] == "part" for x in mentioned - {t}):
                return f"the {t[1]}"
            mentioned.add(("item",))
            return f"the {t[1]} {t[2]} the {obj}"
        if t in mentioned or (t == ("item",) and ("each",) in mentioned) or (t == ("each",) and ("item",) in mentioned):
            return pr if t == ("item",) else ("each" if False else pr)
        mentioned.add(t)
        if t == ("each",):
            sing = obj[:-1] if obj.endswith("s") and not obj.endswith("ss") else obj
            return f"each {sing}"
        return f"the {obj}"
    PARTICLES = ("up", "apart", "open", "out", "off", "down", "together", "through")
    for tgt, verbs, rest in clauses:
        n = noun(tgt)
        first = rest.split()[0].lower() if rest else ""
        vjoin = verbs[0] if len(verbs) == 1 else (" and ".join(verbs) if len(verbs) == 2 else ", ".join(verbs[:-1]) + ", and " + verbs[-1])
        if first in PARTICLES and n.startswith(("the ", "each ")):   # "Break up the larger pieces", "Roll out the dough"
            out.append(f"{vjoin} {rest.split()[0]} {n}{' ' + ' '.join(rest.split()[1:]) if len(rest.split()) > 1 else ''}")
        else:
            out.append(f"{vjoin} {n}{' ' + rest if rest else ''}")
    s = ", then ".join(out)
    if cool:
        adv = " ".join(cool.split()[:-1])
        last = clauses[-1][0]
        s += f" and let {pronoun(last[1]) if last[0] == 'part' else pr} cool{' ' + adv if adv else ''}"
    for st in [st for st in states if st]:                     # 'cold and cubed': cube it, keep it cold
        clause = st.replace(f"the {obj}", pr if s else f"the {obj}", 1)
        s = f"{s} and {clause[:1].lower() + clause[1:]}" if s else clause
    return s[:1].upper() + s[1:].rstrip(".") + "."


def source_sentence(item: str, src: str):
    """'fresh lemon juice', 'from 1 lemon' -> 'Juice 1 lemon.'; zest likewise; else None (kept as a note)."""
    low = item.lower()
    if "juice" in low:
        return f"Juice {src}."
    if "zest" in low:
        return f"Zest {src}."
    return None


def classify(fragment: str) -> str:
    f = fragment.strip()
    if USAGE.match(f): return "usage"
    if BRAND.match(f): return "brand"
    if ALT.match(f): return "alternative"
    if SOURCE.match(f): return "source"
    if re.search(r"\bdivided$", f, re.I) or re.match(r"^(only if|if )", f, re.I):
        return "usage"
    if PART_ONLY.search(f):
        return "part"
    if re.match(r"^not\b", f, re.I):
        return "descriptor"
    low = f.lower()
    if low in UN_STATE:
        return "state"
    verbs_in = [w for w in re.findall(r"[a-z-]+", low) if base_verb(w)]
    if verbs_in and all(w in DESCRIPTIVE for w in verbs_in) and len(low.split()) <= 6:
        return "descriptor"                       # "canned in adobo", "roasted", "dried", "assorted colors"
    if re.search(r"\bor\b", f) and not to_sentence("x", f):
        return "alternative"                      # "freshly squeezed or store-bought", "virgin or refined"
    if ON_ITEM.match(f):
        return "descriptor"
    if state_clause("x", re.sub(r"^(and|then)\s+", "", f, flags=re.I)):
        return "state"
    if to_sentence("x", f):
        return "prep"
    if DESCRIPTOR.match(f):
        return "descriptor"
    if not any(base_verb(w) for w in f.split()):   # a bare noun phrase: an 'or' the page lost ("parsley, tarragon")
        return "alternative"
    return "prep"


def prep_groups(name: str):
    """-> item, [(kind, parse_text, original_text)] where original_text is the page's own wording of that group
    (for the ingredient's "prep" field) and parse_text is what to_sentence reads."""
    pieces = split_top(name)
    out = []
    for p in pieces[1:]:
        k = classify(p)
        if k in ("prep", "state") and out and out[-1][0] in ("prep", "state"):
            joined = f"{out[-1][1]} and {re.sub(r'^((and|then)\s+)+', '', p, flags=re.I)}"
            if to_sentence(pieces[0], joined):
                out[-1] = ("prep", joined, f"{out[-1][2]}, {p}"); continue
        out.append((k, p, p))
    return pieces[0], out


def split_line(name: str):
    """-> item, [(kind, fragment)]; consecutive prep/state fragments of one line become ONE prep fragment
    ("stemmed", "seeded", "and roughly chopped" -> "stemmed and seeded and roughly chopped"), so the line gets one sentence."""
    pieces = split_top(name)
    out = []
    for p in pieces[1:]:
        k = classify(p)
        if k in ("prep", "state") and out and out[-1][0] in ("prep", "state"):
            joined = f"{out[-1][1]} and {re.sub(r'^((and|then)\s+)+', '', p, flags=re.I)}"
            if to_sentence(pieces[0], joined):
                out[-1] = ("prep", joined); continue
        out.append((k, p))
    return pieces[0], out
