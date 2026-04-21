"""Tests for two-factor confirmation manager."""

import time

import pytest

from oci_logan_mcp.confirmation import ConfirmationManager, GUARDED_TOOLS
from oci_logan_mcp.secret_store import SecretStore


class TestConfirmationManager:
    """Unit tests for ConfirmationManager."""

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

    def test_is_guarded(self, manager):
        """All guarded tools are recognized; known non-mutating tools are not."""
        for tool in GUARDED_TOOLS:
            assert manager.is_guarded(tool) is True
        assert manager.is_guarded("run_query") is False
        assert manager.is_guarded("list_alerts") is False

    def test_create_tools_are_guarded(self, manager):
        """Create_* tools must require server-side confirmation.

        Regression guard for a bug where descriptions said 'APPROVAL REQUIRED'
        but GUARDED_TOOLS did not include the creates, so a misaligned client
        could skip the approval step entirely.
        """
        assert manager.is_guarded("create_alert") is True
        assert manager.is_guarded("create_saved_search") is True
        assert manager.is_guarded("create_dashboard") is True

    def test_is_available_with_secret(self, manager):
        assert manager.is_available() is True

    def test_is_available_without_secret(self, manager_no_secret):
        assert manager_no_secret.is_available() is False

    def test_request_returns_token_and_summary(self, manager):
        result = manager.request_confirmation(
            "delete_alert", {"alert_id": "ocid1.alarm.oc1..abc"}
        )
        assert result["status"] == "confirmation_required"
        assert "confirmation_token" in result
        assert len(result["confirmation_token"]) >= 32
        assert "DELETE ALERT" in result["summary"]
        assert "ocid1.alarm.oc1..abc" in result["summary"]

    def test_validate_succeeds_with_matching_args(self, manager):
        args = {"alert_id": "ocid1.alarm.oc1..abc"}
        result = manager.request_confirmation("delete_alert", args)
        token = result["confirmation_token"]
        assert (
            manager.validate_confirmation(token, "test-secret", "delete_alert", args)
            is True
        )

    def test_validate_fails_wrong_secret(self, manager):
        args = {"alert_id": "ocid1.alarm.oc1..abc"}
        token = manager.request_confirmation("delete_alert", args)["confirmation_token"]
        assert (
            manager.validate_confirmation(token, "wrong", "delete_alert", args)
            is False
        )

    def test_validate_fails_different_args(self, manager):
        """Token for alert A cannot authorize delete of alert B."""
        args_a = {"alert_id": "ocid1.alarm.oc1..aaa"}
        args_b = {"alert_id": "ocid1.alarm.oc1..bbb"}
        token = manager.request_confirmation("delete_alert", args_a)[
            "confirmation_token"
        ]
        assert (
            manager.validate_confirmation(token, "test-secret", "delete_alert", args_b)
            is False
        )

    def test_validate_fails_different_tool(self, manager):
        """Token for delete_alert cannot authorize delete_dashboard."""
        args = {"alert_id": "ocid1.alarm.oc1..abc"}
        token = manager.request_confirmation("delete_alert", args)[
            "confirmation_token"
        ]
        assert (
            manager.validate_confirmation(
                token, "test-secret", "delete_dashboard", args
            )
            is False
        )

    def test_token_single_use(self, manager):
        args = {"alert_id": "ocid1.alarm.oc1..abc"}
        token = manager.request_confirmation("delete_alert", args)[
            "confirmation_token"
        ]
        assert (
            manager.validate_confirmation(token, "test-secret", "delete_alert", args)
            is True
        )
        assert (
            manager.validate_confirmation(token, "test-secret", "delete_alert", args)
            is False
        )

    def test_token_consumed_on_failed_attempt(self, manager):
        """Even a failed validation consumes the token — must request fresh."""
        args = {"alert_id": "ocid1.alarm.oc1..abc"}
        token = manager.request_confirmation("delete_alert", args)[
            "confirmation_token"
        ]
        assert (
            manager.validate_confirmation(token, "wrong", "delete_alert", args)
            is False
        )
        # Token is now consumed — correct secret also fails
        assert (
            manager.validate_confirmation(token, "test-secret", "delete_alert", args)
            is False
        )

    def test_token_expires(self, tmp_path):
        store = SecretStore(tmp_path / "expire_test.hash")
        store.set_secret("test-secret")
        manager = ConfirmationManager(secret_store=store, token_expiry_seconds=1)
        args = {"alert_id": "a"}
        token = manager.request_confirmation("delete_alert", args)[
            "confirmation_token"
        ]
        time.sleep(1.1)
        assert (
            manager.validate_confirmation(token, "test-secret", "delete_alert", args) is False
        )

    def test_bogus_token_rejected(self, manager):
        assert (
            manager.validate_confirmation("bogus", "test-secret", "delete_alert", {})
            is False
        )

    def test_cleanup_expired(self, tmp_path):
        store = SecretStore(tmp_path / "cleanup_test.hash")
        store.set_secret("test-secret")
        manager = ConfirmationManager(secret_store=store, token_expiry_seconds=1)
        for _ in range(5):
            manager.request_confirmation("delete_alert", {"alert_id": "a"})
        time.sleep(1.1)
        manager.request_confirmation("delete_alert", {"alert_id": "b"})
        assert len(manager._pending) == 1

    def test_summary_for_update(self, manager):
        """Update tools mention what is being updated."""
        result = manager.request_confirmation(
            "update_alert",
            {"alert_id": "ocid1.alarm.oc1..abc", "severity": "WARNING"},
        )
        assert "UPDATE ALERT" in result["summary"]
        assert "ocid1.alarm.oc1..abc" in result["summary"]

    def test_summary_for_add_tile(self, manager):
        """add_dashboard_tile summary includes visualization type."""
        result = manager.request_confirmation(
            "add_dashboard_tile",
            {
                "dashboard_id": "ocid1.db.test",
                "title": "Errors",
                "query": "* | stats count",
                "visualization_type": "bar",
            },
        )
        assert "ADD DASHBOARD TILE" in result["summary"]
        assert "bar" in result["summary"]
