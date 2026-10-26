"""No-text audit mode must not retain short or nested free-text arguments."""
import hashlib
import json

import pytest

from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.sanitize import AuditFingerprint


@pytest.mark.parametrize("field,value", [
    ("resolved_value", "private-production-host"),
    ("title", "Private customer outage"),
    ("name", "private-query-name"),
    ("label", "small-sensitive-label"),
    ("format", "private-view"),
    ("unknown_argument", "v" * 64),
])
def test_no_text_mode_hashes_short_nested_values_and_preserves_structural_fields(tmp_path, field, value):
    # A length-only rule or a free-text-key denylist would retain these values.
    logger = AuditLogger(tmp_path / "audit", store_arguments=False)
    fingerprint = AuditFingerprint("synthetic MQL")
    logger.log(user="review", tool="example", outcome="executed", args={
        "payload": [{field: value}], "format": "json", "time_range": "last_1_hour",
        "max_results": 5, "read_only": True, "query_fingerprint": fingerprint,
    })
    rows = list(logger.iter_entries())
    assert len(rows) == 1, "The operation must produce a durable audit record"
    args = rows[0]["args_redacted"]
    assert args["payload"][0][field] == {"sha256": hashlib.sha256(value.encode()).hexdigest(), "length": len(value)}
    assert value not in json.dumps(rows[0])
    assert args["format"] == "json" and args["time_range"] == "last_1_hour"
    assert args["max_results"] == 5 and args["read_only"] is True
    assert args["query_fingerprint"] == fingerprint
    assert rows[0]["text_retained"] is False
