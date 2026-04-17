"""Tests for SecretStore – scrypt-based per-user secret hashing."""

import os
import stat

import pytest
import yaml

from oci_logan_mcp.secret_store import SecretStore


class TestSecretStore:
    """Unit tests for SecretStore."""

    @pytest.fixture()
    def store(self, tmp_path):
        return SecretStore(tmp_path / "user_secret.yaml")

    # ── existence / validity ──────────────────────────────────────────

    def test_has_secret_false_initially(self, store):
        assert store.has_secret() is False

    def test_has_secret_true_after_set(self, store):
        store.set_secret("abcdefgh")
        assert store.has_secret() is True

    def test_is_valid_false_when_no_file(self, store):
        assert store.is_valid() is False

    def test_is_valid_true_after_set(self, store):
        store.set_secret("abcdefgh")
        assert store.is_valid() is True

    def test_is_valid_false_when_corrupted(self, store):
        store.set_secret("abcdefgh")
        store._path.write_text("not: valid: yaml: {{{}}", encoding="utf-8")
        assert store.is_valid() is False

    def test_is_valid_false_when_missing_fields(self, store):
        store.set_secret("abcdefgh")
        store._path.write_text(
            yaml.dump({"algorithm": "scrypt", "salt": "aa"}),
            encoding="utf-8",
        )
        assert store.is_valid() is False

    # ── set / verify ──────────────────────────────────────────────────

    def test_set_and_verify(self, store):
        store.set_secret("my-secret-phrase")
        assert store.verify_secret("my-secret-phrase") is True

    def test_wrong_secret_rejected(self, store):
        store.set_secret("my-secret-phrase")
        assert store.verify_secret("wrong-phrase!!") is False

    def test_minimum_length_enforced(self, store):
        with pytest.raises(ValueError, match="at least 8 characters"):
            store.set_secret("short")

    # ── file properties ───────────────────────────────────────────────

    def test_file_permissions(self, store):
        store.set_secret("abcdefgh")
        mode = stat.S_IMODE(os.stat(store._path).st_mode)
        assert mode == 0o600

    def test_hash_file_is_yaml_with_scrypt_params(self, store):
        store.set_secret("abcdefgh")
        with open(store._path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["algorithm"] == "scrypt"
        assert isinstance(data["salt"], str) and len(data["salt"]) == 64  # 32 bytes hex
        assert isinstance(data["hash"], str) and len(data["hash"]) > 0
        assert data["n"] == 16384
        assert data["r"] == 8
        assert data["p"] == 1

    def test_different_salts_per_set(self, store):
        store.set_secret("abcdefgh")
        with open(store._path, "r", encoding="utf-8") as f:
            salt1 = yaml.safe_load(f)["salt"]

        store.set_secret("abcdefgh")
        with open(store._path, "r", encoding="utf-8") as f:
            salt2 = yaml.safe_load(f)["salt"]

        assert salt1 != salt2

    # ── forward compatibility: params read from file ──────────────────

    def test_verify_reads_params_from_file(self, store):
        store.set_secret("abcdefgh")
        # Tamper with n in the file so the hash won't match if verify
        # actually reads n from the file (which it should).
        with open(store._path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        original_n = data["n"]
        data["n"] = original_n * 2  # double n -> different hash
        with open(store._path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False)

        # Verification must fail because stored n no longer matches
        assert store.verify_secret("abcdefgh") is False

    # ── error resilience ──────────────────────────────────────────────

    def test_corrupted_file_returns_false(self, store):
        store._path.parent.mkdir(parents=True, exist_ok=True)
        store._path.write_text("<<<garbage>>>", encoding="utf-8")
        assert store.verify_secret("anything") is False

    def test_missing_fields_returns_false(self, store):
        store._path.parent.mkdir(parents=True, exist_ok=True)
        store._path.write_text(
            yaml.dump({"algorithm": "scrypt", "salt": "aa"}),
            encoding="utf-8",
        )
        assert store.verify_secret("anything") is False
