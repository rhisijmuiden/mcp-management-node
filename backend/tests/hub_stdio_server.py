"""A tiny MCP server over stdio, started by test_hub.py as an MCP hub upstream."""

import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

srv = MCPServer(name="stdio-test", instructions="Test server")


@srv.tool(annotations=ToolAnnotations(readOnlyHint=True))
def whoami() -> str:
    """Says which token the server got through its environment."""
    return f"token={os.environ.get('TEST_TOKEN', '')} mode={os.environ.get('TEST_MODE', '')}"


if __name__ == "__main__":
    srv.run("stdio")
