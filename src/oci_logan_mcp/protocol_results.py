"""Shared MCP result construction for sending and measuring tool responses."""
from mcp.types import CallToolResult, ImageContent, TextContent


def build_call_tool_result(results: list[dict]) -> CallToolResult:
    """Preserve tool content and expose handler failures to MCP clients."""
    return CallToolResult(
        content=[
            ImageContent(type="image", data=result["data"], mimeType=result.get("mimeType", "image/png"))
            if result["type"] == "image" else TextContent(type="text", text=result["text"])
            for result in results
        ],
        isError=any(result.get("is_error", False) for result in results),
    )
