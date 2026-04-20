# src/oci_logan_mcp/sanitize.py
"""Detect and redact sensitive data before promoting queries to shared storage."""
from __future__ import annotations

import re
from typing import Optional

OCID_RE = re.compile(r"ocid1\.[A-Za-z0-9_.-]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SECRETISH_RE = re.compile(
    r"(?:api[_-]?key|secret|password|token|bearer|authorization)", re.IGNORECASE,
)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE,
)
LONG_HEX_RE = re.compile(r"\b[a-f0-9]{24,}\b", re.IGNORECASE)

_SENSITIVE_PATTERNS = [OCID_RE, IPV4_RE, EMAIL_RE, SECRETISH_RE, UUID_RE, LONG_HEX_RE]


def looks_sensitive(text: str) -> bool:
    """Return True if text contains sensitive-looking data."""
    return any(p.search(text) for p in _SENSITIVE_PATTERNS)


def sanitize_query_text(query_text: str) -> Optional[str]:
    """Redact sensitive values in query text. Returns None if unredeemable."""
    if not query_text:
        return None
    cleaned = OCID_RE.sub("<resource_ocid>", query_text)
    cleaned = IPV4_RE.sub("<ip_address>", cleaned)
    cleaned = EMAIL_RE.sub("<email>", cleaned)
    cleaned = UUID_RE.sub("<uuid>", cleaned)
    cleaned = LONG_HEX_RE.sub("<id>", cleaned)
    if SECRETISH_RE.search(cleaned):
        return None
    return cleaned


def normalize_query_text(query: str) -> str:
    """Normalize query text for cross-user aggregation:
    - Strip leading/trailing whitespace
    - Collapse internal whitespace runs to single spaces
    - Preserve case (query syntax can be case-significant for strings)
    """
    return re.sub(r"\s+", " ", query.strip())


def sanitize_pattern(text: str) -> Optional[str]:
    """Sanitize a natural language pattern. Returns None if sensitive."""
    candidate = text.strip()
    if not candidate:
        return None
    if looks_sensitive(candidate):
        return None
    return candidate


_SECRETISH_KEYS = frozenset({
    "password", "secret", "token", "bearer", "authorization",
    "api_key", "apikey", "auth",
    "confirmation_secret", "confirmation_secret_confirm", "confirmation_token",
})


def redact_dict(obj):
    """Recursively redact PII and secrets from a dict/list structure.

    - Keys matching _SECRETISH_KEYS have their values replaced with '<redacted>'.
    - String values are passed through sanitize_query_text to mask OCIDs, IPs,
      emails, UUIDs, and long hex ids.
    - Non-string scalars are returned unchanged.
    - Tuples are converted to lists.
    """
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRETISH_KEYS:
                result[k] = "<redacted>"
            else:
                result[k] = redact_dict(v)
        return result
    elif isinstance(obj, (list, tuple)):
        return [redact_dict(item) for item in obj]
    elif isinstance(obj, str):
        if not obj:
            return obj
        sanitized = sanitize_query_text(obj)
        # sanitize_query_text returns None when the text contains a secret keyword
        # (e.g. "password=x"). In that case fall back to full redaction.
        return sanitized if sanitized is not None else "<redacted>"
    else:
        return obj
