"""
Candidate source foods, across every tier, for master ingredients that nutrition_foods.json does
not cover yet -- the input to the user's review (picking stays a human step).

    python nutrition_match.py propose --usage <usage.json> --out <candidates.json> [--limit N]

For each unmatched ingredient used by non-Mealime recipes (most-used first):
  1. OpenNutrition exact name: the ingredient's name, aliases or common spellings equal an
     OpenNutrition food's name or alternate name. When that food cites a USDA SR Legacy, CNF or
     Frida food, the cited lab-grade food is proposed (tier 1/2) -- OpenNutrition copied its
     numbers from there; otherwise the OpenNutrition food itself (tier 3).
  2. Otherwise: an SR Legacy search via the FDC API (cached in the output file, so re-runs resume)
     plus fuzzy name matches in CNF, Frida and OpenNutrition (shared words, singularized).
  3. Always: OpenNutrition grocery products whose name (before " by <brand>") equals the
     ingredient's name -- e.g. "Aleppo Pepper by Morton & Bassett Spices" -- most complete first.
     Labels that fail nutrition_sources.label_problem (all zeros from a rounded 1-tsp serving,
     calories that don't fit the macros) are dropped and only counted in "on_grocery_rejected".
     Needs the index: python nutrition_sources.py import --on-grocery-index <tsv>.
USDA Branded products are not searched here; they are for what remains after review.

usage.json: {ingredient id: non-Mealime line count}, as written by fdc_ingredient_match.py --usage.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import fdc_client
import nutrition_composite as nc
import nutrition_sources as ns
from fdc_ingredient_match import search_query
from ingredient_master import load_master, master_path_for
from library_paths import DEFAULT_LIBRARY_DIR

CITED = {"USDA Standard Reference, Legacy": "sr", "Canadian Nutrient File": "cnf", "Frida": "frida"}
STOP = {"and", "or", "with", "the", "of", "in", "a", "fresh", "raw", "plain", "large", "small", "medium"}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", (text or "").lower().replace("-", " "))).strip()


def singular(text: str) -> str:
    return " ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in text.split())


def words(text: str) -> set:
    return {w for w in singular(norm(text)).split() if w not in STOP and len(w) > 2}


def ingredient_names(entry: dict) -> list:
    names = [entry["name"]] + list(entry.get("aliases") or [])
    names += [n for n, _ in sorted((entry.get("names") or {}).items(), key=lambda kv: -kv[1])[:5]]
    out = []
    for n in names:
        for v in (n, re.split(r",|\(", n)[0]):
            if v.strip() and v not in out:
                out.append(v.strip())
    return out


def fuzzy(records: dict, name_words: set, top: int = 3) -> list:
    """Best records by shared words (Jaccard on singularized words of the main name)."""
    scored = []
    for rec in records.values():
        rw = words(re.split(r",|\(", rec["name"] or "")[0]) | words(rec["name"] or "")
        if not rw or not (name_words & rw):
            continue
        score = len(name_words & rw) / len(name_words | rw)
        scored.append((score, rec))
    scored.sort(key=lambda s: -s[0])
    return [{"ref": r["key"], "name": r["name"], "score": round(s, 2), "fields": len(r["per_100g"])}
            for s, r in scored[:top]]


def cmd_propose(args) -> None:
    library_dir = Path(args.recipes_dir)
    master = load_master(master_path_for(library_dir))["ingredients"]
    db = nc.load_db(library_dir)
    usage = json.loads(Path(args.usage).read_text(encoding="utf-8"))
    todo = [i for i, _ in sorted(usage.items(), key=lambda kv: -kv[1]) if i in master and i not in db["ingredients"]]
    todo = todo[:args.limit] if args.limit else todo

    sources = {s: ns.load(s) for s in ("cnf", "frida", "on")}
    exact = {}
    for rec in sources["on"].values():
        for n in [rec["name"]] + rec["alt_names"]:
            exact.setdefault(singular(norm(n)), rec)

    grocery = ns.load("on_grocery_index") if (ns.DATA_DIR / "on_grocery_index.json.gz").exists() else {}

    by_word = {}                       # word -> grocery base names containing it
    for base in grocery:
        for w in set(base.split()):
            by_word.setdefault(w, []).append(base)

    def grocery_hits(names):
        """(usable products, number rejected as implausible labels)"""
        hits, rejected = {}, 0

        def add(products, match):
            nonlocal rejected
            for g in products:
                problem = ns.label_problem(g[1], *g[3:8])
                if problem:
                    rejected += 1
                    continue
                hits[g[0]] = {"id": g[0], "name": g[1], "fields": g[2], "kcal": g[3], "match": match}

        for nm in names:
            add(grocery.get(ns.grocery_base_name(nm), []), "exact")
        if not hits:   # brand-prefixed names: "Mother in Law's, Gochugaru Korean Chile Flakes"
            for phrase in {ns.grocery_base_name(nm) for nm in names[:3]}:
                ws = phrase.split()
                if len(phrase) < 4 or not ws:
                    continue
                pool = min((by_word.get(w, []) for w in ws), key=len)
                pat = re.compile(r"\b" + re.escape(phrase) + r"\b")
                for base in pool:
                    if pat.search(base):
                        add(grocery[base], "contains")
            return sorted(hits.values(), key=lambda g: (len(g["name"]), -g["fields"]))[:5], rejected
        return sorted(hits.values(), key=lambda g: -g["fields"])[:5], rejected

    out_path = Path(args.out)
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    for n, iid in enumerate(todo, 1):
        if iid in out:
            if grocery and args.refresh_grocery:
                out[iid]["on_grocery"], out[iid]["on_grocery_rejected"] = grocery_hits(ingredient_names(master[iid]))
            continue
        entry = master[iid]
        names = ingredient_names(entry)
        prop = {"name": entry["name"], "uses": usage[iid], "names": names[:6]}
        hit = next((exact[singular(norm(nm))] for nm in names if singular(norm(nm)) in exact), None)
        if hit:
            # tier order: SR Legacy first, then CNF / Frida; skip ids our imports don't have
            cited = sorted((c for c in hit["cites"] if c["db"] in CITED), key=lambda c: CITED[c["db"]] != "sr")
            cited = [c for c in cited if CITED[c["db"]] == "sr" or f"{CITED[c['db']]}:{c['id']}" in sources[CITED[c["db"]]]]
            prop["on_exact"] = {"ref": hit["key"], "name": hit["name"], "cites": cited}
            if cited:
                c = cited[0]
                prop["suggest"] = {"ref": f"{CITED[c['db']]}:{c['id']}", "name": c["name"], "via": "OpenNutrition citation"}
            else:
                prop["suggest"] = {"ref": hit["key"], "name": hit["name"], "via": "OpenNutrition exact name"}
        else:
            nw = words(entry["name"])
            prop["sr_search"] = fdc_client.search(search_query(entry["name"]), data_types=("SR Legacy",), page_size=8)
            for s in ("cnf", "frida", "on"):
                prop[f"{s}_fuzzy"] = fuzzy(sources[s], nw)
        if grocery:
            prop["on_grocery"], prop["on_grocery_rejected"] = grocery_hits(names)
        out[iid] = prop
        if n % 50 == 0:
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {n}/{len(todo)}", file=sys.stderr)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(out)} ingredients with candidates -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recipes-dir", default=str(DEFAULT_LIBRARY_DIR))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--usage", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--refresh-grocery", action="store_true",
                   help="recompute grocery-product candidates for ingredients already in --out")
    args = parser.parse_args()
    cmd_propose(args)


if __name__ == "__main__":
    main()
