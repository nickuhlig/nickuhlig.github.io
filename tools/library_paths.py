"""
Where things live, derived from where this tools/ folder sits in the repo -- so the same scripts
work unchanged on any machine (the PC, the Raspberry Pi) instead of hardcoding a drive path.

    <repo>/tools/                              scripts, git-ignored headers file and sweep results
    <repo>/projects/free-recipe-database/      the library (families/, manifest.json, ingredient_master.json)
"""

from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
DEFAULT_LIBRARY_DIR = REPO_ROOT / "projects" / "free-recipe-database"
DEFAULT_SWEEP_DIR = TOOLS_DIR / "albertsons_sweep_results"
