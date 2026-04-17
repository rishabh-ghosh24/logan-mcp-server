"""Salted scrypt secret storage for per-user confirmation secrets."""

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
    """Store and verify a per-user secret as a salted scrypt hash in a YAML file."""

    def __init__(self, secret_path: Path) -> None:
        self._path = secret_path

    # ── queries ───────────────────────────────────────────────────────

    def has_secret(self) -> bool:
        """Return *True* if the hash file exists on disk."""
        return self._path.is_file()

    def is_valid(self) -> bool:
        """Return *True* if the hash file exists **and** contains all required keys."""
        if not self._path.is_file():
            return False
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return bool(
                data and all(k in data for k in ("salt", "hash", "n", "r", "p"))
            )
        except Exception:
            return False

    # ── mutations ─────────────────────────────────────────────────────

    def set_secret(self, plaintext: str) -> None:
        """Hash *plaintext* with a fresh random salt and persist to YAML."""
        if len(plaintext) < _MIN_SECRET_LENGTH:
            raise ValueError(
                f"Secret must be at least {_MIN_SECRET_LENGTH} characters"
            )

        salt = secrets.token_bytes(_SALT_BYTES)
        hash_bytes = hashlib.scrypt(
            plaintext.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
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
        with open(self._path, "w", encoding="utf-8") as fh:
            yaml.dump(data, fh, default_flow_style=False)

        os.chmod(self._path, 0o600)

    # ── verification ──────────────────────────────────────────────────

    def verify_secret(self, plaintext: str) -> bool:
        """Verify *plaintext* against the stored hash.

        Parameters are read from the file (not hard-coded) so that
        previously-written hashes remain verifiable even if defaults change.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)

            if not data or not all(k in data for k in ("salt", "hash", "n", "r", "p")):
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
            return False
