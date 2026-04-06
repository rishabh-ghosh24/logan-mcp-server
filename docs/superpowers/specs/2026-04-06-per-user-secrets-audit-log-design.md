# Per-User Confirmation Secrets & Shared Audit Log

## Problem

The MCP server runs on a shared remote VM with multiple users. The current two-factor confirmation system uses a single server-wide env var (`OCI_LA_CONFIRMATION_SECRET`) as the second factor. This is inadequate because:

- All users must share the same secret (defeats per-user accountability)
- Env vars can leak via process listings on shared VMs
- No audit trail of who performed destructive operations

## Solution

1. **Per-user secrets** stored as salted hashes in each user's directory, set interactively on first run
2. **Shared audit log** recording every guarded tool interaction with user attribution
3. **Remove** the `OCI_LA_CONFIRMATION_SECRET` env var entirely

## Guarded Tools (unchanged)

| Tool | Reason |
|------|--------|
| `delete_alert` | Destroys alarm + backing OCI resources |
| `delete_saved_search` | Destroys saved search |
| `delete_dashboard` | Destroys dashboard + tile data sources |
| `update_alert` | Modifies existing alert (may belong to another user) |
| `update_saved_search` | Modifies existing saved search (may belong to another user) |
| `add_dashboard_tile` | Modifies existing dashboard (may belong to another user) |

---

## Per-User Secret Lifecycle

### First Run

User starts `oci-logan-mcp --user alice.smith`. No secret hash file exists. Server prompts interactively:

```
No confirmation secret found for user 'alice.smith'.
Destructive operations (delete/update) require a secret for safety.
Enter your confirmation secret: ********
Confirm: ********
Secret saved. You'll need this to confirm destructive operations.
```

The plaintext is hashed using `hashlib.scrypt` (stdlib, no new dependency) — a deliberately slow key derivation function resistant to brute-force attacks. The salt, hash, and algorithm parameters are stored in `~/.oci-logan-mcp/users/alice.smith/confirmation_secret.hash` as YAML:

```yaml
algorithm: "scrypt"
salt: "<hex-encoded-random-32-byte-salt>"
hash: "<hex-encoded-scrypt-hash>"
n: 16384
r: 8
p: 1
```

The file is created with `0600` permissions (owner read/write only). The user directory is `0700`.

A minimum secret length of 8 characters is enforced at input time.

The plaintext is never stored.

### Normal Use

Server starts, reads hash file, initializes `ConfirmationManager` with the stored salt+hash. Guarded tools work via the existing two-step token flow — the user provides their plaintext secret in the second call, which is hashed and compared to the stored hash.

### Forgot Secret

```bash
oci-logan-mcp --user alice.smith --reset-secret
```

Prompts for a new secret, overwrites the hash file. This is an audit-logged event (`secret_reset`).

**Identity check:** `--reset-secret` verifies that the current OS user (`os.getuid()`) owns the user directory. This prevents Bob from resetting Alice's secret on a shared VM.

### Admin Recovery

Delete the hash file:
```bash
rm ~/.oci-logan-mcp/users/alice.smith/confirmation_secret.hash
```

Next server start treats the user as first-time and prompts for a new secret.

### Fail-Closed

- No hash file for user → interactive prompt to set one before server starts
- If running non-interactively (no TTY) and no hash file exists → server refuses to start with a clear error message

---

## Shared Audit Log

A single append-only JSON-lines log at `~/.oci-logan-mcp/logs/audit.log`, shared across all users and server instances.

### Events Recorded

| Outcome | When |
|---------|------|
| `secret_set` | User set their confirmation secret for the first time |
| `secret_reset` | User reset their confirmation secret via `--reset-secret` |
| `confirmation_requested` | First call to a guarded tool — token issued |
| `confirmation_failed` | Wrong secret, expired token, or changed args |
| `confirmed` | Valid token + secret |
| `executed` | Handler completed successfully |
| `execution_failed` | Handler threw an error |

### Format

One JSON object per line. Fields:

- `timestamp` — ISO 8601 with timezone
- `user` — user_id from `--user` flag
- `pid` — server process ID (correlates multi-step flows from the same session)
- `tool` — tool name (e.g., `delete_alert`); omitted for `secret_set`/`secret_reset` events
- `args` — full tool arguments (excluding `confirmation_token` and `confirmation_secret`)
- `outcome` — one of the events above
- `result_summary` — human-readable summary of what happened (on `executed` and `execution_failed` events)
- `error` — error message (on `execution_failed` and `confirmation_failed` events)

Example lines:

```json
{"timestamp": "2026-04-06T14:23:01Z", "user": "alice.smith", "tool": "delete_alert", "args": {"alert_id": "ocid1.alarm.oc1..abc"}, "outcome": "confirmation_requested"}
{"timestamp": "2026-04-06T14:23:15Z", "user": "alice.smith", "tool": "delete_alert", "args": {"alert_id": "ocid1.alarm.oc1..abc"}, "outcome": "confirmed"}
{"timestamp": "2026-04-06T14:23:16Z", "user": "alice.smith", "tool": "delete_alert", "args": {"alert_id": "ocid1.alarm.oc1..abc"}, "outcome": "executed", "result_summary": "deleted alarm 'OOMKill Monitor' and 2 backing resources"}
{"timestamp": "2026-04-06T15:10:02Z", "user": "bob.jones", "tool": "update_alert", "args": {"alert_id": "ocid1.alarm.oc1..xyz", "severity": "WARNING", "query": "'Log Source' = 'OKE Control Plane Logs' | where Severity = 'ERROR' | stats count"}, "outcome": "executed", "result_summary": "updated severity, query"}
```

### Security

- `confirmation_token` and `confirmation_secret` are **never** logged
- File-level locking via the existing `locked_file()` pattern from `file_lock.py` for concurrent writes
- Manual log rotation (consistent with codebase patterns): 10MB max, 5 backups (50MB total audit history)
- Audit log directory (`~/.oci-logan-mcp/logs/`) created with `0750` permissions
- Audit log file created with `0640` permissions (owner read/write, group read for admin review)

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `src/oci_logan_mcp/secret_store.py` | Generate salt, hash secret, verify secret, read/write hash file |
| `src/oci_logan_mcp/audit.py` | `AuditLogger` — append JSON lines with file locking and rotation |
| `tests/test_secret_store.py` | Unit tests for secret hashing, verification, file I/O |
| `tests/test_audit.py` | Unit tests for audit log writing, format, rotation |

### Modified Files

| File | Changes |
|------|---------|
| `src/oci_logan_mcp/confirmation.py` | Replace plaintext secret comparison with hash-based verification via `SecretStore`; update user-facing instructions string to remove `OCI_LA_CONFIRMATION_SECRET` reference |
| `src/oci_logan_mcp/handlers.py` | Accept `SecretStore` and `AuditLogger` as constructor args; create `ConfirmationManager` from `SecretStore`; read `user_id` from `self.user_store.user_id`; audit-log all guarded tool outcomes |
| `src/oci_logan_mcp/server.py` | Init `SecretStore` and `AuditLogger`; inject both into `MCPHandlers`; handle first-run secret prompt before starting server |
| `src/oci_logan_mcp/__main__.py` | Add `--reset-secret` CLI flag |
| `src/oci_logan_mcp/config.py` | Remove `OCI_LA_CONFIRMATION_SECRET` env var handling and `Settings.confirmation_secret` field |
| `src/oci_logan_mcp/tools.py` | Update tool descriptions and `confirmation_secret` field descriptions to remove `OCI_LA_CONFIRMATION_SECRET` references; replace with "your confirmation secret" |
| `tests/test_confirmation.py` | Update tests for hash-based verification |
| `tests/test_handlers.py` | Update fixtures and tests for per-user secret and audit logging |
| `tests/test_config.py` | Remove tests for `OCI_LA_CONFIRMATION_SECRET` env var |
| `README.md` | Update documentation |

### Directory Layout

```
~/.oci-logan-mcp/
├── users/
│   ├── alice.smith/
│   │   ├── confirmation_secret.hash    ← NEW (salted SHA-256, YAML)
│   │   ├── learned_queries.yaml
│   │   └── preferences.yaml
│   └── bob.jones/
│       ├── confirmation_secret.hash    ← NEW
│       └── ...
├── logs/
│   ├── queries.log                     (existing)
│   └── audit.log                       ← NEW (shared JSON lines)
└── config.yaml
```

---

## Component Interfaces

### SecretStore

```python
class SecretStore:
    def __init__(self, user_dir: Path) -> None: ...
    def has_secret(self) -> bool: ...
    def set_secret(self, plaintext: str) -> None: ...
    def verify_secret(self, plaintext: str) -> bool: ...
    def delete_secret(self) -> None: ...
```

- `has_secret()` — checks if hash file exists and is readable
- `set_secret(plaintext)` — validates minimum 8 chars, generates random 32-byte salt, hashes with `hashlib.scrypt(n=16384, r=8, p=1)`, writes YAML with `0600` permissions
- `verify_secret(plaintext)` — reads salt+params from file, hashes provided plaintext, compares with `hmac.compare_digest`

Admin recovery is done by deleting the hash file directly (`rm`), not via a code path.

### AuditLogger

```python
class AuditLogger:
    def __init__(self, log_dir: Path) -> None: ...
    def log(self, user: str, tool: str, args: dict, outcome: str,
            result_summary: str = "", error: str = "") -> None: ...
```

- Creates `log_dir` with `0750` permissions if it doesn't exist
- Appends one JSON line per call with `timestamp`, `user`, `pid`, `tool`, `args`, `outcome`
- File-level locking via `locked_file()` pattern from `file_lock.py`
- Strips `confirmation_token` and `confirmation_secret` from args before writing
- Manual log rotation: 10MB max, 5 backups

### ConfirmationManager (updated)

```python
class ConfirmationManager:
    def __init__(self, secret_store: SecretStore, token_expiry_seconds: int = 300) -> None: ...
    def is_guarded(self, tool_name: str) -> bool: ...
    def is_available(self) -> bool: ...  # delegates to secret_store.has_secret()
    def request_confirmation(self, tool_name: str, arguments: dict) -> dict: ...
    def validate_confirmation(self, token: str, secret: str, tool_name: str, arguments: dict) -> bool: ...
```

- `is_available()` now delegates to `secret_store.has_secret()`
- `validate_confirmation()` calls `secret_store.verify_secret(secret)` instead of `hmac.compare_digest` against a stored plaintext
- All other behavior (token binding, single-use, expiry) unchanged

---

## What Gets Removed

- `OCI_LA_CONFIRMATION_SECRET` env var handling in `config.py` `_apply_env_overrides()`
- `Settings.confirmation_secret` field
- Direct plaintext secret comparison in `ConfirmationManager`
- Tests for env var secret loading

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| No hash file, interactive TTY | Prompt user to set secret |
| No hash file, no TTY | Refuse to start, print error: "Run with --reset-secret to set a confirmation secret" |
| Hash file corrupted/unreadable | Refuse to start, print error suggesting --reset-secret |
| --reset-secret with existing secret | Prompt for new secret, overwrite, audit-log the reset |
| Concurrent writes to audit.log | File-level locking ensures one writer at a time |
