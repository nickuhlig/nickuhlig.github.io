#!/usr/bin/env python3
"""
Imports Serious Eats recipes: finds the recipe pages, fetches them, and (with --align) turns each into
library records via external_align.py -- so what lands in the library already has the library's format and
the exporters need no special cases.

Fetching and aligning are separate steps on purpose. Pages are kept on disk (default; --no-raw to skip), so
a mistake in the alignment rules is fixed by re-running `align` over the saved pages -- no network, no
refetching thousands of pages. Once the rules are settled, `fetch --align --no-raw` does it in one pass.

COMMANDS
    python seriouseats_scraper.py discover                  read the sitemap, record every recipe URL
    python seriouseats_scraper.py fetch --limit 5           fetch pages that are new or changed (try a few first)
    python seriouseats_scraper.py fetch --limit 5 --align   ... and align each into library records as it arrives
    python seriouseats_scraper.py fetch --sample 100 --align --out-dir ../scratch_lib6    a random test batch of 100, into a scratch folder
    python seriouseats_scraper.py align --limit 5           re-align pages already saved (no network): the dev loop
    python seriouseats_scraper.py fetch                     all of them (~4.5 h for ~4,900 pages at the 3 s default)
    python seriouseats_scraper.py thumbnails                download the picture of every recipe that has none (thumbnail.jpg, ~300 px, ~16 KB)
                                                            (fetch --align does this by itself for each new or changed page; --no-thumbs skips it)
    python seriouseats_scraper.py status                    counts by state
    python seriouseats_scraper.py renumber                  one-off: move an old-scheme registry to the 5,000,000 block
    python seriouseats_scraper.py run                       discover, then fetch + align (what a scheduled job would call)

  While developing, add --out-dir C:/scratch/lib: records and a COPY of the ingredient master go there, so
  experiments never touch the real library. After each run read tools/external_review_report.json (new
  ingredients with suggested restrictions, loose matches, optional lines moved to notes, untagged categories).

THE REGISTRY  <library>/source_registry.csv  (committed with the library; one row per URL)
    source,url,id,lastmod,fetched_lastmod,fetched_at,status
  `id` is the URL's permanent family id, from a block starting at 5,000,000 (see external_recipes.py; the
  Metric twin is id + 71). New URLs are numbered in order of the site's own page number where the URL has one,
  never reused, so the same recipe keeps the same id on every machine. `lastmod` is the sitemap's
  last-modified date (checked against each page's own dateModified: they agree). A page is re-fetched
  only when its lastmod is newer than the one it was fetched at ("stale").
  status:  new -> ok | gone (404/410) | norecipe (page has no Recipe data) | error (retried next run)
           ok -> stale (changed on the site) -> ok

THE SAVED PAGES  <raw-dir>/<source>/<id>.html.gz   (default tools/raw_pages/, git-ignored)

POLITE AND SAFE
  * about one request every 3 seconds (--delay, with jitter; at 1 s the site refuses every third or fourth page), resumable: the registry is saved every 25 pages and
    on Ctrl-C, so a rerun carries on where it stopped.
  * Plain, honest user agent; no attempt to look like a browser.
  * If the site starts refusing (403/429/503, or a challenge page instead of a recipe) five times in a
    row, the run STOPS with exit code 3 and says so -- it never tries to get around a block.

Needs `pip install requests`.
"""

import argparse
import csv
import gzip
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from external_align import Aligner
from external_recipes import family_index, is_family_id, nth_family_id, parse_page, site_number
from library_paths import DEFAULT_LIBRARY_DIR, TOOLS_DIR

SOURCE = "seriouseats"
BASE_URL = "https://www.seriouseats.com"
RECIPE_PATH_RE = re.compile(r"-recipe(-\d{6,})?$")   # "...-recipe" (older) and "...-recipe-12168126" (newer)
USER_AGENT = "Mozilla/5.0 (compatible; recipe-library-scraper/1.0)"
COLUMNS = ["source", "url", "id", "lastmod", "fetched_lastmod", "fetched_at", "status"]
DEFAULT_RAW_DIR = TOOLS_DIR / "raw_pages"
MAX_CONSECUTIVE_BLOCKS = 5
RETRIES = 2          # extra tries for a refused page, after RETRY_WAIT and then 2 x RETRY_WAIT seconds
RETRY_WAIT = 30.0
SAVE_EVERY = 25
EXIT_OK, EXIT_ERROR, EXIT_BLOCKED = 0, 1, 3


class Blocked(Exception):
    """The site is refusing requests; stop rather than push on."""


# --- registry -------------------------------------------------------------------------------

def load_registry(path: Path) -> dict[tuple[str, str], dict]:
    rows = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rows[(row["source"], row["url"])] = row
    return rows


def save_registry(path: Path, rows: dict[tuple[str, str], dict]) -> None:
    ordered = sorted(rows.values(), key=lambda r: (r["source"], int(r["id"])))
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(ordered)
    temp.replace(path)


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def is_stale(row: dict) -> bool:
    lastmod, fetched = parse_time(row["lastmod"]), parse_time(row["fetched_lastmod"])
    return bool(lastmod and fetched and lastmod > fetched)


# --- discovery -------------------------------------------------------------------------------

def http_get(session: requests.Session, url: str, retries: int = 4) -> requests.Response:
    """GET with retries for connection trouble and 5xx (except 503, which counts as a possible block)."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=30)
            if response.status_code < 500 or response.status_code == 503:
                return response
            last = RuntimeError(f"HTTP {response.status_code}")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last = exc
        if attempt < retries:
            wait = 2 * 2 ** (attempt - 1)
            print(f"    [retry] {url} ({last}); waiting {wait}s (attempt {attempt}/{retries})")
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}: {last}")


def sitemap_entries(session: requests.Session, sitemap_url: str) -> list[tuple[str, str]]:
    """[(url, lastmod)] from a sitemap, following a sitemap index into its children."""
    response = http_get(session, sitemap_url)
    if response.status_code != 200:
        raise RuntimeError(f"{sitemap_url}: HTTP {response.status_code}")
    xml = response.text
    if "<sitemapindex" in xml:
        found = []
        for child in re.findall(r"<sitemap>.*?<loc>([^<]+)</loc>.*?</sitemap>", xml, re.S):
            found += sitemap_entries(session, child.strip())
        return found
    entries = []
    for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
        loc = re.search(r"<loc>([^<]+)</loc>", block)
        mod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
        if loc:
            entries.append((loc.group(1).strip(), mod.group(1).strip() if mod else ""))
    return entries


def id_order_key(url: str):
    number = site_number(url)
    return (number is None, number or 0, url)


def check_ids_current(mine: list[dict]) -> None:
    if any(not is_family_id(int(r["id"])) for r in mine):
        raise RuntimeError("the registry still has ids from the earlier numbering scheme -- run "
                           "`python seriouseats_scraper.py renumber` once first")


def renumber(rows: dict, registry_path: Path, raw_dir: Path, *dirs: Path) -> int:
    """One-off move from the first numbering (50,000,001...) to the 5,000,000 block. Reassigns every row's id,
    renames the saved pages to match, and deletes the records written under the old ids in the given library /
    scratch folders (they are only derived output: `align` rewrites them under the new ids)."""
    mine = sorted((r for (src, _), r in rows.items() if src == SOURCE), key=lambda r: id_order_key(r["url"]))
    if not any(not is_family_id(int(r["id"])) for r in mine):
        print("Nothing to renumber: every id is already in the 5,000,000 block.")
        return 0
    old_ids = {r["id"]: r for r in mine}
    mapping = {}
    for n, row in enumerate(mine):
        mapping[row["id"]] = str(nth_family_id(n))
    if set(mapping.values()) & set(mapping):
        raise RuntimeError("old and new ids overlap; refusing to renumber")
    moved = 0
    for old, new in mapping.items():
        source_page = raw_dir / SOURCE / f"{old}.html.gz"
        if source_page.exists():
            source_page.rename(source_page.with_name(f"{new}.html.gz"))
            moved += 1
    for old, new in mapping.items():
        old_ids[old]["id"] = new
    save_registry(registry_path, rows)               # the registry first: clearing out old output below is only tidying
    removed, stuck = 0, []
    for base in dict.fromkeys(dirs):
        for old in mapping:
            family = base / "families" / old
            if not family.is_dir():
                continue
            for attempt in range(3):                 # Google Drive / Explorer can hold a folder for a moment
                try:
                    shutil.rmtree(family)
                    removed += 1
                    break
                except OSError:
                    time.sleep(1)
            else:
                stuck.append(family)
    print(f"Renumbered {len(mapping)} recipe(s): ids {min(int(v) for v in mapping.values())} .. {max(int(v) for v in mapping.values())}; "
          f"renamed {moved} saved page(s); removed {removed} old family folder(s) (run `align` to rewrite them).")
    if stuck:
        print(f"{len(stuck)} old folder(s) could not be deleted (in use?) -- harmless, but delete them by hand when you can:")
        for family in stuck[:10]:
            print(f"    {family}")
    return len(mapping)


def discover(session: requests.Session, base_url: str, rows: dict, urls_file: Path | None = None) -> tuple[int, int, int]:
    """Add new recipe URLs to the registry and refresh lastmods. Returns (new, changed, total_recipe_urls)."""
    if urls_file:
        entries = [(line.strip(), "") for line in urls_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        entries = sitemap_entries(session, base_url + "/sitemap.xml")
    # A hand-made URL list is taken as given (sub-recipes like ".../fresh-egg-pasta" have no "-recipe" in the
    # slug); fetch still marks a page without Recipe data as norecipe.
    recipes = {url: mod for url, mod in entries if urls_file or RECIPE_PATH_RE.search(re.sub(r"^https?://[^/]+", "", url))}

    mine = [r for (src, _), r in rows.items() if src == SOURCE]
    check_ids_current(mine)
    next_index = max((family_index(int(r["id"])) for r in mine), default=-1) + 1
    new = changed = 0
    # New URLs are numbered in order of the site's own page number where the URL has one (older URLs have none
    # and follow, by URL). Ids are permanent once handed out, so this only orders each batch.
    for url in sorted(recipes, key=id_order_key):
        key = (SOURCE, url)
        if key not in rows:
            rows[key] = {"source": SOURCE, "url": url, "id": str(nth_family_id(next_index)), "lastmod": recipes[url],
                         "fetched_lastmod": "", "fetched_at": "", "status": "new"}
            next_index += 1
            new += 1
        elif recipes[url] and recipes[url] != rows[key]["lastmod"]:
            rows[key]["lastmod"] = recipes[url]
            if rows[key]["status"] in ("ok", "norecipe") and is_stale(rows[key]):
                rows[key]["status"] = "stale"
                changed += 1
    return new, changed, len(recipes)


# --- fetching --------------------------------------------------------------------------------

RECIPE_MARKER = re.compile(r'application/ld\+json.*?"@type":\s*\[?\s*"Recipe"', re.S)
CHALLENGE_HINT = re.compile(r"captcha|are you a (human|robot)|access denied|unusual traffic|challenge-platform", re.I)


def fetch_page(session: requests.Session, url: str) -> tuple[str, bytes | None]:
    """(status, page bytes): status is ok | gone | norecipe | error; the bytes are the page EXACTLY as served
    (never response.text -- requests guesses Latin-1 when a server omits the charset, which would mangle
    accented characters like the e in Gruyere). Raises Blocked if the site is refusing."""
    try:
        response = http_get(session, url)
    except RuntimeError as exc:
        print(f"    -> error: {exc}")
        return "error", None
    code = response.status_code
    if code in (403, 429, 503):
        raise Blocked(f"HTTP {code} from {url}")
    if code in (404, 410):
        return "gone", None
    if code != 200:
        print(f"    -> unexpected HTTP {code}")
        return "error", None
    body = response.content.decode("utf-8", errors="replace")  # for looking at only; what gets saved is response.content
    if RECIPE_MARKER.search(body):
        return "ok", response.content
    if len(body) < 30_000 and CHALLENGE_HINT.search(body):
        raise Blocked(f"a challenge page instead of a recipe at {url}")
    return "norecipe", None


def fetch_with_retries(session: requests.Session, url: str, retry_wait: float) -> tuple[str, bytes | None]:
    """fetch_page, but a refusal gets RETRIES more tries after a pause (retry_wait, then 2x) before it counts as
    blocked -- the site's refusals are intermittent. Plain backoff; nothing about the request changes."""
    for attempt in range(RETRIES + 1):
        try:
            return fetch_page(session, url)
        except Blocked as exc:
            if attempt == RETRIES or retry_wait <= 0:
                raise
            pause = retry_wait * 2 ** attempt
            print(f"    -> refused ({exc}); retry {attempt + 1}/{RETRIES} in {pause:g}s")
            time.sleep(pause)
    raise AssertionError("unreachable")


def raw_path(raw_dir: Path, row: dict) -> Path:
    return raw_dir / row["source"] / f"{row['id']}.html.gz"


def needs_fetch(row: dict) -> bool:
    return row["status"] in ("new", "stale", "error")


def select_rows(rows: dict, predicate, *, limit: int | None, match: str | None, ids: set[int] | None,
                sample: int | None = None, seed: int | None = None, source: str = SOURCE) -> list[dict]:
    chosen = sorted((r for (src, _), r in rows.items() if src == source and predicate(r)), key=lambda r: int(r["id"]))
    if match:
        chosen = [r for r in chosen if match in r["url"]]
    if ids:
        chosen = [r for r in chosen if int(r["id"]) in ids]
    if sample is not None and sample < len(chosen):                    # a random sample, back in id order (a seed makes it repeatable)
        chosen = sorted(random.Random(seed).sample(chosen, sample), key=lambda r: int(r["id"]))
    return chosen[:limit] if limit is not None else chosen


# --- thumbnails -------------------------------------------------------------------------------------------------------------

THUMB_NAME = "thumbnail.jpg"        # what the manifest builder and the exporters look for in a family folder
THUMB_SIZE = 300                    # the library's other thumbnails are about 300 px, median 17 KB: this fits them in
THUMB_QUALITY = 82
MAX_THUMB_FAILURES = 5              # this many in a row and the run stops, like a block on the pages themselves
THUMB_COUNTS: dict[str, int] = {}


def make_thumbnail(data: bytes) -> bytes:
    """The site's image (about 1500 px, 150 KB) shrunk to fit 300 x 300 and saved as a small JPEG. Needs Pillow."""
    from io import BytesIO
    from PIL import Image, ImageOps
    image = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    image.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)         # never enlarges
    out = BytesIO()
    image.save(out, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
    return out.getvalue()


def ensure_thumbnail(session: requests.Session, family_dir: Path, image_url: str | None, previous_url: str | None = None) -> str:
    """Downloads the recipe's picture into family_dir/thumbnail.jpg unless it is already there for the same picture.
    Returns downloaded | kept | no_image | error: ... (an error never stops the run by itself)."""
    target = family_dir / THUMB_NAME
    if not image_url:
        return "no_image"
    if target.exists() and previous_url in (None, image_url):
        return "kept"
    try:
        response = session.get(image_url, timeout=30)
        if response.status_code != 200:
            return f"error: HTTP {response.status_code}"
        target.write_bytes(make_thumbnail(response.content))
    except ImportError:
        return "error: Pillow is not installed (pip install Pillow)"
    except Exception as exc:  # noqa: BLE001  a bad image must not lose the recipe
        return f"error: {type(exc).__name__}: {exc}"
    return "downloaded"


def note_thumbnail(result: str, row: dict) -> None:
    kind = result.split(":")[0]
    THUMB_COUNTS[kind] = THUMB_COUNTS.get(kind, 0) + 1
    if kind == "error":
        print(f"    -> thumbnail for {row['id']}: {result}")
        if THUMB_COUNTS.get("error", 0) >= MAX_THUMB_FAILURES and not THUMB_COUNTS.get("downloaded"):
            raise Blocked(f"{MAX_THUMB_FAILURES} thumbnail downloads failed and none succeeded; stopping. Last: {result}")


def run_thumbnails(session: requests.Session, rows: dict, raw_dir: Path, out_dir: Path, *, delay: float,
                   limit: int | None, match: str | None, ids: set[int] | None, source: str = SOURCE, parse=parse_page) -> None:
    """Fills in the missing thumbnail.jpg of every recipe that has a saved page (the picture's address is read from it)."""
    todo = [r for r in select_rows(rows, lambda r: r["status"] == "ok", limit=None, match=match, ids=ids, source=source)
            if raw_path(raw_dir, r).exists() and not (out_dir / "families" / r["id"] / THUMB_NAME).exists()]
    todo = todo[:limit] if limit is not None else todo
    print(f"{len(todo)} recipe(s) without a thumbnail")
    for n, row in enumerate(todo, start=1):
        try:
            parsed = parse(gzip.decompress(raw_path(raw_dir, row).read_bytes()).decode("utf-8", errors="replace"), row["url"])
        except Exception as exc:  # noqa: BLE001  a page with no recipe data (like the two without an ingredient list) must not stop the run
            print(f"    -> skipped {row['id']} {row['url']}: {type(exc).__name__}: {exc}")
            continue
        family_dir = out_dir / "families" / row["id"]
        family_dir.mkdir(parents=True, exist_ok=True)
        result = ensure_thumbnail(session, family_dir, parsed.get("image_url"))
        note_thumbnail(result, row)
        if n % 50 == 0:
            print(f"  ...{n}/{len(todo)}")
        if result == "downloaded":
            time.sleep(delay * random.uniform(0.5, 1.0))


def run_align(rows: dict, raw_dir: Path, aligner, *, limit: int | None, match: str | None, ids: set[int] | None,
              source: str = SOURCE) -> int:
    """Re-aligns pages already saved on disk -- no network. This is the loop for improving the rules."""
    todo = [r for r in select_rows(rows, lambda r: r["status"] == "ok", limit=limit, match=match, ids=ids, source=source)
            if raw_path(raw_dir, r).exists()]
    print(f"{len(todo)} saved page(s) to align")
    failed = 0
    for n, row in enumerate(todo, start=1):
        try:
            aligner.align(row, gzip.decompress(raw_path(raw_dir, row).read_bytes()))
            if n % 100 == 0:
                print(f"  ...{n}/{len(todo)} aligned")
        except Exception as exc:  # noqa: BLE001  one odd page must not stop the batch
            failed += 1
            print(f"    -> could not align {row['id']} {row['url']}: {type(exc).__name__}: {exc}")
    return failed


def run_fetch(session: requests.Session, rows: dict, registry_path: Path, raw_dir: Path, *, delay: float,
              limit: int | None, match: str | None, ids: set[int] | None,
              keep_raw: bool = True, on_page=None, retry_wait: float = RETRY_WAIT,
              sample: int | None = None, seed: int | None = None, source: str = SOURCE) -> dict[str, int]:
    todo = select_rows(rows, needs_fetch, limit=limit, match=match, ids=ids, sample=sample, seed=seed, source=source)
    print(f"{len(todo)} page(s) to fetch")
    counts: dict[str, int] = {}
    consecutive_blocks = 0
    try:
        for n, row in enumerate(todo, start=1):
            print(f"[{n}/{len(todo)}] {row['id']} {row['url']}")
            try:
                status, page = fetch_with_retries(session, row["url"], retry_wait)
                consecutive_blocks = 0
            except Blocked as exc:
                consecutive_blocks += 1
                print(f"    -> blocked, retries exhausted ({consecutive_blocks}/{MAX_CONSECUTIVE_BLOCKS}): {exc}")
                if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                    raise Blocked(f"{MAX_CONSECUTIVE_BLOCKS} refusals in a row; the site is blocking us. "
                                  f"Stopping -- progress is saved. Wait, then rerun (or run from elsewhere).") from exc
                time.sleep(delay * 3)
                continue
            if page is not None:
                if keep_raw:
                    path = raw_path(raw_dir, row)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(gzip.compress(page))
                if on_page is not None:
                    try:
                        on_page(row, page)
                    except Blocked:
                        row["status"] = status                          # keep this page recorded, then stop the run
                        row["fetched_lastmod"], row["fetched_at"] = row["lastmod"], datetime.now().isoformat(timespec="seconds")
                        raise
                    except Exception as exc:  # noqa: BLE001  the page is fetched; a parsing problem must not lose the run
                        counts["align_error"] = counts.get("align_error", 0) + 1
                        print(f"    -> fetched, but could not align: {type(exc).__name__}: {exc}")
            row["status"] = status
            if status in ("ok", "norecipe", "gone"):
                row["fetched_lastmod"] = row["lastmod"]
                row["fetched_at"] = datetime.now().isoformat(timespec="seconds")
            counts[status] = counts.get(status, 0) + 1
            print(f"    -> {status}" + (f" ({len(page) // 1024} KB)" if page else ""))
            if n % SAVE_EVERY == 0:
                save_registry(registry_path, rows)
            time.sleep(delay * random.uniform(0.8, 1.4))
    finally:
        save_registry(registry_path, rows)   # also on Ctrl-C or a block
    return counts


# --- command line ----------------------------------------------------------------------------

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8"})
    return session


def print_status(rows: dict, source: str = SOURCE) -> None:
    mine = [r for (src, _), r in rows.items() if src == source]
    counts: dict[str, int] = {}
    for r in mine:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"{source}: {len(mine)} recipe URL(s) in the registry")
    for status in ("new", "stale", "ok", "norecipe", "gone", "error"):
        if counts.get(status):
            print(f"  {status:9} {counts[status]}")
    print(f"  to fetch next run: {sum(1 for r in mine if needs_fetch(r))}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["discover", "fetch", "align", "thumbnails", "status", "run", "renumber"])
    parser.add_argument("--base-url", default=BASE_URL, help=f"Site root (default {BASE_URL}; changed only for testing)")
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR, help="Library root (holds source_registry.csv)")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help=f"Where saved pages go (default {DEFAULT_RAW_DIR})")
    parser.add_argument("--urls-file", type=Path, help="discover from a file of URLs (one per line) instead of the sitemap")
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between page fetches (default 3.0; 1.0 gets many 403s)")
    parser.add_argument("--retry-wait", type=float, default=RETRY_WAIT,
                        help=f"fetch: seconds to wait before retrying a refused page (then double; {RETRIES} retries; "
                             f"0 = no retries; default {RETRY_WAIT:g})")
    parser.add_argument("--limit", type=int, help="fetch at most this many pages")
    parser.add_argument("--match", help="fetch only URLs containing this text (e.g. 'kimchi')")
    parser.add_argument("--ids", help="fetch only these library ids (comma-separated)")
    parser.add_argument("--sample", type=int, help="fetch: a random sample of this many of the pages still to fetch (for test batches)")
    parser.add_argument("--seed", type=int, help="with --sample: makes the random sample repeatable")
    parser.add_argument("--align", action="store_true",
                        help="fetch: also align each page into library records as it arrives (implied by `run`)")
    parser.add_argument("--no-raw", action="store_true", help="fetch: don't keep the page on disk (fetch -> align in one pass)")
    parser.add_argument("--no-thumbs", action="store_true", help="fetch --align: don't download each recipe's picture (thumbnail.jpg)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where aligned records and the ingredient master are written (default: the library). Point it "
                             "at a scratch folder while developing: the master is copied there, so the real one is untouched")
    parser.add_argument("--review-file", type=Path, default=TOOLS_DIR / "external_review_report.json",
                        help="where the alignment review report goes (default: tools/external_review_report.json)")
    args = parser.parse_args()

    registry_path = args.library_dir / "source_registry.csv"
    rows = load_registry(registry_path)
    session = make_session()
    base = args.base_url.rstrip("/")
    if args.command == "renumber":
        try:
            renumber(rows, registry_path, args.raw_dir, args.library_dir, args.out_dir or args.library_dir)
        except (RuntimeError, ValueError) as exc:
            print(f"\nERROR: {exc}")
            return EXIT_ERROR
        print_status(rows)
        return EXIT_OK
    try:
        check_ids_current([r for (src, _), r in rows.items() if src == SOURCE])
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return EXIT_ERROR
    aligner = None
    if args.command == "align" or args.align or args.command == "run":
        aligner = Aligner(args.library_dir, args.out_dir or args.library_dir, args.review_file)
    ids = {int(x) for x in args.ids.split(",")} if args.ids else None
    try:
        if args.command in ("discover", "run"):
            new, changed, total = discover(session, base, rows, args.urls_file)
            save_registry(registry_path, rows)
            print(f"Discovered {total} recipe URL(s): {new} new, {changed} changed since last fetched.")
        if args.command in ("fetch", "run"):
            def align_and_thumbnail(row: dict, page: bytes) -> None:
                info = aligner.align(row, page)
                if not args.no_thumbs:                                  # the picture is fetched with the page, only when new or changed
                    note_thumbnail(ensure_thumbnail(session, info["family_dir"], info["image_url"], info["previous_url"]), row)
            counts = run_fetch(session, rows, registry_path, args.raw_dir, delay=args.delay,
                               limit=args.limit, match=args.match, ids=ids,
                               keep_raw=not args.no_raw, on_page=align_and_thumbnail if aligner else None,
                               retry_wait=args.retry_wait, sample=args.sample, seed=args.seed)
            print("Fetched:", counts or "nothing")
        if args.command == "thumbnails":
            run_thumbnails(session, rows, args.raw_dir, args.out_dir or args.library_dir, delay=args.delay,
                           limit=args.limit, match=args.match, ids=ids)
        if THUMB_COUNTS:
            print("Thumbnails:", THUMB_COUNTS)
        if args.command == "align":
            failed = run_align(rows, args.raw_dir, aligner, limit=args.limit, match=args.match, ids=ids)
            print(f"Aligned {aligner.aligned} recipe(s)" + (f", {failed} could not be aligned" if failed else ""))
        if aligner is not None:
            review = aligner.finish()
            new_ings = review["new_ingredients"]
            print(f"Review report: {args.review_file}\n  new ingredients: {new_ings['count']} ({new_ings['undecided']} still need a "
                  f"restriction decision) | loose matches: {len(review['loose_matches'])} | optional lines moved to notes: "
                  f"{len(review['optional_lines_moved_to_notes'])} | categories without a tag: {len(review['categories_without_a_tag'])}")
        print_status(rows)
    except Blocked as exc:
        if aligner is not None:
            aligner.finish()
        print(f"\nSTOPPED: {exc}")
        print_status(rows)
        return EXIT_BLOCKED
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
