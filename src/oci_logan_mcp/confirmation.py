# src/oci_logan_mcp/confirmation.py
"""Two-factor confirmation for destructive MCP tool operations.

Prevents accidental or automated delete/update of OCI resources by requiring
a server-generated request-bound token AND a per-user hashed secret.

Fail-closed: if no secret is configured, guarded tools refuse to execute.
"""

import hashlib
import json
import logging
import secrets
import time
from typing import Any, Dict

from .secret_store import SecretStore

logger = logging.getLogger(__name__)

GUARDED_TOOLS = frozenset({
    "delete_alert",
    "delete_saved_search",
    "delete_dashboard",
    "update_alert",
    "update_saved_search",
    "add_dashboard_tile",
})

_SUMMARY_KEYS: Dict[str, list] = {
    "delete_alert": ["alert_id"],
    "delete_saved_search": ["saved_search_id"],
    "delete_dashboard": ["dashboard_id"],
    "update_alert": ["alert_id", "display_name", "severity", "query"],
    "update_saved_search": ["saved_search_id", "display_name", "query"],
    "add_dashboard_tile": ["dashboard_id", "title", "query", "visualization_type"],
}


class ConfirmationManager:
    """Manages two-factor confirmation tokens for destructive operations.

    Factor 1: Server-generated single-use token bound to exact tool+args.
    Factor 2: Per-user secret verified via SecretStore.

    Tokens are consumed on any validation attempt (success or failure).
    """

    def __init__(self, secret_store: SecretStore, token_expiry_seconds: int = 300):
        self._secret_store = secret_store
        self._token_expiry_seconds = token_expiry_seconds
        self._pending: Dict[str, Dict[str, Any]] = {}

    def is_guarded(self, tool_name: str) -> bool:
        """Return True if the tool requires confirmation."""
        return tool_name in GUARDED_TOOLS

    def is_available(self) -> bool:
        """Return True if the confirmation secret is configured."""
        return self._secret_store.has_secret()

    def request_confirmation(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate a confirmation token bound to this exact request.

        Returns a human-readable summary, the token, and instructions.
        """
        self._cleanup_expired()

        token = secrets.token_hex(24)
        self._pending[token] = {
            "fingerprint": self._fingerprint(tool_name, arguments),
            "created_at": time.time(),
        }

        summary = self._build_summary(tool_name, arguments)

        logger.info(
            "Confirmation requested for %s (token=%s...)", tool_name, token[:8]
        )

        return {
            "status": "confirmation_required",
            "confirmation_token": token,
            "summary": summary,
            "expires_in_seconds": self._token_expiry_seconds,
            "instructions": (
                "IMPORTANT: You MUST show this confirmation summary to the user "
                "and ASK them to provide their confirmation secret. "
                "NEVER reuse a secret from a previous operation. "
                "Wait for the user's explicit response before proceeding.\n\n"
                "To proceed, re-invoke this tool with the same arguments plus:\n"
                "  - confirmation_token: the token above\n"
                "  - confirmation_secret: the secret the user provides NOW"
            ),
        }

    def validate_confirmation(
        self,
        token: str,
        secret: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> bool:
        """Validate a confirmation token+secret+tool+args. Consumes the token."""
        self._cleanup_expired()

        # Always consume — even failed attempts burn the token
        pending = self._pending.pop(token, None)
        if pending is None:
            logger.warning("Confirmation rejected: invalid or expired token")
            return False

        if time.time() - pending["created_at"] > self._token_expiry_seconds:
            logger.warning("Confirmation rejected: token expired")
            return False

        if not self._secret_store.verify_secret(secret):
            logger.warning("Confirmation rejected: wrong secret")
            return False

        expected_fp = pending["fingerprint"]
        actual_fp = self._fingerprint(tool_name, arguments)
        if actual_fp != expected_fp:
            logger.warning(
                "Confirmation rejected: tool/args mismatch (expected=%s, got=%s)",
                expected_fp[:12],
                actual_fp[:12],
            )
            return False

        logger.info(
            "Confirmation approved for %s (token=%s...)", tool_name, token[:8]
        )
        return True

    def _fingerprint(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """SHA-256 fingerprint of canonicalized (tool_name, arguments)."""
        clean = {
            k: v
            for k, v in sorted(arguments.items())
            if k not in ("confirmation_token", "confirmation_secret")
        }
        canonical = json.dumps({"tool": tool_name, "args": clean}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _build_summary(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Build a human-readable summary of the pending operation."""
        action = tool_name.replace("_", " ").upper()
        parts = [f"Action: {action}"]
        for key in _SUMMARY_KEYS.get(tool_name, []):
            if key in arguments:
                val = str(arguments[key])
                if len(val) > 120:
                    val = val[:120] + "..."
                parts.append(f"  {key}: {val}")
        return "\n".join(parts)

    def _cleanup_expired(self) -> None:
        """Remove expired tokens to prevent memory leaks."""
        now = time.time()
        expired = [
            t
            for t, p in self._pending.items()
            if now - p["created_at"] > self._token_expiry_seconds
        ]
        for t in expired:
            del self._pending[t]
