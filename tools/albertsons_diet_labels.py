"""
Applies diet_rules.py to the Albertsons recipes the weekly pipeline has just fetched, so a loosely-labelled recipe
(Paleo with cheese, Vegan with naan, Pescatarian with bacon ...) never enters the library with a label our rules reject.

WHERE IT FITS IN THE PIPELINE -- last stage, AFTER mealime_ingredient_index.py (needs ingredient ids) and
albertsons_restriction_backfill.py, and BEFORE the manifest is rebuilt (so the manifest carries the corrected labels):

    fetch  ->  ingredients  ->  restrictions  ->  cookbooks  ->  diet labels (this script)

WHICH RECIPES  only the files fetch_albertsons_native_recipes.py wrote in THIS run (it returns their paths), plus the
"pending" ones from earlier runs (below).  Recipes already in the library are never revisited, so a label you restore by
hand is never taken away again.

WHAT IT DOES  removes labels, never adds them:
    Paleo (5)        any ingredient flagged not_paleo or dairy  (strict Paleo; honey / maple syrup / dates are fine)
    Pescatarian (4)  any meat (gelatin, bacon bits ... included)               -> also removes Vegetarian and Vegan
    Vegetarian (2)   any meat, fish or shellfish                                -> also removes Vegan
    Vegan (6)        any meat, fish, shellfish, dairy, egg or honey
  and when Paleo goes, the word "Paleo" is dropped from group_label ("Keto/Paleo" -> "Keto", "Paleo" -> "Default").
  Low Carb / Keto are untouched (Albertsons recipes carry no nutrition).  Thai curry paste is exempt from the shellfish flag
  (Mealime and the library treat it as vegan).

UNDECIDED INGREDIENTS  a recipe with an ingredient the master has not decided yet cannot be judged (a recipe is never called
vegan on a guess), so it keeps Albertsons' own labels and is written to <sweep-dir>/diet_label_pending.json.  Every later run
re-checks the pending ones, so once you have decided the ingredient (violates in ingredient_master.json) the labels get
corrected on the next run and the recipe leaves the list.  The list is bookkeeping only: nobody edits it.

Writes albertsons_diet_labels_report.json next to this script: what was removed and why, and which ingredients are holding
recipes back.

Usage (normally called by albertsons_pipeline.py; standalone for a check):
    python albertsons_diet_labels.py --recipes-dir <library> --all --dry-run     # would anything change?
"""

import argparse
import json
import re
from pathlib import Path

import diet_rules
from ingredient_master import IngredientMaster, load_master, master_path_for
from library_paths import DEFAULT_LIBRARY_DIR, DEFAULT_SWEEP_DIR

OWN_SOURCES = {"albertsons_native", "albertsons_native_gap_fill"}
NESTED = {4: [2, 6], 2: [6]}                      # losing the key also loses these (vegan is a subset of vegetarian of pescatarian)
PENDING_FILENAME = "diet_label_pending.json"
DEFAULT_REPORT_PATH = Path(__file__).with_name("albertsons_diet_labels_report.json")
NAMES = {2: "vegetarian", 4: "pescatarian", 5: "paleo", 6: "vegan"}


def new_group_label(label):
    """Drops the word "Paleo" from a group label: "Keto/Paleo" -> "Keto", "Paleo" -> "Default"."""
    if not label or not re.search(r"paleo", label, re.I):
        return label
    out = re.sub(r"\s*[/,&]?\s*paleo\s*[/,&]?\s*", " ", label, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip(" /,&-")
    return out or "Default"


def judge(record: dict, master: IngredientMaster):
    """(removals, undecided): removals = {type id: sorted offending ingredient names}; undecided = names the master has not decided.
    When anything is undecided the removals are empty -- no guessing."""
    by_flag: dict = {}
    undecided = []
    for ing in record.get("ingredients") or []:
        entry = master.entries.get(str(ing.get("ingredient_id"))) if ing.get("ingredient_id") is not None else None
        if entry is None or (entry.get("violates") is None and entry.get("diet_flags") is None):
            undecided.append(ing.get("name"))
            continue
        flags = diet_rules.ingredient_flags(entry)
        if re.search(r"curry paste", ing.get("name") or "", re.I):
            flags = flags - {"shellfish"}
        for flag in flags:
            by_flag.setdefault(flag, set()).add(ing.get("name"))
    if undecided:
        return {}, undecided
    labels = record.get("allowed_type_ids") or []
    removals: dict = {}

    def add(type_id, *flag_names):
        if type_id in labels:
            names = set().union(*(by_flag.get(f, set()) for f in flag_names))
            if names:
                removals[type_id] = sorted(names)

    add(5, "not_paleo", "dairy")
    add(4, "meat")
    add(2, "meat", "fish", "shellfish")
    add(6, "meat", "fish", "shellfish", "dairy", "egg", "honey")
    return removals, []


def apply_removals(record: dict, removals: dict) -> None:
    drop = set(removals)
    for type_id in list(drop):
        drop.update(NESTED.get(type_id, []))
    had_paleo = 5 in record["allowed_type_ids"]
    record["allowed_type_ids"] = [t for t in record["allowed_type_ids"] if t not in drop]
    if had_paleo and 5 not in record["allowed_type_ids"]:
        record["group_label"] = new_group_label(record.get("group_label"))


def _rel(recipes_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(recipes_dir.resolve()).as_posix()


def run(recipes_dir: Path, new_paths, sweep_dir: Path, dry_run: bool = False, report_path: Path = DEFAULT_REPORT_PATH,
        everything: bool = False) -> dict:
    """new_paths: files written by this run's fetch (None / [] for a local-only run). everything=True checks every Albertsons record
    (for a standalone dry-run check; the pipeline never does this)."""
    master = IngredientMaster(load_master(master_path_for(recipes_dir)))
    pending_path = sweep_dir / PENDING_FILENAME
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pending = []
    candidates = [_rel(recipes_dir, Path(p)) for p in (new_paths or [])] + list(pending)
    if everything:
        candidates = [_rel(recipes_dir, p) for p in (recipes_dir / "families").glob("*/*.json") if p.name != "alt_variants.json"]
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    changed, still_pending, held_by = [], [], {}
    checked = 0
    for rel in candidates:
        path = recipes_dir / rel
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                                     # gone or unreadable: drop it from the list
        if not isinstance(record, dict) or record.get("source") not in OWN_SOURCES or not record.get("allowed_type_ids"):
            continue
        checked += 1
        removals, undecided = judge(record, master)
        if undecided:
            still_pending.append(rel)
            for name in undecided:
                held_by.setdefault(name, []).append(rel)
            continue
        if removals:
            before = list(record["allowed_type_ids"])
            apply_removals(record, removals)
            changed.append({"file": rel, "name": record.get("name"), "removed": sorted(set(before) - set(record["allowed_type_ids"])),
                            "because": {NAMES[t]: names for t, names in removals.items()}})
            if not dry_run:
                path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    if not dry_run:
        sweep_dir.mkdir(parents=True, exist_ok=True)
        if not everything:
            pending_path.write_text(json.dumps(sorted(still_pending), indent=2), encoding="utf-8")
    report = {"checked": checked, "changed": changed,
              "waiting_on_undecided_ingredients": {name: sorted(files) for name, files in sorted(held_by.items())}}
    if not dry_run:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    verb = "Would change" if dry_run else "Changed"
    print(f"Diet labels: checked {checked} Albertsons file(s); {verb.lower()} {len(changed)}; {len(still_pending)} waiting on undecided ingredients.")
    for item in changed[:20]:
        print(f"  {item['name']}: removed {[NAMES.get(t, t) for t in item['removed']]} because {item['because']}")
    if len(changed) > 20:
        print(f"  ... and {len(changed) - 20} more (see {report_path.name})")
    if held_by:
        print(f"  Undecided ingredients holding recipes back ({len(held_by)}): {sorted(held_by)[:15]}"
              f" -- decide them in ingredient_master.json (violates) and the next run finishes the job")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recipes-dir", type=Path, default=DEFAULT_LIBRARY_DIR)
    parser.add_argument("--sweep-dir", type=Path, default=DEFAULT_SWEEP_DIR)
    parser.add_argument("--all", action="store_true", help="check every Albertsons record (use with --dry-run)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.recipes_dir, [], args.sweep_dir, args.dry_run, everything=args.all)


if __name__ == "__main__":
    main()
