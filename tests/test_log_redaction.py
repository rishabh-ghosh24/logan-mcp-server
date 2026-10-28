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


def test_main_redacts_root_handlers_added_after_import(monkeypatch):
    import oci_logan_mcp.server as server_module

    buffer = io.StringIO()
    late_handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level

    class LoggingServer:
        async def run(self):
            logging.getLogger("mcp.server.lowlevel.server").warning(
                "late-handler args=%s safe_id=release_check",
                {"confirmation_secret": "LATE_HANDLER_SECRET_MARKER"},
            )

    monkeypatch.setattr(server_module, "OCILogAnalyticsMCPServer", LoggingServer)
    root.handlers = [late_handler]
    root.setLevel(logging.DEBUG)
    try:
        server_module.main()
    finally:
        root.handlers, root.level = old_handlers, old_level
        late_handler.close()
    output = buffer.getvalue()
    assert "safe_id=release_check" in output
    assert "LATE_HANDLER_SECRET_MARKER" not in output
    assert output.count("<REDACTED>") == 1


def test_repeated_install_filters_each_handler_once_and_keeps_other_filters(monkeypatch):
    import oci_logan_mcp.server as server_module

    calls = []
    real_filter = server_module._SecretRedactFilter.filter

    def observed_filter(self, record):
        calls.append(record.name)
        return real_filter(self, record)

    monkeypatch.setattr(server_module._SecretRedactFilter, "filter", observed_filter)
    buffer = io.StringIO()
    handlers = [logging.StreamHandler(buffer), logging.StreamHandler(buffer)]
    other_filters = [logging.Filter(), logging.Filter()]
    try:
        for index, handler in enumerate(handlers):
            handler.addFilter(other_filters[index])
            server_module.install_secret_redaction(handler)
            server_module.install_secret_redaction(handler)
            handler.handle(logging.LogRecord(
                f"cleanup.handler.{index}", logging.WARNING, "<probe>", 1,
                "confirmation_secret=FAKE_CLEANUP_SECRET safe_id=retained", (), None,
            ))
            assert other_filters[index] in handler.filters
    finally:
        for handler in handlers:
            handler.close()

    assert calls == ["cleanup.handler.0", "cleanup.handler.1"]
    output = buffer.getvalue()
    assert "FAKE_CLEANUP_SECRET" not in output
    assert output.count("safe_id=retained") == 2
    assert output.count("<REDACTED>") == 2
