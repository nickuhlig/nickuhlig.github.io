#!/usr/bin/env python3
"""
One unattended refresh cycle for the recipe library -- what the Raspberry Pi's timer/cron runs
weekly. Does, in order:

  1. takes a lock (a second run while one is going just exits) and logs to tools/logs/
  2. git pull --rebase --autostash origin master          (skipped with --no-git)
  3. the Albertsons pipeline, in FULL mode: sweep (with both restriction-discovery passes -- they
     are 2/3 of what a sweep finds, so a plain sweep would shrink the file), diff, fetch, then the
     local steps (ingredients, restrictions, cookbooks). The network stages need the session
     headers in albertsons_headers.json (see set_headers.py); without them, or if Albertsons
     rejects them mid-run, or any network stage fails, the run does NOT stop: the local steps
     still run on the existing sweep and the result is flagged (exit code 2 + a notification).
  4. regenerates manifest.json, refusing to replace the old one if it came out smaller
  5. commits ONLY projects/free-recipe-database and pushes to origin master
     (--no-push: commit but don't push)

EXIT CODES   0 everything ran   2 finished and pushed, but the network stages were skipped/failed
             1 failed -- nothing was committed or pushed (see the log)

NOTIFICATIONS  set NTFY_URL (e.g. https://ntfy.sh/your-secret-topic) to get a push message on any
non-zero exit; NOTIFY_ON_SUCCESS=1 to also get one on success. Everything is logged regardless.

Usage:
    python weekly_refresh.py                      # the real thing
    python weekly_refresh.py --local-only         # skip the network stages on purpose
    python weekly_refresh.py --no-git             # don't touch git at all (testing)
    python weekly_refresh.py --no-push            # commit locally, don't push

Needs Python 3.10+ and `pip install requests` (see requirements.txt).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

MIN_PYTHON = (3, 10)
if sys.version_info < MIN_PYTHON:
    raise SystemExit(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (this is {sys.version.split()[0]}).")

import albertsons_pipeline  # noqa: E402  (after the version check, so old Pythons get the message above)
import mealime_generate_manifest  # noqa: E402
from albertsons_auth import AuthExpired, load_headers  # noqa: E402
from library_paths import DEFAULT_LIBRARY_DIR, DEFAULT_SWEEP_DIR, REPO_ROOT, TOOLS_DIR  # noqa: E402

REMOTE = "origin"
BRANCH = "master"
LOCK_STALE_SECONDS = 12 * 3600
MANIFEST_MIN_FRACTION = 0.99  # a new manifest with fewer than this fraction of the old entries is rejected
EXIT_OK, EXIT_FAILED, EXIT_PARTIAL = 0, 1, 2


class Tee:
    """Mirrors everything printed (including the pipeline stages' own output) into the log file."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            try:
                stream.write(text)
            except UnicodeEncodeError:  # e.g. a Windows console that can't show "sauté"
                encoding = getattr(stream, "encoding", None) or "ascii"
                stream.write(text.encode(encoding, "replace").decode(encoding))
            stream.flush()
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        return False


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}")


def notify(title: str, body: str, priority: str = "default") -> None:
    url = os.environ.get("NTFY_URL")
    if not url:
        return
    try:
        request = urllib.request.Request(url, data=body.encode("utf-8"),
                                         headers={"Title": title.encode("ascii", "replace").decode(),
                                                  "Priority": priority})
        urllib.request.urlopen(request, timeout=15).read()
    except Exception as exc:  # a failed notification must never fail the run
        log(f"(could not send notification: {exc})")


@contextmanager
def single_instance(lock_path: Path):
    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            raise SystemExit(f"Another refresh seems to be running (lock {lock_path.name} is {age / 60:.0f} min "
                             f"old) -- exiting. Delete the lock if that run died.")
        log(f"Removing stale lock ({age / 3600:.1f} h old).")
        lock_path.unlink()
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as handle:
        handle.write(str(os.getpid()))
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


class Git:
    def __init__(self, repo: Path):
        self.repo = repo
        self.exe = os.environ.get("GIT_EXE") or shutil.which("git")
        if not self.exe:
            raise RuntimeError("git not found (install it, or set GIT_EXE)")

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run([self.exe, "-C", str(self.repo), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): "
                               f"{(proc.stderr or proc.stdout).strip()[:600]}")
        return proc

    def identity_args(self) -> list[str]:
        if self.run("config", "user.email", check=False).stdout.strip():
            return []
        return ["-c", "user.name=Weekly refresh", "-c", "user.email=noreply@users.noreply.github.com"]


def manifest_entry_count(path: Path) -> int:
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get("entries", {}))
    except (OSError, json.JSONDecodeError):
        return 0


def rebuild_manifest(library: Path) -> tuple[int, int]:
    """Regenerate manifest.json; refuses (RuntimeError) to replace a larger one. Returns (old, new) counts."""
    path = library / "manifest.json"
    old = manifest_entry_count(path)
    manifest = mealime_generate_manifest.build_manifest(library)
    new = len(manifest["entries"])
    if old and new < old * MANIFEST_MIN_FRACTION:
        raise RuntimeError(f"new manifest has {new} entries vs {old} in the current one "
                           f"(< {MANIFEST_MIN_FRACTION:.0%}) -- refusing to replace it")
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return old, new


def run_pipeline(library: Path, sweep_dir: Path, *, network: bool) -> None:
    albertsons_pipeline.run(
        library, sweep_dir,
        do_sweep=network, skip_diff=not network, skip_fetch=not network,
        skip_ingredients=False, skip_restrictions=False, skip_cookbooks=False,
        restriction_discovery=network, pair_restriction_discovery=network,
        overwrite_fetch=False, dry_run=False,
    )


def commit_and_push(git: Git, library: Path, *, push: bool, new_manifest_count: int, old_manifest_count: int) -> str:
    rel = library.resolve().relative_to(git.repo.resolve()).as_posix()
    git.run("add", "-A", "--", rel)
    status = git.run("diff", "--cached", "--name-status", "--", rel).stdout.splitlines()
    if not status:
        return "nothing changed -- no commit"
    added = sum(1 for line in status if line.startswith("A") and "/families/" in line and line.endswith(".json")
                and not line.endswith("alt_variants.json"))
    modified = sum(1 for line in status if line.startswith("M"))
    message = (f"Weekly refresh {date.today():%Y-%m-%d}: +{added} recipe file(s), {modified} modified, "
               f"manifest {old_manifest_count} -> {new_manifest_count}")
    git.run(*git.identity_args(), "commit", "-m", message)
    summary = f"committed: {message}"
    if not push:
        return summary + " (not pushed: --no-push)"
    pushed = git.run("push", REMOTE, BRANCH, check=False)
    if pushed.returncode != 0:  # most likely someone pushed meanwhile -- rebase onto it and retry once
        log(f"push rejected ({pushed.stderr.strip()[:200]}); pulling and retrying once")
        git.run("pull", "--rebase", "--autostash", REMOTE, BRANCH)
        git.run("push", REMOTE, BRANCH)
    return summary + " and pushed"


def refresh(args) -> int:
    library, sweep_dir = args.library_dir.resolve(), args.sweep_dir.resolve()
    git = None if args.no_git else Git(args.repo_dir)
    problems: list[str] = []

    if git:
        branch = git.run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != BRANCH:
            raise RuntimeError(f"repo is on branch {branch!r}, expected {BRANCH!r} -- not touching it")
        log(f"git pull --rebase --autostash {REMOTE} {BRANCH}")
        pulled = git.run("pull", "--rebase", "--autostash", REMOTE, BRANCH, check=False)
        if pulled.returncode != 0:
            git.run("rebase", "--abort", check=False)
            raise RuntimeError(f"git pull failed: {(pulled.stderr or pulled.stdout).strip()[:600]}")

    have_headers = len(load_headers(("Authorization", "Ocp-Apim-Subscription-Key"))) == 2
    network = have_headers and not args.local_only
    if not args.local_only and not have_headers:
        problems.append("network stages skipped: no session headers (albertsons_headers.json missing or incomplete)")
        log(problems[-1])

    if network:
        log("Pipeline: full run (sweep + discovery passes, diff, fetch, local steps)")
        try:
            run_pipeline(library, sweep_dir, network=True)
        except AuthExpired as exc:
            problems.append(f"Albertsons rejected the session headers: {exc}")
        except SystemExit as exc:
            problems.append(f"network stage stopped: {exc}")
        except Exception as exc:  # noqa: BLE001  one flaky network failure shouldn't block the local steps
            problems.append(f"network stage failed: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        if problems:
            log(problems[-1])
            log("Continuing with the local steps on the existing sweep.")
            network = False
    if not network:
        log("Pipeline: local steps only (ingredients, restrictions, cookbooks)")
        run_pipeline(library, sweep_dir, network=False)

    log("Regenerating manifest.json")
    old_count, new_count = rebuild_manifest(library)
    log(f"manifest: {old_count} -> {new_count} entries")

    if git:
        log(commit_and_push(git, library, push=not args.no_push, new_manifest_count=new_count,
                            old_manifest_count=old_count))
    else:
        log("git skipped (--no-git)")

    if problems:
        message = "Refresh finished with problems:\n- " + "\n- ".join(problems)
        log(message)
        notify("Recipe refresh: needs attention", message, priority="high")
        return EXIT_PARTIAL
    if os.environ.get("NOTIFY_ON_SUCCESS"):
        notify("Recipe refresh OK", f"manifest {old_count} -> {new_count} entries")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-dir", type=Path, default=REPO_ROOT, help=f"Git repo root (default: {REPO_ROOT})")
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR,
                        help=f"Library to refresh (default: {DEFAULT_LIBRARY_DIR})")
    parser.add_argument("--sweep-dir", type=Path, default=DEFAULT_SWEEP_DIR,
                        help=f"Where sweep results are kept (default: {DEFAULT_SWEEP_DIR})")
    parser.add_argument("--local-only", action="store_true", help="Skip the network stages (sweep + fetch)")
    parser.add_argument("--no-git", action="store_true", help="Don't pull, commit or push")
    parser.add_argument("--no-push", action="store_true", help="Commit but don't push")
    args = parser.parse_args()

    log_dir = TOOLS_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = open(log_dir / f"weekly_refresh_{date.today():%Y-%m-%d}.log", "a", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_file)
    sys.stderr = Tee(sys.__stderr__, log_file)

    log(f"=== weekly refresh starting (library: {args.library_dir}) ===")
    try:
        with single_instance(TOOLS_DIR / ".weekly_refresh.lock"):
            code = refresh(args)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            log(f"stopped: {exc}")
        code = EXIT_FAILED if exc.code not in (None, 0) else EXIT_OK
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        log(f"FAILED: {type(exc).__name__}: {exc}")
        notify("Recipe refresh FAILED", f"{type(exc).__name__}: {exc}", priority="urgent")
        code = EXIT_FAILED
    log(f"=== weekly refresh finished, exit code {code} ===")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
