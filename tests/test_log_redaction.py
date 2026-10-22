"""Exercise real child loggers, interpolation and SDK message representations."""

import io
import json
import logging

import pytest
from mcp.types import CallToolRequestParams


@pytest.mark.parametrize("secret", ["MARKER_HEAD_hunter2_MARKER_TAIL", 'MARKER_HEAD" mid\' MARKER_TAIL\\z'])
def test_child_logger_records_and_format_args_are_redacted(secret):
    from oci_logan_mcp.server import install_secret_redaction

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    install_secret_redaction(handler)
    root = logging.getLogger()
    old_level = root.level
    old_handlers = root.handlers[:]
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        sdk = logging.getLogger("mcp.server.lowlevel.server")
        sdk.debug("Received message: %s", json.dumps({"confirmation_secret": secret, "id": 1}))
        sdk.debug("Received message: %s", repr({"confirmation_secret": secret, "confirmation_secret_confirm": secret}))
        sdk.debug("Received message: %s", CallToolRequestParams(name="delete_alert", arguments={"confirmation_secret": secret}))
        sdk.debug("model %s", "confirmation_secret=" + repr(secret))
        logging.getLogger("oci_logan_mcp.handlers").info("args=%s", "confirmation_secret=BARE_MARKER, alert_id=x")
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)
        handler.close()
    output = buffer.getvalue()
    assert "MARKER_HEAD" not in output and "MARKER_TAIL" not in output and "BARE_MARKER" not in output
    assert output.count("<REDACTED>") == 6 and "alert_id=x" in output and '"id": 1' in output
