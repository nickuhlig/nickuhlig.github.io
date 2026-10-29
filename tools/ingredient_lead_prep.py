"""Leading prep words on Serious Eats names: 'finely chopped fresh thyme leaves' -> item 'fresh thyme leaves' + prep 'finely chopped'.
lead_split(name, master_name) -> (item, lead) or (name, None); line_sentence(item, fragment) -> sentence, with fresh herbs
de-stemmed ("Pick the thyme leaves from the stems and finely chop them."). Words the linked ingredient is sold with stay
("crumbled feta cheese"), as do "packed", hard-boiled eggs and fried shallots (the caller handles eggs; shallots are a product)."""
import re
import ingredient_prep as ip

MOVE = {"chopped", "minced", "grated", "sliced", "diced", "peeled", "picked", "crumbled", "melted", "shredded", "drained",
        "rinsed", "julienned", "cubed", "torn", "halved", "quartered", "trimmed", "mashed", "beaten", "softened", "cored",
        "stemmed", "smashed", "pureed", "puréed", "shaved"}
ADV = {"finely", "thinly", "roughly", "coarsely", "lightly", "very", "freshly", "fine", "thick", "thin", "rough", "coarse",
       "thickly", "well", "gently"}
PACKED = re.compile(r"^((?:loosely|lightly|firmly|tightly|well)\s+)?packed\s+", re.I)   # a measuring note: stays in the name
_mv = "|".join(sorted(MOVE, key=len, reverse=True)); _ad = "|".join(ADV)
LEAD = re.compile(r"^((?:(?:%s)\s+)*(?:%s)(?:\s*(?:,|and|or|&)\s*(?:(?:%s)\s+)*(?:%s))*)\s+(.+)$" % (_ad, _mv, _ad, _mv), re.I)
KEEP_ITEM = re.compile(r"^(hard|soft)[- ]?(boiled|cooked|steamed)\b|^fried shallots?\b|^tonic\b", re.I)   # decided elsewhere
FIRST = re.compile(r"^(drained|rinsed|thawed|washed|scrubbed|cleaned|peeled|trimmed|stemmed|seeded|deseeded|cored|pitted|"
                   r"shelled|deveined)\b", re.I)   # done before cutting

HERBS = ["lemon thyme", "thyme", "rosemary", "oregano", "marjoram", "sage", "tarragon", "mint", "basil", "parsley",
         "cilantro", "dill", "chervil", "savory", "epazote", "lovage", "shiso", "perilla", "curry leaves"]


def lead_split(name: str, master_name: str = ""):
    """-> (new name, lead) ; lead None when nothing moves."""
    s = name.strip()
    packed = PACKED.match(s)
    if packed:
        s = s[packed.end():]
    leads = []
    while True:                                   # 'finely minced peeled fresh garlic' -> ['finely minced', 'peeled']
        m = LEAD.match(s)
        if not m or KEEP_ITEM.search(m.group(2)):
            break
        leads.append(m.group(1)); s = m.group(2)
    if not leads:
        return name, None
    words = {w.lower() for l in leads for w in re.findall(r"[a-zé]+", l, re.I)} & MOVE
    if master_name and any(re.search(rf"\b{w}\b", master_name, re.I) for w in words):   # sold that way: 'crumbled feta cheese'
        return name, None
    if packed:
        s = packed.group(0).lower() + s
    if name[:1].isupper():
        s = s[:1].upper() + s[1:]
    lead = " and ".join(reversed(leads)).lower()  # inner word happens first: 'peeled and finely minced'
    return s, lead


PROPER = {"Parmesan"}   # words seen capitalized mid-name (Parmigiano, Gruyère, Kalamata); filled by the caller


def core(item: str) -> str:
    """The object the sentence names: before any comma, without 'packed'/'fresh'; a line-start capital is dropped."""
    c = ip.split_top(item)[0]
    c = PACKED.sub("", c)
    c = re.sub(r"^fresh\s+", "", c, flags=re.I)
    first = c.split()[0] if c.split() else ""
    if first[:1].isupper() and first not in PROPER:
        c = c[:1].lower() + c[1:]
    return c


def herb_of(item: str):
    low = item.lower()
    if re.search(r"\bdried\b|\bground\b", low):
        return None
    for h in HERBS:
        if re.search(rf"\b{h}\b(?! (pickles?|seeds?|oil|paste|sauce|butter|salt|jelly|syrup|tea|extract|chutney))", low):
            return h
    if re.search(r"\bherbs\b", low):
        return "herbs"
    return None


def combine(lead: str, old_prep: str):
    """One fragment for to_sentence: drain/peel/trim-type words first, then the cuts."""
    parts = [p for p in (lead, old_prep) if p]
    firsts = [p for p in parts if FIRST.match(p)]
    return " and ".join(firsts + [p for p in parts if p not in firsts])


def line_sentence(item: str, frag: str):
    """Sentence for a line's whole prep fragment, de-stemming fresh herbs."""
    obj = core(item)
    h = herb_of(obj)
    if h and not re.search(r"\b(stems?|sprigs?|leaves|fronds?)\b|\bpicked\b.*\bleaves\b|whole|chiffonade|torn|bruised", frag, re.I):
        acts = re.sub(r"\b(freshly\s+)?picked\b(\s+and\b)?", "", frag, flags=re.I).strip(" ,")
        acts = re.sub(r"^and\s+", "", acts)
        tender = re.search(r"tender stems", item, re.I)
        several = h == "herbs" or len([x for x in HERBS if re.search(r"\b%s\b" % x, item.lower())]) > 1
        leaves = "herb leaves" if several else h if h.endswith("leaves") else "dill fronds" if h == "dill" else f"{h} leaves"
        pick = (f"Pick the {leaves} and tender stems from the thick stems" if tender else f"Pick the {leaves} from the stems")
        if not acts:
            return pick + "."
        s = ip.to_sentence("x", acts)
        if not s or not s.endswith(" the x."):
            return None
        verb = s[:-len(" the x.")]
        return f"{pick} and {verb[0].lower() + verb[1:]} them."
    m = re.fullmatch(r"(lemon|lime|orange|grapefruit) zest", obj, re.I)
    if m and re.fullmatch(r"(finely |freshly )?grated", frag, re.I):
        return f"{'Finely grate' if 'finely' in frag.lower() else 'Grate'} the zest from the {m.group(1).lower()}."
    if re.fullmatch(r"(freshly\s+)?picked", frag, re.I):
        return f"Pick the leaves of the {obj} from the stems."
    return ip.to_sentence(obj, frag)


# whole-line overrides: (name, old prep) -> (new name, prep, sentence)
SPECIAL = {("peeled fresh ginger", "minced or grated on a Microplane"):
           ("ginger root", "peeled, minced or grated on a Microplane", "Peel and mince or grate the ginger.")}
PARM = re.compile(r"^(?:fresh\s+)?parmig+iano[- ]reggiano(?:\s+cheese)?", re.I)


def standard_name(name: str) -> str:
    """Page spellings of the standard ingredient: 'Parmigiano-Reggiano cheese, divided' -> 'Parmesan cheese, divided'."""
    return PARM.sub("Parmesan cheese", name)
