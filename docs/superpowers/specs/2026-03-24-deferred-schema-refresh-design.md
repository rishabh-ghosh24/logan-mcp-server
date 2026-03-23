# Deferred Schema Refresh — Design Spec

## Problem

MCP clients (Codex, Claude Code) cannot connect to the logan MCP server via stdio because the MCP handshake times out. The server performs expensive initialization — 7 sequential OCI API calls in `refresh_schema()` — before opening the stdio transport. This takes 10-30+ seconds, exceeding the typical 10-second handshake timeout.

Additionally, if no config file exists, the server launches an interactive setup wizard that reads from stdin, conflicting with the MCP stdio transport and blocking indefinitely.

## Solution

Move schema refresh off the critical startup path. The MCP transport opens immediately after lightweight initialization, and schema refresh runs as a background task.

## New Startup Sequence

**Before:**
```
initialize() → stdio_server() → server.run()
```
`initialize()` includes config loading, OCI auth, handler setup, AND 7 sequential API calls.

**After:**
```
initialize_core() → stdio_server() → background_task(schema_refresh) → server.run()
```

### `initialize_core()`
Runs before stdio transport opens. Only lightweight operations:

1. **Load config** — Read `~/.oci-logan-mcp/config.yaml`. If missing, raise `RuntimeError` with message: "No configuration found. Run 'oci-logan-mcp --setup' to configure the server before using it with an MCP client."
2. **Create CacheManager and QueryLogger** — File I/O only.
3. **Create OCI client** — Auth signer + SDK client instantiation. If auth fails, log warning, set `oci_client = None`. When `oci_client` is `None`, handlers are not created and tool calls return "Server not initialized" — this matches current behavior.
4. **Create ContextManager** — Load existing tenancy context from YAML (file I/O only).
5. **Create MCPHandlers** — In-memory setup of SchemaManager, QueryEngine, etc. Only created if `oci_client` is not `None`.

Expected duration: <2 seconds.

### stdio_server() opens
MCP handshake can now proceed immediately.

### `_refresh_schema_background()`
Launched as `asyncio.create_task()` after entering the stdio context:

```python
async def _refresh_schema_background(self):
    try:
        counts = await asyncio.wait_for(
            self.context_manager.refresh_schema(self.oci_client, self.settings),
            timeout=60
        )
        logger.info(f"Background schema refresh complete: {counts}")
    except asyncio.TimeoutError:
        logger.warning("Background schema refresh timed out after 60s — tools will fetch on demand")
    except Exception as e:
        logger.warning(f"Background schema refresh failed: {e} — tools will fetch on demand")
```

### Tool behavior during loading
No changes needed. `SchemaManager` already fetches on-demand with caching. The background refresh warms the persistent context store (`~/.oci-logan-mcp/context/` YAML files) but is not required for tool functionality.

If a tool is called before background refresh completes, it makes its own API call via `SchemaManager`, caches the result, and returns normally.

## Updated `run()` Method

The rewritten `run()` manages both the schema refresh and keepalive tasks with proper cleanup:

```python
async def run(self):
    await self.initialize_core()
    logger.info("Starting MCP server on stdio...")
    async with stdio_server() as (read_stream, write_stream):
        schema_task = asyncio.create_task(self._refresh_schema_background())
        keepalive_task = asyncio.create_task(self._keepalive_loop())
        try:
            await self.server.run(
                read_stream, write_stream,
                self.server.create_initialization_options()
            )
        finally:
            schema_task.cancel()
            keepalive_task.cancel()
```

Both background tasks are cancelled in the `finally` block to avoid "Task was destroyed but it is pending" warnings on shutdown.

## Setup Wizard Change

The interactive setup wizard (`run_setup_wizard()`) is removed from the normal startup path. It becomes accessible only via a `--setup` CLI flag:

```
oci-logan-mcp --setup    # Run interactive configuration wizard
oci-logan-mcp            # Start MCP server (requires existing config)
```

### Change in `__main__.py`
Add argument parsing with `--setup` flag. When `--setup` is passed, run the wizard and exit. Otherwise, start the MCP server normally.

## Files to Modify

| File | Change |
|---|---|
| `src/oci_logan_mcp/server.py` | Split `initialize()` into `initialize_core()` (no schema refresh, no wizard). Add `_refresh_schema_background()`. Update `run()` to open stdio first, then launch background task. |
| `src/oci_logan_mcp/__main__.py` | Add `--setup` CLI flag for standalone wizard. Default behavior starts MCP server. |

## Files Unchanged

- `context_manager.py` — `refresh_schema()` method unchanged
- `schema_manager.py` — on-demand fetching unchanged
- `tools.py` — tool definitions unchanged
- `client.py` — OCI client unchanged
- All tool handlers — no changes needed

## Acceptance Criteria

1. MCP handshake completes within 2-3 seconds
2. Schema refresh runs in background after transport is live
3. Tools work immediately via on-demand fetching, even before background refresh finishes
4. If no config exists, server exits with a clear error pointing to `--setup`
5. Background refresh has a 60-second timeout to prevent indefinite stalls
6. Background refresh failure is logged as a warning but does not crash the server
7. `oci-logan-mcp --setup` runs the interactive wizard and exits

## Testing

1. Start server with valid config — verify MCP handshake completes in <3 seconds
2. Call `list_log_sources` immediately after connection — verify it returns results (via on-demand fetch)
3. Wait for background refresh to complete — verify log message appears
4. Start server with no config — verify it exits with clear error message
5. Run `oci-logan-mcp --setup` — verify wizard runs interactively
6. Simulate slow network — verify background refresh times out after 60s and server continues working
