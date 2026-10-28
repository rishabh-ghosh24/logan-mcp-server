# Development and verification

[Back to the README](../README.md)

Use the `venv/` environment and install the development dependencies:

```bash
pip install -e ".[dev]"
```

Run the regression suite with a temporary home and Matplotlib cache:

```bash
python - <<'PYTEST'
import os
from pathlib import Path
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix="logan-tests-") as scratch:
    environment = os.environ.copy()
    environment["HOME"] = scratch
    environment["MPLCONFIGDIR"] = str(Path(scratch) / "matplotlib")
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        env=environment, check=True,
    )
PYTEST
```

The suite builds a real wheel and speaks MCP stdio to subprocesses using a fake OCI backend. CI is configured for Linux and macOS with Python 3.11, 3.12, 3.13 and 3.14. Live OCI integration checks are separate and require configured access:

```bash
python run_tests.py
```
