# Per-User Secrets & Audit Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single shared env-var secret with per-user scrypt-hashed secrets and add a shared audit log for all destructive operations.

**Architecture:** `SecretStore` handles hashing/verifying per-user secrets via `hashlib.scrypt`. `AuditLogger` writes JSON-lines to a shared log with file locking. `ConfirmationManager` is updated to delegate secret verification to `SecretStore`. The `OCI_LA_CONFIRMATION_SECRET` env var is removed entirely. First-run interactive prompt and `--reset-secret` CLI flag handle secret lifecycle.

**Tech Stack:** Python 3.10+, `hashlib.scrypt` (stdlib), existing `file_lock.py` patterns, YAML for hash storage, JSON-lines for audit log.

**Spec:** `docs/superpowers/specs/2026-04-06-per-user-secrets-audit-log-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/oci_logan_mcp/secret_store.py` | Hash, verify, read/write per-user secret files |
| Create | `src/oci_logan_mcp/audit.py` | Shared JSON-lines audit log with file locking and rotation |
| Create | `tests/test_secret_store.py` | Unit tests for SecretStore |
| Create | `tests/test_audit.py` | Unit tests for AuditLogger |
| Modify | `src/oci_logan_mcp/confirmation.py:39-131` | Delegate to SecretStore; update instructions string |
| Modify | `src/oci_logan_mcp/handlers.py:30-169` | Accept SecretStore + AuditLogger; audit all guarded outcomes |
| Modify | `src/oci_logan_mcp/server.py:184-202` | Init SecretStore + AuditLogger; first-run prompt |
| Modify | `src/oci_logan_mcp/__main__.py:15-61` | Add --reset-secret flag with identity check |
| Modify | `src/oci_logan_mcp/config.py:286-289` | Remove OCI_LA_CONFIRMATION_SECRET; add deprecation warning |
| Modify | `src/oci_logan_mcp/tools.py` | Update 6 guarded tool descriptions |
| Modify | `tests/test_confirmation.py` | Update for SecretStore-based verification |
| Modify | `tests/test_handlers.py` | Update fixtures for SecretStore + AuditLogger |
| Modify | `tests/test_config.py` | Remove env var test; add deprecation warning test |
| Modify | `README.md` | Update documentation |

---

### Task 1: Create SecretStore

**Files:**
- Create: `src/oci_logan_mcp/secret_store.py`
- Create: `tests/test_secret_store.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_secret_store.py
"""Tests for per-user secret hashing and verification."""

import os
import stat
import pytest

from oci_logan_mcp.secret_store import SecretStore


class TestSecretStore:
    @pytest.fixture
    def store(self, tmp_path):
        return SecretStore(tmp_path / "confirmation_secret.hash")

    def test_has_secret_false_initially(self, store):
        assert store.has_secret() is False

    def test_set_and_verify(self, store):
        store.set_secret("my-secret-123")
        assert store.has_secret() is True
        assert store.verify_secret("my-secret-123") is True

    def test_wrong_secret_rejected(self, store):
        store.set_secret("correct-secret")
        assert store.verify_secret("wrong-secret") is False

    def test_minimum_length_enforced(self, store):
        with pytest.raises(ValueError, match="at least 8 characters"):
            store.set_secret("short")

    def test_file_permissions(self, store):
        store.set_secret("my-secret-123")
        mode = os.stat(store._path).st_mode & 0o777
        assert mode == 0o600

    def test_hash_file_is_yaml_with_scrypt_params(self, store):
        import yaml
        store.set_secret("my-secret-123")
        with open(store._path) as f:
            data = yaml.safe_load(f)
        assert data["algorithm"] == "scrypt"
        assert "salt" in data
        assert "hash" in data
        assert data["n"] == 16384
        assert data["r"] == 8
        assert data["p"] == 1

    def test_different_salts_per_set(self, store):
        import yaml
        store.set_secret("same-secret-1")
        with open(store._path) as f:
            salt1 = yaml.safe_load(f)["salt"]
        store.set_secret("same-secret-2")
        with open(store._path) as f:
            salt2 = yaml.safe_load(f)["salt"]
        assert salt1 != salt2

    def test_verify_reads_params_from_file(self, store):
        """Forward compat: verification uses stored params, not hardcoded."""
        store.set_secret("my-secret-123")
        # Tamper with n to prove it's read from file
        import yaml
        with open(store._path) as f:
            data = yaml.safe_load(f)
        data["n"] = 1024  # weaker, different hash
        with open(store._path, "w") as f:
            yaml.dump(data, f)
        # Should fail because hash was computed with n=16384
        assert store.verify_secret("my-secret-123") is False

    def test_corrupted_file_returns_false(self, store):
        store._path.parent.mkdir(parents=True, exist_ok=True)
        store._path.write_text("garbage: [not valid yaml")
        assert store.has_secret() is True  # file exists
        assert store.verify_secret("anything") is False

    def test_missing_fields_returns_false(self, store):
        import yaml
        store._path.parent.mkdir(parents=True, exist_ok=True)
        with open(store._path, "w") as f:
            yaml.dump({"algorithm": "scrypt"}, f)  # missing salt/hash
        assert store.verify_secret("anything") is False

    def test_is_valid_true_after_set(self, store):
        store.set_secret("my-secret-123")
        assert store.is_valid() is True

    def test_is_valid_false_when_no_file(self, store):
        assert store.is_valid() is False

    def test_is_valid_false_when_corrupted(self, store):
        store._path.parent.mkdir(parents=True, exist_ok=True)
        store._path.write_text("garbage: [not valid")
        assert store.is_valid() is False

    def test_is_valid_false_when_missing_fields(self, store):
        import yaml
        store._path.parent.mkdir(parents=True, exist_ok=True)
        with open(store._path, "w") as f:
            yaml.dump({"algorithm": "scrypt"}, f)
        assert store.is_valid() is False
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `/usr/local/bin/python3.14 -m pytest tests/test_secret_store.py -v`
Expected: `ModuleNotFoundError: No module named 'oci_logan_mcp.secret_store'`

- [ ] **Step 3: Implement SecretStore**

```python
# src/oci_logan_mcp/secret_store.py
"""Per-user confirmation secret storage with scrypt hashing.

Secrets are hashed with hashlib.scrypt and stored as YAML.
Plaintext is never stored. File permissions are 0600.
"""

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 32
_MIN_SECRET_LENGTH = 8


class SecretStore:
    """Manages per-user confirmation secret hashing and verification."""

    def __init__(self, secret_path: Path) -> None:
        self._path = secret_path

    def has_secret(self) -> bool:
        """Return True if the hash file exists."""
        return self._path.is_file()

    def is_valid(self) -> bool:
        """Return True if hash file exists and has valid structure."""
        if not self._path.is_file():
            return False
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return bool(
                data
                and all(k in data for k in ("salt", "hash", "n", "r", "p"))
            )
        except Exception:
            return False

    def set_secret(self, plaintext: str) -> None:
        """Hash and store a new secret. Enforces minimum length."""
        if len(plaintext) < _MIN_SECRET_LENGTH:
            raise ValueError(
                f"Secret must be at least {_MIN_SECRET_LENGTH} characters"
            )

        salt = secrets.token_bytes(_SALT_BYTES)
        hash_bytes = hashlib.scrypt(
            plaintext.encode(),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
        )

        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "algorithm": "scrypt",
            "salt": salt.hex(),
            "hash": hash_bytes.hex(),
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
        }
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False)
        os.chmod(self._path, 0o600)

        logger.info("Confirmation secret set (hash stored at %s)", self._path)

    def verify_secret(self, plaintext: str) -> bool:
        """Verify a plaintext secret against the stored hash."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not data or not all(k in data for k in ("salt", "hash", "n", "r", "p")):
                logger.warning("Secret hash file missing required fields")
                return False

            salt = bytes.fromhex(data["salt"])
            expected = bytes.fromhex(data["hash"])
            actual = hashlib.scrypt(
                plaintext.encode(),
                salt=salt,
                n=data["n"],
                r=data["r"],
                p=data["p"],
            )
            return hmac.compare_digest(actual, expected)
        except Exception:
            logger.warning("Failed to verify secret", exc_info=True)
            return False
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `/usr/local/bin/python3.14 -m pytest tests/test_secret_store.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/secret_store.py tests/test_secret_store.py
git commit -m "feat: add SecretStore with scrypt hashing for per-user secrets"
```

---

### Task 2: Create AuditLogger

**Files:**
- Create: `src/oci_logan_mcp/audit.py`
- Create: `tests/test_audit.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_audit.py
"""Tests for shared audit logger."""

import json
import os
import stat
import pytest

from oci_logan_mcp.audit import AuditLogger


class TestAuditLogger:
    @pytest.fixture
    def log_dir(self, tmp_path):
        return tmp_path / "logs"

    @pytest.fixture
    def logger(self, log_dir):
        return AuditLogger(log_dir)

    def test_creates_log_dir(self, logger, log_dir):
        assert log_dir.is_dir()

    def test_log_creates_audit_file(self, logger, log_dir):
        logger.log(user="alice", tool="delete_alert",
                   args={"alert_id": "a1"}, outcome="confirmed")
        assert (log_dir / "audit.log").is_file()

    def test_log_format_is_json_lines(self, logger, log_dir):
        logger.log(user="alice", tool="delete_alert",
                   args={"alert_id": "a1"}, outcome="confirmed")
        logger.log(user="bob", tool="delete_dashboard",
                   args={"dashboard_id": "d1"}, outcome="executed",
                   result_summary="deleted dashboard")
        lines = (log_dir / "audit.log").read_text().strip().split("\n")
        assert len(lines) == 2
        entry = json.loads(lines[0])
        assert entry["user"] == "alice"
        assert entry["tool"] == "delete_alert"
        assert entry["args"] == {"alert_id": "a1"}
        assert entry["outcome"] == "confirmed"
        assert "timestamp" in entry
        assert "pid" in entry

    def test_result_summary_and_error_fields(self, logger, log_dir):
        logger.log(user="bob", tool="delete_alert",
                   args={"alert_id": "a1"}, outcome="execution_failed",
                   error="connection timeout")
        entry = json.loads((log_dir / "audit.log").read_text().strip())
        assert entry["error"] == "connection timeout"

    def test_strips_confirmation_fields(self, logger, log_dir):
        logger.log(user="alice", tool="delete_alert",
                   args={"alert_id": "a1", "confirmation_token": "tok123",
                         "confirmation_secret": "sec456"},
                   outcome="confirmed")
        entry = json.loads((log_dir / "audit.log").read_text().strip())
        assert "confirmation_token" not in entry["args"]
        assert "confirmation_secret" not in entry["args"]
        assert entry["args"] == {"alert_id": "a1"}

    def test_timestamp_is_utc(self, logger, log_dir):
        logger.log(user="alice", tool="delete_alert",
                   args={}, outcome="confirmed")
        entry = json.loads((log_dir / "audit.log").read_text().strip())
        assert entry["timestamp"].endswith("Z")

    def test_secret_management_events(self, logger, log_dir):
        logger.log(user="alice", tool="__secret_management",
                   args={}, outcome="secret_set")
        entry = json.loads((log_dir / "audit.log").read_text().strip())
        assert entry["tool"] == "__secret_management"
        assert entry["outcome"] == "secret_set"

    def test_rotation_at_10mb(self, log_dir):
        audit_logger = AuditLogger(log_dir)
        audit_file = log_dir / "audit.log"
        # Create a file just under 10MB
        audit_file.write_text("x" * (10 * 1024 * 1024))
        audit_logger.log(user="alice", tool="delete_alert",
                         args={}, outcome="confirmed")
        assert (log_dir / "audit.log.1").is_file()
        # New audit.log should have just the one new entry
        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_rotation_shifts_backups(self, log_dir):
        audit_logger = AuditLogger(log_dir)
        # Create existing backups
        for i in range(1, 4):
            (log_dir / f"audit.log.{i}").write_text(f"backup {i}")
        # Create oversized main file
        (log_dir / "audit.log").write_text("x" * (10 * 1024 * 1024))
        audit_logger.log(user="alice", tool="delete_alert",
                         args={}, outcome="confirmed")
        assert (log_dir / "audit.log.4").read_text() == "backup 3"
        assert (log_dir / "audit.log.1").read_text().startswith("x")

    def test_audit_file_permissions(self, logger, log_dir):
        logger.log(user="alice", tool="delete_alert",
                   args={}, outcome="confirmed")
        mode = os.stat(log_dir / "audit.log").st_mode & 0o777
        assert mode == 0o640

    def test_log_dir_permissions(self, log_dir):
        AuditLogger(log_dir)
        mode = os.stat(log_dir).st_mode & 0o777
        assert mode == 0o750

    def test_rotation_max_5_backups(self, log_dir):
        audit_logger = AuditLogger(log_dir)
        for i in range(1, 6):
            (log_dir / f"audit.log.{i}").write_text(f"backup {i}")
        (log_dir / "audit.log").write_text("x" * (10 * 1024 * 1024))
        audit_logger.log(user="alice", tool="delete_alert",
                         args={}, outcome="confirmed")
        assert not (log_dir / "audit.log.6").exists()
        assert (log_dir / "audit.log.5").exists()
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `/usr/local/bin/python3.14 -m pytest tests/test_audit.py -v`
Expected: `ModuleNotFoundError: No module named 'oci_logan_mcp.audit'`

- [ ] **Step 3: Implement AuditLogger**

```python
# src/oci_logan_mcp/audit.py
"""Shared audit log for destructive MCP operations.

Appends JSON-lines to ~/.oci-logan-mcp/logs/audit.log with file locking.
All timestamps are UTC. Confirmation secrets are never logged.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .file_lock import locked_file

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
_MAX_BACKUPS = 5
_AUDIT_FILENAME = "audit.log"
_STRIP_KEYS = frozenset({"confirmation_token", "confirmation_secret"})


class AuditLogger:
    """Append-only JSON-lines audit log with file locking and rotation."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._log_dir, 0o750)
        except OSError:
            pass  # may not own the directory
        self._log_path = self._log_dir / _AUDIT_FILENAME
        self._lock_path = self._log_dir / "audit.lock"
        self._thread_lock = threading.RLock()

    def log(
        self,
        user: str,
        tool: str,
        args: Dict[str, Any],
        outcome: str,
        result_summary: str = "",
        error: str = "",
    ) -> None:
        """Append one audit entry. Thread-safe and process-safe."""
        clean_args = {k: v for k, v in args.items() if k not in _STRIP_KEYS}

        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user": user,
            "pid": os.getpid(),
            "tool": tool,
            "args": clean_args,
            "outcome": outcome,
        }
        if result_summary:
            entry["result_summary"] = result_summary
        if error:
            entry["error"] = error

        line = json.dumps(entry, separators=(",", ":")) + "\n"

        with locked_file(self._lock_path, self._thread_lock):
            self._rotate_if_needed()
            new_file = not self._log_path.is_file()
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            if new_file:
                try:
                    os.chmod(self._log_path, 0o640)
                except OSError:
                    pass

    def _rotate_if_needed(self) -> None:
        """Rotate audit.log if it exceeds _MAX_FILE_SIZE."""
        if not self._log_path.is_file():
            return
        try:
            if self._log_path.stat().st_size < _MAX_FILE_SIZE:
                return
        except OSError:
            return

        # Shift existing backups up: .4 -> .5, .3 -> .4, etc.
        for i in range(_MAX_BACKUPS, 0, -1):
            src = self._log_dir / f"{_AUDIT_FILENAME}.{i}"
            dst = self._log_dir / f"{_AUDIT_FILENAME}.{i + 1}"
            if src.is_file():
                if i == _MAX_BACKUPS:
                    src.unlink()  # drop oldest
                else:
                    src.rename(dst)

        # Move current log to .1
        self._log_path.rename(self._log_dir / f"{_AUDIT_FILENAME}.1")
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `/usr/local/bin/python3.14 -m pytest tests/test_audit.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/audit.py tests/test_audit.py
git commit -m "feat: add shared AuditLogger with JSON-lines, file locking, and rotation"
```

---

### Task 3: Update ConfirmationManager to use SecretStore

**Files:**
- Modify: `src/oci_logan_mcp/confirmation.py:39-131`
- Modify: `tests/test_confirmation.py`

- [ ] **Step 1: Update tests for SecretStore-based verification**

Replace the existing `manager` and `manager_no_secret` fixtures and update all tests to use `SecretStore` instead of a plaintext secret string. Key changes:

```python
# At top of tests/test_confirmation.py, add:
from oci_logan_mcp.secret_store import SecretStore

# Replace fixtures:
@pytest.fixture
def secret_store(self, tmp_path):
    store = SecretStore(tmp_path / "confirmation_secret.hash")
    store.set_secret("test-secret")
    return store

@pytest.fixture
def manager(self, secret_store):
    return ConfirmationManager(secret_store=secret_store, token_expiry_seconds=300)

@pytest.fixture
def manager_no_secret(self, tmp_path):
    store = SecretStore(tmp_path / "no_secret.hash")  # no set_secret called
    return ConfirmationManager(secret_store=store, token_expiry_seconds=300)
```

All existing test assertions stay the same — `validate_confirmation` still takes a plaintext `secret` parameter, but now internally delegates to `SecretStore.verify_secret()`.

- [ ] **Step 2: Run tests — expect FAIL**

Run: `/usr/local/bin/python3.14 -m pytest tests/test_confirmation.py -v`
Expected: FAIL — `ConfirmationManager.__init__` still expects `secret: str`

- [ ] **Step 3: Update ConfirmationManager**

In `src/oci_logan_mcp/confirmation.py`:

1. Update module docstring: replace "env-only secret" with "per-user hashed secret"
2. Update class docstring: replace "Factor 2: Env-only secret" with "Factor 2: Per-user secret verified via SecretStore"
3. Update `__init__`:
```python
def __init__(self, secret_store: "SecretStore", token_expiry_seconds: int = 300):
    self._secret_store = secret_store
    self._token_expiry_seconds = token_expiry_seconds
    self._pending: Dict[str, Dict[str, Any]] = {}
```

4. Update `is_available()`:
```python
def is_available(self) -> bool:
    return self._secret_store.has_secret()
```

5. Update `validate_confirmation()` — replace:
```python
if not hmac.compare_digest(secret, self._secret):
```
with:
```python
if not self._secret_store.verify_secret(secret):
```

6. Update instructions string in `request_confirmation()` — replace:
```python
"  - confirmation_secret: your OCI_LA_CONFIRMATION_SECRET value"
```
with:
```python
"  - confirmation_secret: your confirmation secret"
```

7. Add import at top (regular import, no circular dependency):
```python
from .secret_store import SecretStore
```

8. Remove the unused `import hmac` (no longer needed after removing `hmac.compare_digest` from this file — it's now in `SecretStore`).

- [ ] **Step 4: Run tests — expect PASS**

Run: `/usr/local/bin/python3.14 -m pytest tests/test_confirmation.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/confirmation.py tests/test_confirmation.py
git commit -m "refactor: ConfirmationManager delegates secret verification to SecretStore"
```

---

### Task 4: Wire SecretStore + AuditLogger into handlers

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py:30-169`
- Modify: `tests/test_handlers.py`

- [ ] **Step 1: Update handler test fixtures**

In `tests/test_handlers.py`:

1. Add imports:
```python
from oci_logan_mcp.secret_store import SecretStore
from oci_logan_mcp.audit import AuditLogger
```

2. Add fixtures:
```python
@pytest.fixture
def mock_secret_store(tmp_path):
    store = SecretStore(tmp_path / "test_user" / "confirmation_secret.hash")
    return store

@pytest.fixture
def mock_audit_logger(tmp_path):
    return AuditLogger(tmp_path / "logs")
```

3. Update the `handlers` fixture to pass `secret_store` and `audit_logger`:
```python
@pytest.fixture
def handlers(settings, mock_oci_client, mock_cache, mock_query_logger,
             mock_context_manager, mock_user_store, mock_preference_store,
             mock_secret_store, mock_audit_logger):
    return MCPHandlers(
        settings=settings,
        oci_client=mock_oci_client,
        cache=mock_cache,
        query_logger=mock_query_logger,
        context_manager=mock_context_manager,
        user_store=mock_user_store,
        preference_store=mock_preference_store,
        secret_store=mock_secret_store,
        audit_logger=mock_audit_logger,
    )
```

4. Update `TestConfirmationFlow.handlers_with_secret` fixture: set a secret on `mock_secret_store` and pass it.

5. Update `TestConfirmationIntegration.handlers_confirmed` fixture similarly.

6. Add audit log verification tests:
```python
@pytest.mark.asyncio
async def test_guarded_tool_audit_logged(self, handlers_with_secret, tmp_path):
    """Guarded tool interactions are audit-logged."""
    args = {"alert_id": "ocid1.alarm.oc1..abc"}
    await handlers_with_secret.handle_tool_call("delete_alert", args)
    # Check audit log exists and has confirmation_requested entry
    import json
    audit_file = tmp_path / "logs" / "audit.log"
    # (verify entry exists with outcome=confirmation_requested)
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `/usr/local/bin/python3.14 -m pytest tests/test_handlers.py -v`
Expected: FAIL — `MCPHandlers.__init__` doesn't accept `secret_store`/`audit_logger` yet

- [ ] **Step 3: Update MCPHandlers**

In `src/oci_logan_mcp/handlers.py`:

1. Add imports:
```python
from .secret_store import SecretStore
from .audit import AuditLogger
```

2. Update `__init__` signature (add after `preference_store`):
```python
secret_store: Optional[SecretStore] = None,
audit_logger: Optional[AuditLogger] = None,
```

3. Store them and update ConfirmationManager creation:
```python
self.audit_logger = audit_logger

# Always create ConfirmationManager — fail-closed if no secret is set.
# If no SecretStore provided (e.g., tests), create one with a dummy path.
if secret_store is None:
    from .secret_store import SecretStore
    secret_store = SecretStore(Path("/dev/null/no_secret"))
self.secret_store = secret_store
self.confirmation_manager = ConfirmationManager(
    secret_store=secret_store,
    token_expiry_seconds=settings.guardrails.token_expiry_seconds,
)
```

4. Update the confirmation gate in `handle_tool_call()` to audit-log every outcome. Replace the existing gate (lines 131-162) with:

```python
user_id = self.user_store.user_id if self.user_store else "unknown"

if self.confirmation_manager.is_guarded(name):
    clean_args = {k: v for k, v in arguments.items()
                  if k not in ("confirmation_token", "confirmation_secret")}

    if not self.confirmation_manager.is_available():
        if self.audit_logger:
            self.audit_logger.log(user=user_id, tool=name,
                                  args=clean_args, outcome="confirmation_unavailable")
        return [{"type": "text", "text": json.dumps({
            "status": "confirmation_unavailable",
            "error": "No confirmation secret is set. "
                     "Run the server interactively to set one.",
        }, indent=2)}]

    token = arguments.get("confirmation_token")
    secret = arguments.get("confirmation_secret", "")

    if not token:
        confirmation = self.confirmation_manager.request_confirmation(name, arguments)
        if self.audit_logger:
            self.audit_logger.log(user=user_id, tool=name,
                                  args=clean_args, outcome="confirmation_requested")
        return [{"type": "text", "text": json.dumps(confirmation, indent=2)}]

    if not self.confirmation_manager.validate_confirmation(
        token, secret, name, arguments
    ):
        if self.audit_logger:
            self.audit_logger.log(user=user_id, tool=name,
                                  args=clean_args, outcome="confirmation_failed")
        return [{"type": "text", "text": json.dumps({
            "status": "confirmation_failed",
            "error": "Invalid/expired token, wrong secret, or arguments changed. "
                     "Request a new confirmation token.",
        }, indent=2)}]

    if self.audit_logger:
        self.audit_logger.log(user=user_id, tool=name,
                              args=clean_args, outcome="confirmed")

    arguments = {k: v for k, v in arguments.items()
                 if k not in ("confirmation_token", "confirmation_secret")}
```

5. After the `try/except` block, audit the execution outcome:

```python
try:
    result = await handler(arguments)
    if self.confirmation_manager.is_guarded(name):
        if self.audit_logger:
            summary = result[0]["text"][:200] if result else ""
            self.audit_logger.log(user=user_id, tool=name,
                                  args=clean_args, outcome="executed",
                                  result_summary=summary)
    return result
except Exception as e:
    if self.confirmation_manager.is_guarded(name):
        if self.audit_logger:
            self.audit_logger.log(user=user_id, tool=name,
                                  args=clean_args, outcome="execution_failed",
                                  error=str(e)[:200])
    logger.exception(f"Error in tool {name}")
    return [{"type": "text", "text": f"Error executing {name}: {str(e)}"}]
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `/usr/local/bin/python3.14 -m pytest tests/test_handlers.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/handlers.py tests/test_handlers.py
git commit -m "feat: wire SecretStore and AuditLogger into handler dispatch"
```

---

### Task 5: Update server startup and CLI

**Files:**
- Modify: `src/oci_logan_mcp/server.py:184-202`
- Modify: `src/oci_logan_mcp/__main__.py:15-61`
- Modify: `src/oci_logan_mcp/config.py:286-289`

- [ ] **Step 1: Update server.py to init SecretStore + AuditLogger**

In `src/oci_logan_mcp/server.py`:

1. Add imports:
```python
from .secret_store import SecretStore
from .audit import AuditLogger
```

2. After UserStore/PreferenceStore init (line 190), add:
```python
# Initialize per-user secret store
secret_path = base_dir / "users" / self.user_store.user_id / "confirmation_secret.hash"
self.secret_store = SecretStore(secret_path)

# Initialize shared audit logger
self.audit_logger = AuditLogger(log_dir=base_dir / "logs")

# Deprecation warning for old env var
if os.environ.get("OCI_LA_CONFIRMATION_SECRET"):
    logger.warning(
        "OCI_LA_CONFIRMATION_SECRET is no longer used. "
        "Per-user secrets are now stored in the user directory."
    )
```

3. Update MCPHandlers construction to pass `secret_store` and `audit_logger`:
```python
self.handlers = MCPHandlers(
    settings=self.settings,
    oci_client=self.oci_client,
    cache=self.cache,
    query_logger=self.query_logger,
    context_manager=self.context_manager,
    user_store=self.user_store,
    preference_store=self.preference_store,
    secret_store=self.secret_store,
    audit_logger=self.audit_logger,
)
```

4. Before initializing handlers, add first-run secret prompt:
```python
if self.secret_store.has_secret() and not self.secret_store.is_valid():
    import sys
    logger.error(
        "Confirmation secret file for user '%s' is corrupted. "
        "Run with --reset-secret to set a new one.",
        self.user_store.user_id,
    )
    sys.exit(1)

if not self.secret_store.has_secret():
    import sys
    if sys.stdin.isatty():
        import getpass
        print(f"\nNo confirmation secret found for user '{self.user_store.user_id}'.")
        print("Destructive operations (delete/update) require a secret for safety.")
        while True:
            secret = getpass.getpass("Enter your confirmation secret: ")
            confirm = getpass.getpass("Confirm: ")
            if secret != confirm:
                print("Secrets do not match. Try again.")
                continue
            try:
                self.secret_store.set_secret(secret)
                print("Secret saved. You'll need this to confirm destructive operations.\n")
                self.audit_logger.log(
                    user=self.user_store.user_id,
                    tool="__secret_management",
                    args={}, outcome="secret_set",
                )
                break
            except ValueError as e:
                print(f"Error: {e}. Try again.")
    else:
        logger.error(
            "No confirmation secret set for user '%s'. "
            "Run interactively or use --reset-secret to set one.",
            self.user_store.user_id,
        )
        sys.exit(1)
```

- [ ] **Step 2: Update __main__.py to add --reset-secret**

In `src/oci_logan_mcp/__main__.py`:

1. Add `--reset-secret` argument after `--user`:
```python
parser.add_argument(
    "--reset-secret",
    action="store_true",
    help="Reset your confirmation secret for destructive operations",
)
```

2. Add validation:
```python
if args.reset_secret and not args.user:
    parser.error("--reset-secret requires --user")
if args.reset_secret and (args.setup or args.promote_and_exit):
    parser.error("--reset-secret cannot be combined with --setup or --promote-and-exit")
```

3. Add handler before `server_main()`:
```python
if args.reset_secret:
    if args.user:
        os.environ["LOGAN_USER"] = args.user
    _reset_secret(args.user)
    sys.exit(0)
```

4. Add `_reset_secret` function:
```python
def _reset_secret(user_id: str) -> None:
    """Reset confirmation secret for a user with identity verification."""
    import getpass
    from .config import CONFIG_PATH
    from .secret_store import SecretStore
    from .audit import AuditLogger

    if not sys.stdin.isatty():
        print("Error: --reset-secret requires an interactive terminal.",
              file=sys.stderr)
        sys.exit(1)

    base_dir = CONFIG_PATH.parent
    user_dir = base_dir / "users" / user_id

    # Identity check: OS user must own the user directory.
    # If the directory doesn't exist yet, the calling OS user will become
    # the owner when it's created — this is the expected first-run flow
    # where the legitimate user sets up their own identity.
    if user_dir.exists():
        dir_owner = os.stat(user_dir).st_uid
        if dir_owner != os.getuid():
            print(f"Error: You do not own the directory for user '{user_id}'.",
                  file=sys.stderr)
            sys.exit(1)

    secret_path = user_dir / "confirmation_secret.hash"
    store = SecretStore(secret_path)

    while True:
        secret = getpass.getpass("Enter new confirmation secret: ")
        confirm = getpass.getpass("Confirm: ")
        if secret != confirm:
            print("Secrets do not match. Try again.")
            continue
        try:
            store.set_secret(secret)
            print("Secret reset successfully.")
            audit = AuditLogger(base_dir / "logs")
            audit.log(user=user_id, tool="__secret_management",
                      args={}, outcome="secret_reset")
            return
        except ValueError as e:
            print(f"Error: {e}. Try again.")
```

- [ ] **Step 3: Update config.py — remove env var handling**

In `src/oci_logan_mcp/config.py` (deprecation warning is already in server.py from Step 1):

1. Remove from `_apply_env_overrides()`:
```python
if v := os.environ.get("OCI_LA_CONFIRMATION_SECRET"):
    settings.confirmation_secret = v
```

2. Remove from `Settings` dataclass:
```python
confirmation_secret: str = ""
```

- [ ] **Step 4: Run full test suite**

Run: `/usr/local/bin/python3.14 -m pytest tests/ -v --tb=short`
Expected: ALL PASS (some tests may need fixture updates due to removed `confirmation_secret`)

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/server.py src/oci_logan_mcp/__main__.py src/oci_logan_mcp/config.py
git commit -m "feat: add first-run secret prompt, --reset-secret CLI, remove env var"
```

---

### Task 6: Update tool descriptions and config tests

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Update tool descriptions**

In `src/oci_logan_mcp/tools.py`, for all 6 guarded tools:

Replace in `confirmation_secret` field description:
```
"Your OCI_LA_CONFIRMATION_SECRET value. Required with token to execute."
```
with:
```
"Your confirmation secret. Required with token to execute."
```

- [ ] **Step 2: Update config tests**

In `tests/test_config.py`:

1. Remove `test_confirmation_secret_from_env` (env var no longer exists)
2. Remove `test_confirmation_secret_not_in_to_dict` (field removed)
3. Remove `test_confirmation_secret_default_empty` (field removed)
4. Keep `test_guardrails_token_expiry_default` and `test_guardrails_token_expiry_from_yaml`

- [ ] **Step 3: Run full test suite**

Run: `/usr/local/bin/python3.14 -m pytest tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add src/oci_logan_mcp/tools.py tests/test_config.py
git commit -m "chore: update tool descriptions and remove env var tests"
```

---

### Task 7: Update README documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the "Destructive Operation Safety" section**

Replace the current section with updated content covering:
- Per-user secrets (set on first run, hashed with scrypt)
- The two-step confirmation flow (unchanged)
- `--reset-secret` for forgotten secrets
- Admin recovery (delete hash file)
- Shared audit log at `~/.oci-logan-mcp/logs/audit.log`
- No more `OCI_LA_CONFIRMATION_SECRET` env var
- Audit log format and what it records

- [ ] **Step 2: Run full test suite one final time**

Run: `/usr/local/bin/python3.14 -m pytest tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: update safety docs for per-user secrets and audit log"
```

---

## Verification

1. **Unit tests:** `/usr/local/bin/python3.14 -m pytest tests/test_secret_store.py tests/test_audit.py -v`
2. **Confirmation tests:** `/usr/local/bin/python3.14 -m pytest tests/test_confirmation.py -v`
3. **Handler tests:** `/usr/local/bin/python3.14 -m pytest tests/test_handlers.py -v`
4. **Config tests:** `/usr/local/bin/python3.14 -m pytest tests/test_config.py -v`
5. **Full suite:** `/usr/local/bin/python3.14 -m pytest tests/ -v --tb=short`
6. **Manual test:** Start server with `--user testuser`, set secret interactively, call `delete_alert` → get confirmation_required, confirm with token+secret → executes, check `~/.oci-logan-mcp/logs/audit.log` for entries.
7. **Reset test:** Run `--user testuser --reset-secret`, set new secret, verify old secret no longer works.
8. **Fail-closed test:** Delete hash file, restart server → prompted to set new secret.
