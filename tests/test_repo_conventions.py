"""Repo-hygiene guards that scan tracked files for convention drift.

These tests run fully offline (no OCI connectivity) and exist to stop classes
of mistakes from silently re-entering the repo.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The Python virtualenv is named `venv` (no leading dot) everywhere a path is
# referenced: README.md, the setup/run scripts, run_tests.py, and the MCP client
# configs. A stray dotted `.venv/` path silently breaks deployments whose venv is
# `venv/` -- the launcher dies with `bash: .../.venv/bin/oci-logan-mcp: No such
# file or directory`. Keep everything on the `venv/` convention.
#
# Files/dirs allowed to mention `.venv/`:
#   - .gitignore intentionally ignores BOTH `venv/` and `.venv/`.
#   - docs/ holds frozen, point-in-time plan documents (historical record).
#   - this guard file necessarily contains the search pattern itself.
ALLOWED_FILES = {".gitignore", "tests/test_repo_conventions.py"}
ALLOWED_PREFIXES = ("docs/",)


def _dot_venv_references():
    """Return tracked-file lines (path:line:content) that reference `.venv/`."""
    result = subprocess.run(
        ["git", "grep", "-In", r"\.venv/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    offenders = []
    for line in result.stdout.splitlines():
        path = line.split(":", 1)[0]
        if path in ALLOWED_FILES:
            continue
        if any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            continue
        offenders.append(line)
    return offenders


def test_no_dot_venv_in_live_files():
    """Live scripts/docs/configs must use `venv/` (no dot), never `.venv/`."""
    offenders = _dot_venv_references()
    assert not offenders, (
        "Found dotted `.venv/` references; the repo convention is `venv` (no "
        "leading dot) to match README.md and scripts/. Fix these to `venv/`:\n  "
        + "\n  ".join(offenders)
    )
