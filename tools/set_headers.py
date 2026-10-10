#!/usr/bin/env python3
"""
Writes albertsons_headers.json (git-ignored; read by albertsons_full_sweep.py and
fetch_albertsons_native_recipes.py via albertsons_auth.py) from a request copied out of your own
logged-in browser session.

HOW:
  1. On an Albertsons meal-plans-recipes page, open DevTools -> Network and click a recipe so a
     request to .../menuservice/v2/recipe?id=... fires.
  2. Right-click that request -> Copy -> Copy as cURL (bash).
  3. Run:   python set_headers.py            (then paste, press Enter, and finish with Ctrl-D on
                                              Linux/macOS or Ctrl-Z then Enter on Windows)
       or:  python set_headers.py --from-file curl.txt
       or:  pbpaste | python set_headers.py     (any command that prints the paste)

Understands -H/--header lines and -b/--cookie; a plain "Name: value" per line paste also works.
Only Authorization, Ocp-Apim-Subscription-Key and Cookie are kept. The values themselves are
never printed -- only which headers were found and how long they are.
"""

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

from albertsons_auth import headers_path

WANTED = {"authorization": "Authorization", "ocp-apim-subscription-key": "Ocp-Apim-Subscription-Key",
          "cookie": "Cookie"}


def parse_curl(text: str) -> dict[str, str]:
    """Header name -> value for -H/--header and -b/--cookie in a cURL command (bash form)."""
    text = re.sub(r"\\\r?\n", " ", text)         # bash line continuations
    text = text.replace("$'", "'")               # Chrome's $'...' quoting -> plain single quotes
    tokens = shlex.split(text)
    found: dict[str, str] = {}
    for i, token in enumerate(tokens[:-1]):
        nxt = tokens[i + 1]
        if token in ("-H", "--header"):
            name, sep, value = nxt.partition(":")
            if sep:
                found[name.strip().lower()] = value.strip()
        elif token in ("-b", "--cookie"):
            found["cookie"] = nxt.strip()
    return found


def parse_header_lines(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() in WANTED:
            found[name.strip().lower()] = value.strip()
    return found


def parse(text: str) -> dict[str, str]:
    parsed = {}
    if "curl" in text.lower().split("\n", 1)[0] or " -H " in text or " -b " in text:
        try:
            parsed = parse_curl(text)
        except ValueError:
            parsed = {}
    if not parsed:
        parsed = parse_header_lines(text)
    return {WANTED[k]: v for k, v in parsed.items() if k in WANTED and v}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-file", type=Path, help="Read the copied request from this file instead of stdin")
    parser.add_argument("--out", type=Path, default=None,
                        help=f"Where to write (default: {headers_path()})")
    args = parser.parse_args()

    if args.from_file:
        text = args.from_file.read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("Paste the copied cURL request, then finish with Ctrl-D (Linux/macOS) or Ctrl-Z + Enter (Windows):")
        text = sys.stdin.read()

    headers = parse(text)
    if not headers:
        print("No Authorization / Ocp-Apim-Subscription-Key / Cookie headers found in that paste -- "
              "use 'Copy as cURL (bash)'.", file=sys.stderr)
        return 1

    out = args.out or headers_path()
    out.write_text(json.dumps(headers, indent=2), encoding="utf-8")
    try:
        os.chmod(out, 0o600)  # owner-only where the OS supports it
    except OSError:
        pass
    print(f"Wrote {out}")
    for canonical in WANTED.values():
        state = f"{len(headers[canonical])} chars" if canonical in headers else "NOT FOUND"
        print(f"  {canonical}: {state}")
    missing = [n for n in ("Authorization", "Ocp-Apim-Subscription-Key") if n not in headers]
    if missing:
        print(f"WARNING: missing {', '.join(missing)} -- the fetch step needs both.", file=sys.stderr)
        return 1
    if "Cookie" not in headers:
        print("Note: no Cookie header -- fetch only needs the other two, but the sweep uses the cookie too.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
