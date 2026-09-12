"""mcp SDK 1.x/2.x compatibility (2026-09-09).

The live venv pins mcp<2.0 because fastmcp 3.4.7 needs
``mcp.server.lowlevel.server.request_ctx``, removed in mcp 2.0 (without it
hindsight-api fails to start). But this code was written against mcp 2.x,
which renamed ``mcp.server.fastmcp.FastMCP`` to
``mcp.server.mcpserver.MCPServer`` and moved host/port from the constructor
to ``run()``. These helpers speak both APIs; every MCP server entry point
in this package must use them instead of importing either class directly.

Delete this module (inlining the 2.x form) once fastmcp supports mcp 2.x --
see the mcp pin in pyproject.toml.
"""

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _ServerCls

    _V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerCls

    _V2 = False


def make_server(name: str, host: str = "127.0.0.1", port: int | None = None):
    """Construct a server; host/port go to the constructor on 1.x, run() on 2.x."""
    if _V2 or port is None:
        return _ServerCls(name)
    return _ServerCls(name, host=host, port=port)


def run_server(server, transport: str = "stdio",
               host: str = "127.0.0.1", port: int | None = None) -> None:
    """Run a server; host/port go to run() on 2.x only."""
    if _V2 and transport != "stdio":
        server.run(transport=transport, host=host, port=port)
    else:
        server.run(transport=transport)


def tool_input_schema(tool) -> dict:
    """Tool input schema, tolerant of the 1.x/2.x attribute rename."""
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", {})
    return schema or {}


def call_tool_is_error(result) -> bool:
    """CallToolResult error flag, tolerant of the 1.x/2.x rename.

    mcp 2.0 renamed ``CallToolResult.isError`` to ``is_error``; the live
    venv pins mcp<2.0 so the result only has ``isError``. The stdio adapter
    in engram_gateway reads this per call, so it must not pin one spelling.
    """
    if hasattr(result, "is_error"):
        return bool(result.is_error)
    return bool(getattr(result, "isError", False))
