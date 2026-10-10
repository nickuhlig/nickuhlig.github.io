"""
Where the captured Albertsons session headers live, so no script ever holds real values.

The headers sit in albertsons_headers.json next to the scripts (git-ignored -- it holds a
logged-in session, so it must never be committed). Override the location with the
ALBERTSONS_HEADERS_FILE environment variable. Create or refresh it with set_headers.py, which
reads a DevTools "Copy as cURL" paste.

FILE SHAPE
    {"Authorization": "Bearer ...", "Ocp-Apim-Subscription-Key": "...", "Cookie": "..."}

albertsons_full_sweep.py uses all three; fetch_albertsons_native_recipes.py only the first two.
A missing file, or missing keys, just yields fewer headers -- each script's own "HEADERS is
empty" check then stops it with a clear message.

AuthExpired is raised when Albertsons answers 401/403, i.e. the captured session no longer works.
It deliberately is NOT a requests exception: the sweep/fetch loops catch RequestException to skip
one flaky request, and a rejected session must instead stop the whole run (otherwise every
remaining request fails the same way and the run ends "successfully" with partial data).
"""

import json
import os
from pathlib import Path

HEADERS_FILENAME = "albertsons_headers.json"


class AuthExpired(Exception):
    """Albertsons rejected the captured session headers (HTTP 401/403)."""


def headers_path() -> Path:
    override = os.environ.get("ALBERTSONS_HEADERS_FILE")
    return Path(override) if override else Path(__file__).with_name(HEADERS_FILENAME)


def load_headers(names: tuple[str, ...]) -> dict[str, str]:
    """The non-empty headers among `names`, from the headers file ({} when it doesn't exist)."""
    path = headers_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: data[name] for name in names if isinstance(data.get(name), str) and data[name].strip()}


def check_response(resp, what: str) -> None:
    """Raise AuthExpired if Albertsons rejected the session; otherwise do nothing."""
    if resp.status_code in (401, 403):
        raise AuthExpired(f"{what}: HTTP {resp.status_code} -- Albertsons rejected the captured session "
                          f"(expired or blocked). Refresh {HEADERS_FILENAME} with set_headers.py.")
