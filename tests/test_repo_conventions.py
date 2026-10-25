"""Repo-hygiene guards that scan tracked files for convention drift.

These tests run fully offline (no OCI connectivity) and exist to stop classes
of mistakes from silently re-entering the repo.
"""

import subprocess
from pathlib import Path
import pytest

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

INTERNAL_HOST_PATTERNS = [r"130\.162\.", r"emdemo", r"omcinternal", r"assurance4emea"]
REMOVED_PATH_PATTERNS = [r"windows-setup/"]


def _git_grep(patterns, excludes=(), root=REPO_ROOT):
    result = subprocess.run(
        ["git", "grep", "-InEi", "|".join(patterns), "--", ".",
         ":!tests/test_repo_conventions.py", *excludes],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def test_no_internal_hosts_in_tracked_files():
    matches = _git_grep(INTERNAL_HOST_PATTERNS)
    assert not matches, "Internal hosts in tracked files:\n" + matches


def test_no_removed_installer_references_outside_review_history():
    matches = _git_grep(REMOVED_PATH_PATTERNS, [":!docs/reviews/"])
    assert not matches, "References to the removed installer:\n" + matches


def test_pattern_scan_finds_a_tracked_fixture(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "fixture.txt").write_text("Fixture-Marker\n")
    subprocess.run(["git", "add", "fixture.txt"], cwd=tmp_path, check=True)
    assert "fixture.txt:1:Fixture-Marker" in _git_grep(["fixture-marker"], root=tmp_path)


def test_pattern_scan_rejects_git_errors(tmp_path):
    with pytest.raises(RuntimeError, match="git grep failed"):
        _git_grep(["anything"], root=tmp_path)


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


def test_setup_script_python_floor_matches_pyproject():
    """scripts/setup_oel9.sh must refuse the interpreters the package refuses and try the newer ones.

    Accepting Python 3.10 lets the script build a venv the install then rejects.
    """
    import re
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    floor = int(re.search(r'requires-python\s*=\s*">=3\.(\d+)"', pyproject).group(1))
    script = (REPO_ROOT / "scripts" / "setup_oel9.sh").read_text()

    assert f"3.{floor - 1}" not in script, f"setup script still mentions Python 3.{floor - 1}"
    assert f'-ge {floor} ]' in script, f"setup script does not enforce the 3.{floor} floor"
    for minor in range(floor, 15):
        assert f"python3.{minor}" in script, f"setup script never tries python3.{minor}"
