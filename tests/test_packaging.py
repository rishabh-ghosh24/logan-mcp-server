"""Verify distributable artifacts and the version advertised to MCP clients."""

from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile


REPO = Path(__file__).resolve().parents[1]


def test_wheel_builds_with_query_catalogs(tmp_path):
    """A duplicate package-data rule or missing catalog breaks real installs."""
    assert find_spec("hatchling") is not None, (
        'Wheel verification requires the build backend; run: pip install -e ".[dev]"'
    )
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--disable-pip-version-check", "--wheel-dir", str(tmp_path), str(REPO)],
        text=True, capture_output=True, timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheels = list(tmp_path.glob("oci_logan_mcp-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        for name in ("starter_queries.yaml", "builtin_queries.yaml"):
            assert archive.read(f"oci_logan_mcp/data/{name}").strip()
        assert archive.read("oci_logan_mcp/__main__.py").strip()
        entry_points = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        assert "oci-logan-mcp = oci_logan_mcp.__main__:main" in archive.read(entry_points).decode()


def test_package_version_matches_distribution():
    from oci_logan_mcp import __version__

    declared = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert __version__ == version("oci-logan-mcp") == declared


def test_initialize_advertises_package_version_and_instructions():
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer

    options = OCILogAnalyticsMCPServer().server.create_initialization_options()
    assert options.server_version == version("oci-logan-mcp")
    assert options.instructions
    assert "test_connection" in options.instructions
    assert "investigate_incident" in options.instructions
