"""CLI flag tests for oci_logan_mcp.__main__."""

import os
import sys


def test_read_only_flag_sets_env_var(monkeypatch):
    from oci_logan_mcp import __main__ as main_mod
    monkeypatch.delenv("OCI_LOGAN_MCP_READ_ONLY", raising=False)

    captured = {}

    def fake_server_main():
        captured["env"] = os.environ.get("OCI_LOGAN_MCP_READ_ONLY")

    monkeypatch.setattr(sys, "argv", ["oci-logan-mcp", "--read-only"])
    monkeypatch.setattr(main_mod, "server_main", fake_server_main)
    main_mod.main()

    assert captured["env"] == "1"


def test_no_read_only_flag_leaves_env_unset(monkeypatch):
    from oci_logan_mcp import __main__ as main_mod
    monkeypatch.delenv("OCI_LOGAN_MCP_READ_ONLY", raising=False)

    captured = {}

    def fake_server_main():
        captured["env"] = os.environ.get("OCI_LOGAN_MCP_READ_ONLY")

    monkeypatch.setattr(sys, "argv", ["oci-logan-mcp"])
    monkeypatch.setattr(main_mod, "server_main", fake_server_main)
    main_mod.main()

    assert captured["env"] is None


def test_read_only_with_setup_is_rejected(monkeypatch):
    from oci_logan_mcp import __main__ as main_mod
    import pytest
    monkeypatch.setattr(sys, "argv", ["oci-logan-mcp", "--read-only", "--setup"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code != 0


def test_read_only_with_promote_is_rejected(monkeypatch):
    from oci_logan_mcp import __main__ as main_mod
    import pytest
    monkeypatch.setattr(sys, "argv", ["oci-logan-mcp", "--read-only", "--promote-and-exit"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code != 0
