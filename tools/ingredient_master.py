"""
Shared code for ingredient_master.json -- the single master list of ingredients: each
ingredient id, every name that resolves to it, and the dietary restrictions it violates.

This replaces three files that described the same ingredients in three places
(ingredient_index.json: name -> id; ingredient_aliases.json: hand-written merges;
ingredient_restrictions.json: id -> restrictions). mealime_ingredient_index.py and
albertsons_restriction_backfill.py both read and update this one file.

LOCATION: ingredient_master.json lives at the root of the library (next to manifest.json), found
from --recipes-dir via master_path_for(), so it is versioned and pushed with the data it governs.

FILE SHAPE
    {
      "meta": {...},
      "restriction_labels": {"1": "gluten", ...},
      "ingredients": {
        "187": {
          "name":        "large flour tortillas",          display name
          "names":       {"large flour tortillas": 522},    every spelling seen, normalized, with a use count
          "aliases":     ["flour tortilla"],               hand-written: these names ALWAYS resolve here
          "exact_only":  [],                               names that match ONLY exactly, never by cleanup
          "violates":    [1],                              list = known, [] = verified clean, null = UNKNOWN
          "source":      "mealime_learned",                mealime_learned | curated | auto | library_sync
          "recipes_seen": 522,
          "note":        null,
          "added":       null
        }
      }
    }

`violates: null` is the review queue -- ingredients that exist but whose restrictions nobody
has decided yet. A recipe containing one stays "unknown" instead of being guessed.

NAME RESOLUTION (IngredientMaster.resolve), first hit wins:
  1. EXACT      the normalized name is one of an ingredient's names/aliases/exact_only names
  2. ALIAS      the cleaned-up key of a hand-written alias ("flour tortillas" == "flour tortilla")
  3. NORMALIZED the cleaned-up key of any known name: size words (large/small/medium) and plural
                endings are ignored. Prep words (shredded, sliced, chopped, minced, diced), "frozen",
                "white" and parenthetical contents are NOT ignored -- they change the recipe or what
                the ingredient is. exact_only names never take part in this step.
  4. otherwise  resolve_or_create() makes a brand new entry (id from NEW_ID_START up, violates null)
"""

import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

MASTER_FILENAME = "ingredient_master.json"


def master_path_for(recipes_dir: Path) -> Path:
    """The master lives at the library root (next to manifest.json), not beside the scripts: it is
    versioned and pushed with the data it governs, so a machine running the pipeline (e.g. the
    Raspberry Pi) only needs the library checkout to have it."""
    return recipes_dir / MASTER_FILENAME


# Mealime's own ids run 2..486; ids created here start well above so they can't collide.
NEW_ID_START = 1000

# Weight given to hand-written aliases / exact_only names so they win any tie on an exact name.
HAND_WRITTEN_WEIGHT = 1_000_000

STRIP_WORDS = {"large", "small", "medium"}

# Qualifiers that change what a recipe violates; a match may never add or drop one.
PROTECTED_QUALIFIERS = (
    "gluten-free", "gluten free", "dairy-free", "dairy free", "non-dairy", "vegan",
    "vegetarian", "egg-free", "nut-free", "soy-free", "plant-based",
)

RESTRICTION_LABELS = {
    1: "gluten", 2: "dairy", 3: "fish", 4: "shellfish", 5: "peanut", 6: "tree nut",
    9: "soy", 10: "nightshade", 11: "egg", 12: "sesame", 13: "mustard", 14: "sulfite",
}


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _singular(word: str) -> str:
    """Crude plural -> singular. It only has to be CONSISTENT (it is applied to both the
    known names and the name being looked up), not linguistically right."""
    if len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "xes", "zes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def name_key(name: str) -> str:
    """Comparison key: lowercased, STRIP_WORDS dropped, words singularized, punctuation tidied.
    Parenthetical text is kept (only singularized) because it often says what a blend contains."""
    out = []
    for token in re.findall(r"[a-z0-9'\-&]+|[(),]", name.lower()):
        if token in STRIP_WORDS:
            continue
        out.append(_singular(token) if token.isalpha() else token)
    text = " ".join(out)
    text = re.sub(r"\(\s*(?:,\s*)*\)", " ", text)
    text = re.sub(r"\s+([),])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    text = re.sub(r"^[,\s]+|[,\s]+$", "", text)
    return re.sub(r"\s+", " ", text)


def protected_qualifiers(name: str) -> frozenset:
    low = name.lower()
    return frozenset(q.replace(" ", "-") for q in PROTECTED_QUALIFIERS if q in low)


def load_master(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path} not found -- the ingredient master lives at the library root "
                         f"({MASTER_FILENAME} next to manifest.json). Is --recipes-dir correct?")
    return json.loads(path.read_text(encoding="utf-8"))


def save_master(data: dict, path: Path) -> None:
    data["ingredients"] = {k: data["ingredients"][k] for k in sorted(data["ingredients"], key=int)}
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def new_entry(name: str, source: str) -> dict:
    return {
        "name": name.strip(),
        "names": {normalize_name(name): 1},
        "aliases": [],
        "exact_only": [],
        "violates": None,
        "source": source,
        "recipes_seen": 0,
        "note": None,
        "added": date.today().isoformat(),
    }


class IngredientMaster:
    def __init__(self, data: dict):
        self.data = data
        self.entries = data["ingredients"]
        self.created = []          # ids created from an unmatched name this session
        self.synced = []           # ids added from the library's own id-bearing lines
        self.added_names = []      # (id, name) spellings added to existing entries
        self._rebuild()

    def _rebuild(self) -> None:
        name_counts = defaultdict(Counter)   # normalized name -> Counter(id -> weight)
        key_counts = defaultdict(Counter)    # name_key -> Counter(id -> weight)
        self.alias_key_to_id = {}
        for id_str, entry in self.entries.items():
            ing_id = int(id_str)
            exact_only = {normalize_name(n) for n in entry.get("exact_only", [])}
            for name, count in entry.get("names", {}).items():
                name_counts[name][ing_id] += count
                key = name_key(name)
                if key and name not in exact_only:
                    key_counts[key][ing_id] += count
            for name in exact_only:
                name_counts[name][ing_id] += HAND_WRITTEN_WEIGHT
            for alias in entry.get("aliases", []):
                name_counts[normalize_name(alias)][ing_id] += HAND_WRITTEN_WEIGHT
                key = name_key(alias)
                if key:
                    self.alias_key_to_id[key] = ing_id
        self.name_to_id = {name: counter.most_common(1)[0][0] for name, counter in name_counts.items()}
        self.key_to_id = {key: counter.most_common(1)[0][0] for key, counter in key_counts.items()}
        self.ambiguous_keys = {
            key: {str(i): c for i, c in counter.most_common()}
            for key, counter in key_counts.items() if len(counter) > 1
        }
        self.next_id = max([NEW_ID_START - 1] + [int(i) for i in self.entries]) + 1

    def display_name(self, ing_id: int):
        entry = self.entries.get(str(ing_id))
        return entry["name"] if entry else None

    def _qualifiers_ok(self, ing_id: int, wanted: frozenset) -> bool:
        entry = self.entries.get(str(ing_id))
        return entry is None or protected_qualifiers(entry["name"]) == wanted

    def resolve(self, name: str):
        """Returns (id, how) with how in exact/alias/normalized, or (None, "new"/"unresolved")."""
        norm = normalize_name(name)
        if norm in self.name_to_id:
            return self.name_to_id[norm], "exact"
        key = name_key(name)
        if not key:
            return None, "unresolved"
        wanted = protected_qualifiers(name)
        for how, table in (("alias", self.alias_key_to_id), ("normalized", self.key_to_id)):
            candidate = table.get(key)
            if candidate is not None and self._qualifiers_ok(candidate, wanted):
                return candidate, how
        return None, "new"

    def resolve_or_create(self, name: str):
        ing_id, how = self.resolve(name)
        if ing_id is not None or how == "unresolved":
            return ing_id, how
        ing_id = self.next_id
        self.next_id += 1
        self.entries[str(ing_id)] = new_entry(name, "auto")
        self.name_to_id[normalize_name(name)] = ing_id
        key = name_key(name)
        if key and key not in self.key_to_id:
            self.key_to_id[key] = ing_id
        self.created.append(ing_id)
        return ing_id, "new"

    def observe(self, ing_id: int, name: str) -> None:
        """Record an (id, name) pair the library already carries: unknown id -> new entry
        (violates null); known id, unseen spelling -> add the spelling. Call rebuild() after."""
        entry = self.entries.get(str(ing_id))
        norm = normalize_name(name)
        if entry is None:
            self.entries[str(ing_id)] = new_entry(name, "library_sync")
            self.synced.append(ing_id)
        elif norm not in self.name_to_id or self.name_to_id[norm] != ing_id:
            if norm not in entry["names"]:
                entry["names"][norm] = 1
                self.added_names.append((ing_id, norm))

    def rebuild(self) -> None:
        self._rebuild()

    def violates(self, ing_id: int):
        """None if the id is unknown to the master OR its restrictions are still undecided."""
        entry = self.entries.get(str(ing_id))
        if entry is None or entry.get("violates") is None:
            return None
        return frozenset(entry["violates"])
