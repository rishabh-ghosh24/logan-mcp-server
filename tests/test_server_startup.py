"""Tests for deferred schema refresh and fast MCP startup.

Verifies that:
1. initialize_core() completes without schema refresh
2. Schema refresh runs in background after transport is live
3. Server fails fast if no config exists (no interactive wizard)
4. Background schema refresh handles timeouts and errors gracefully
5. --setup CLI flag runs the wizard standalone
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from oci_logan_mcp.server import OCILogAnalyticsMCPServer


@pytest.fixture
def server():
    """Create a fresh server instance with mocked handlers setup."""
    with patch.object(OCILogAnalyticsMCPServer, '_setup_handlers'):
        srv = OCILogAnalyticsMCPServer()
    return srv


class TestInitializeCore:
    """Tests for initialize_core() — lightweight startup without schema refresh."""

    @pytest.mark.asyncio
    async def test_initialize_core_does_not_call_refresh_schema(self, server):
        """initialize_core() must NOT call refresh_schema — that's deferred to background."""
        mock_context_manager = MagicMock()
        mock_context_manager.refresh_schema = AsyncMock()
        mock_secret_store = MagicMock()
        mock_secret_store.has_secret.return_value = True
        mock_secret_store.is_valid.return_value = True

        with patch('oci_logan_mcp.server.config_exists', return_value=True), \
             patch('oci_logan_mcp.server.load_config') as mock_load, \
             patch('oci_logan_mcp.server.CacheManager'), \
             patch('oci_logan_mcp.server.QueryLogger'), \
             patch('oci_logan_mcp.server.OCILogAnalyticsClient'), \
             patch('oci_logan_mcp.server.ContextManager', return_value=mock_context_manager), \
             patch('oci_logan_mcp.server.SecretStore', return_value=mock_secret_store), \
             patch('oci_logan_mcp.server.AuditLogger'), \
             patch('oci_logan_mcp.server.MCPHandlers'):

            mock_load.return_value = MagicMock()
            await server.initialize_core()

        mock_context_manager.refresh_schema.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_core_sets_up_handlers_when_client_succeeds(self, server):
        """When OCI client initializes successfully, handlers should be created."""
        mock_secret_store = MagicMock()
        mock_secret_store.has_secret.return_value = True
        mock_secret_store.is_valid.return_value = True

        with patch('oci_logan_mcp.server.config_exists', return_value=True), \
             patch('oci_logan_mcp.server.load_config') as mock_load, \
             patch('oci_logan_mcp.server.CacheManager'), \
             patch('oci_logan_mcp.server.QueryLogger'), \
             patch('oci_logan_mcp.server.OCILogAnalyticsClient') as mock_client_cls, \
             patch('oci_logan_mcp.server.ContextManager'), \
             patch('oci_logan_mcp.server.SecretStore', return_value=mock_secret_store), \
             patch('oci_logan_mcp.server.AuditLogger'), \
             patch('oci_logan_mcp.server.MCPHandlers') as mock_handlers_cls:

            mock_load.return_value = MagicMock()
            mock_client_cls.return_value = MagicMock()
            await server.initialize_core()

        assert server.handlers is not None
        mock_handlers_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_initialize_core_no_handlers_when_client_fails(self, server):
        """When OCI client fails, handlers should remain None."""
        mock_secret_store = MagicMock()
        mock_secret_store.has_secret.return_value = True
        mock_secret_store.is_valid.return_value = True

        with patch('oci_logan_mcp.server.config_exists', return_value=True), \
             patch('oci_logan_mcp.server.load_config') as mock_load, \
             patch('oci_logan_mcp.server.CacheManager'), \
             patch('oci_logan_mcp.server.QueryLogger'), \
             patch('oci_logan_mcp.server.OCILogAnalyticsClient', side_effect=Exception("auth failed")), \
             patch('oci_logan_mcp.server.ContextManager'), \
             patch('oci_logan_mcp.server.SecretStore', return_value=mock_secret_store), \
             patch('oci_logan_mcp.server.AuditLogger'):

            mock_load.return_value = MagicMock()
            await server.initialize_core()

        assert server.handlers is None

    @pytest.mark.asyncio
    async def test_initialize_core_allows_missing_secret_in_noninteractive_mode(self, server):
        """Missing secret must not abort MCP startup in non-interactive sessions."""
        mock_secret_store = MagicMock()
        mock_secret_store.has_secret.return_value = False
        mock_secret_store.is_valid.return_value = False

        with patch('oci_logan_mcp.server.config_exists', return_value=True), \
             patch('oci_logan_mcp.server.load_config') as mock_load, \
             patch('oci_logan_mcp.server.CacheManager'), \
             patch('oci_logan_mcp.server.QueryLogger'), \
             patch('oci_logan_mcp.server.OCILogAnalyticsClient') as mock_client_cls, \
             patch('oci_logan_mcp.server.ContextManager'), \
             patch('oci_logan_mcp.server.SecretStore', return_value=mock_secret_store), \
             patch('oci_logan_mcp.server.AuditLogger'), \
             patch('oci_logan_mcp.server.MCPHandlers') as mock_handlers_cls, \
             patch('sys.stdin.isatty', return_value=False):

            mock_load.return_value = MagicMock()
            mock_client_cls.return_value = MagicMock()
            await server.initialize_core()

        assert server.handlers is not None
        mock_handlers_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_initialize_core_allows_invalid_secret_file(self, server):
        """Corrupt secret files should not terminate startup."""
        mock_secret_store = MagicMock()
        mock_secret_store.has_secret.return_value = True
        mock_secret_store.is_valid.return_value = False

        with patch('oci_logan_mcp.server.config_exists', return_value=True), \
             patch('oci_logan_mcp.server.load_config') as mock_load, \
             patch('oci_logan_mcp.server.CacheManager'), \
             patch('oci_logan_mcp.server.QueryLogger'), \
             patch('oci_logan_mcp.server.OCILogAnalyticsClient') as mock_client_cls, \
             patch('oci_logan_mcp.server.ContextManager'), \
             patch('oci_logan_mcp.server.SecretStore', return_value=mock_secret_store), \
             patch('oci_logan_mcp.server.AuditLogger'), \
             patch('oci_logan_mcp.server.MCPHandlers') as mock_handlers_cls:

            mock_load.return_value = MagicMock()
            mock_client_cls.return_value = MagicMock()
            await server.initialize_core()

        assert server.handlers is not None
        mock_handlers_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_initialize_core_fails_fast_when_no_config(self, server):
        """If no config exists, initialize_core() should raise RuntimeError, not launch wizard."""
        with patch('oci_logan_mcp.server.config_exists', return_value=False):
            with pytest.raises(RuntimeError, match="No configuration found"):
                await server.initialize_core()


class TestBackgroundSchemaRefresh:
    """Tests for _refresh_schema_background() — background task after transport is live."""

    @pytest.mark.asyncio
    async def test_background_refresh_calls_refresh_schema(self, server):
        """Background task should call context_manager.refresh_schema()."""
        server.context_manager = MagicMock()
        server.context_manager.refresh_schema = AsyncMock(return_value={"sources": 10})
        server.oci_client = MagicMock()
        server.settings = MagicMock()

        await server._refresh_schema_background()

        server.context_manager.refresh_schema.assert_called_once_with(
            server.oci_client, server.settings
        )

    @pytest.mark.asyncio
    async def test_background_refresh_handles_timeout(self, server):
        """If schema refresh exceeds timeout, it should log warning and not crash."""
        async def slow_refresh(*args, **kwargs):
            await asyncio.sleep(999)

        server.context_manager = MagicMock()
        server.context_manager.refresh_schema = slow_refresh
        server.oci_client = MagicMock()
        server.settings = MagicMock()

        # Should not raise — should handle timeout gracefully
        # We patch the timeout to be very short for testing
        with patch('oci_logan_mcp.server.SCHEMA_REFRESH_TIMEOUT', 0.1):
            await server._refresh_schema_background()

    @pytest.mark.asyncio
    async def test_background_refresh_handles_exception(self, server):
        """If schema refresh raises, it should log warning and not crash."""
        server.context_manager = MagicMock()
        server.context_manager.refresh_schema = AsyncMock(
            side_effect=Exception("OCI API error")
        )
        server.oci_client = MagicMock()
        server.settings = MagicMock()

        # Should not raise
        await server._refresh_schema_background()


class TestRunStartupOrder:
    """Tests for run() — verifying stdio opens before schema refresh."""

    @pytest.mark.asyncio
    async def test_run_opens_stdio_before_schema_refresh(self, server):
        """run() must enter stdio_server() context BEFORE launching schema refresh."""
        call_order = []

        async def mock_initialize_core():
            call_order.append('initialize_core')
            server.oci_client = MagicMock()  # enable schema refresh path

        original_refresh = None

        async def mock_schema_refresh():
            call_order.append('schema_refresh')

        server.initialize_core = mock_initialize_core
        server._refresh_schema_background = mock_schema_refresh
        server.server = MagicMock()

        async def mock_server_run(*args, **kwargs):
            call_order.append('server_run')
            # Give background tasks a chance to run
            await asyncio.sleep(0.01)

        server.server.run = mock_server_run
        server.server.create_initialization_options = MagicMock()

        with patch('oci_logan_mcp.server.ENABLE_STARTUP_SCHEMA_REFRESH', True), \
             patch('oci_logan_mcp.server.stdio_server') as mock_stdio:
            mock_read = MagicMock()
            mock_write = MagicMock()

            async def mock_aenter(*args):
                call_order.append('stdio_opened')
                return (mock_read, mock_write)

            cm = MagicMock()
            cm.__aenter__ = mock_aenter
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_stdio.return_value = cm

            server._keepalive_loop = AsyncMock()

            await server.run()

        # Verify ordering: initialize_core → stdio_opened → schema_refresh
        assert call_order.index('initialize_core') < call_order.index('stdio_opened')
        assert call_order.index('stdio_opened') < call_order.index('schema_refresh')

    @pytest.mark.asyncio
    async def test_run_skips_schema_refresh_by_default(self, server):
        """run() should skip startup schema refresh unless explicitly enabled."""
        async def mock_initialize_core():
            server.oci_client = MagicMock()

        server.initialize_core = mock_initialize_core
        server._refresh_schema_background = AsyncMock()
        server.server = MagicMock()
        server.server.run = AsyncMock()
        server.server.create_initialization_options = MagicMock()
        server._keepalive_loop = AsyncMock()

        with patch('oci_logan_mcp.server.ENABLE_STARTUP_SCHEMA_REFRESH', False), \
             patch('oci_logan_mcp.server.stdio_server') as mock_stdio:
            mock_read = MagicMock()
            mock_write = MagicMock()

            async def mock_aenter(*args):
                return (mock_read, mock_write)

            cm = MagicMock()
            cm.__aenter__ = mock_aenter
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_stdio.return_value = cm

            await server.run()

        server._refresh_schema_background.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_starts_schema_refresh_when_enabled(self, server):
        """run() should start schema refresh when the startup flag is enabled."""
        async def mock_initialize_core():
            server.oci_client = MagicMock()

        server.initialize_core = mock_initialize_core
        server._refresh_schema_background = AsyncMock()
        server.server = MagicMock()

        async def mock_server_run(*args, **kwargs):
            await asyncio.sleep(0.01)

        server.server.run = mock_server_run
        server.server.create_initialization_options = MagicMock()
        server._keepalive_loop = AsyncMock()

        with patch('oci_logan_mcp.server.ENABLE_STARTUP_SCHEMA_REFRESH', True), \
             patch('oci_logan_mcp.server.stdio_server') as mock_stdio:
            mock_read = MagicMock()
            mock_write = MagicMock()

            async def mock_aenter(*args):
                return (mock_read, mock_write)

            cm = MagicMock()
            cm.__aenter__ = mock_aenter
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_stdio.return_value = cm

            await server.run()

        server._refresh_schema_background.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_skips_schema_refresh_in_read_only_mode(self, server):
        """run() must not start _refresh_schema_background when read_only=True,
        even if ENABLE_STARTUP_SCHEMA_REFRESH is on — the background path writes
        to the shared tenancy-context YAML, which violates the read-only promise.
        """
        from oci_logan_mcp.config import Settings

        async def mock_initialize_core():
            server.oci_client = MagicMock()
            server.settings = Settings(read_only=True)

        server.initialize_core = mock_initialize_core
        server._refresh_schema_background = AsyncMock()
        server.server = MagicMock()
        server.server.run = AsyncMock()
        server.server.create_initialization_options = MagicMock()
        server._keepalive_loop = AsyncMock()

        with patch('oci_logan_mcp.server.ENABLE_STARTUP_SCHEMA_REFRESH', True), \
             patch('oci_logan_mcp.server.stdio_server') as mock_stdio:
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_stdio.return_value = cm

            await server.run()

        server._refresh_schema_background.assert_not_called()


class TestSetupCLIFlag:
    """Tests for --setup CLI flag."""

    def test_setup_flag_runs_wizard(self):
        """oci-logan-mcp --setup should run the setup wizard and exit."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('oci_logan_mcp.__main__.run_setup_wizard') as mock_wizard, \
             patch('sys.argv', ['oci-logan-mcp', '--setup']):
            mock_wizard.return_value = MagicMock()
            with pytest.raises(SystemExit) as exc_info:
                cli_main()
            assert exc_info.value.code == 0
            mock_wizard.assert_called_once()

    def test_no_flag_starts_server(self):
        """oci-logan-mcp without --setup should start the MCP server."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('sys.argv', ['oci-logan-mcp']), \
             patch('oci_logan_mcp.__main__.server_main') as mock_server_main:
            cli_main()
            mock_server_main.assert_called_once()


class TestPromoteCLIFlag:
    """Tests for --promote-and-exit CLI flag."""

    def test_promote_calls_promote_all_and_exits_zero(self):
        """--promote-and-exit should call promote_all once and exit 0."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('sys.argv', ['oci-logan-mcp', '--promote-and-exit']), \
             patch('oci_logan_mcp.__main__.promote_all', return_value={"promoted": 3, "scanned_users": 2}) as mock_promote:
            with pytest.raises(SystemExit) as exc_info:
                cli_main()
            assert exc_info.value.code == 0
            mock_promote.assert_called_once()

    def test_promote_with_base_dir(self, tmp_path):
        """--promote-and-exit --base-dir passes the correct Path."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('sys.argv', ['oci-logan-mcp', '--promote-and-exit', '--base-dir', str(tmp_path)]), \
             patch('oci_logan_mcp.__main__.promote_all', return_value={"promoted": 0, "scanned_users": 0}) as mock_promote:
            with pytest.raises(SystemExit) as exc_info:
                cli_main()
            assert exc_info.value.code == 0
            mock_promote.assert_called_once_with(tmp_path)

    def test_promote_does_not_start_server(self):
        """--promote-and-exit must not call server_main."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('sys.argv', ['oci-logan-mcp', '--promote-and-exit']), \
             patch('oci_logan_mcp.__main__.promote_all', return_value={"promoted": 0, "scanned_users": 0}), \
             patch('oci_logan_mcp.__main__.server_main') as mock_server:
            with pytest.raises(SystemExit):
                cli_main()
            mock_server.assert_not_called()

    def test_promote_exception_exits_one(self):
        """Promotion failure should exit 1."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('sys.argv', ['oci-logan-mcp', '--promote-and-exit']), \
             patch('oci_logan_mcp.__main__.promote_all', side_effect=RuntimeError("disk full")):
            with pytest.raises(SystemExit) as exc_info:
                cli_main()
            assert exc_info.value.code == 1

    def test_setup_and_promote_rejected(self):
        """--setup and --promote-and-exit together should be rejected."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('sys.argv', ['oci-logan-mcp', '--setup', '--promote-and-exit']):
            with pytest.raises(SystemExit) as exc_info:
                cli_main()
            assert exc_info.value.code == 2  # argparse error exit code

    def test_user_and_promote_rejected(self):
        """--user with --promote-and-exit should be rejected."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('sys.argv', ['oci-logan-mcp', '--promote-and-exit', '--user', 'alice']):
            with pytest.raises(SystemExit) as exc_info:
                cli_main()
            assert exc_info.value.code == 2

    def test_base_dir_without_promote_rejected(self):
        """--base-dir without --promote-and-exit should be rejected."""
        from oci_logan_mcp.__main__ import main as cli_main
        with patch('sys.argv', ['oci-logan-mcp', '--base-dir', '/tmp/x']):
            with pytest.raises(SystemExit) as exc_info:
                cli_main()
            assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_initialize_core_passes_uuid_session_id_to_audit_logger():
    """initialize_core must construct AuditLogger with a 32-char hex session_id."""
    import re
    with patch.object(OCILogAnalyticsMCPServer, '_setup_handlers'):
        srv = OCILogAnalyticsMCPServer()

    captured_calls = []

    class CapturingAuditLogger:
        def __init__(self, **kwargs):
            captured_calls.append(kwargs)

    mock_secret_store = MagicMock()
    mock_secret_store.has_secret.return_value = True
    mock_secret_store.is_valid.return_value = True

    with patch('oci_logan_mcp.server.config_exists', return_value=True), \
         patch('oci_logan_mcp.server.load_config') as mock_load, \
         patch('oci_logan_mcp.server.CacheManager'), \
         patch('oci_logan_mcp.server.QueryLogger'), \
         patch('oci_logan_mcp.server.OCILogAnalyticsClient'), \
         patch('oci_logan_mcp.server.ContextManager'), \
         patch('oci_logan_mcp.server.SecretStore', return_value=mock_secret_store), \
         patch('oci_logan_mcp.server.AuditLogger', side_effect=lambda **kw: CapturingAuditLogger(**kw)), \
         patch('oci_logan_mcp.server.MCPHandlers'):
        mock_load.return_value = MagicMock()
        await srv.initialize_core()

    assert captured_calls, "AuditLogger was never called"
    sid = captured_calls[0].get("session_id")
    assert sid is not None, "session_id not passed to AuditLogger"
    assert re.fullmatch(r"[0-9a-f]{32}", sid), (
        f"Expected 32-char hex session_id, got: {sid!r}"
    )
