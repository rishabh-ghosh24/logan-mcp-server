# src/oci_logan_mcp/file_lock.py
"""Thread-safe and process-safe file locking with atomic YAML I/O."""
from __future__ import annotations

import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows — thread lock only

# Track which lock paths the current thread already holds an flock on,
# so re-entrant calls (via RLock) skip the flock to avoid macOS deadlocks.
_thread_local = threading.local()


@contextmanager
def locked_file(lock_path: Path, thread_lock: threading.RLock) -> Iterator[None]:
    """Acquire thread lock + file lock for safe concurrent access."""
    with thread_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch(exist_ok=True)
        resolved = str(lock_path.resolve())
        held: set[str] = getattr(_thread_local, "held_flocks", None) or set()
        _thread_local.held_flocks = held
        already_held = resolved in held
        if already_held:
            # Re-entrant call — RLock covers us, skip flock
            yield
        else:
            with lock_path.open("a+", encoding="utf-8") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                held.add(resolved)
                try:
                    yield
                finally:
                    held.discard(resolved)
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_yaml_write(path: Path, data: Any) -> None:
    """Write YAML atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        delete=False, prefix=f".{path.stem}.", suffix=".tmp",
    ) as handle:
        yaml.dump(data, handle, default_flow_style=False, sort_keys=False)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def atomic_yaml_read(path: Path, default: Any = None) -> Any:
    """Read YAML file, returning default if missing or corrupt."""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if data is not None else default
    except Exception:
        return default
