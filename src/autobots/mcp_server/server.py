"""Standalone entrypoint for the Autobots MCP server.

The MCP server is normally mounted into the main FastAPI app at /v1/mcp
(see main.py). This module runs it on its own, which is useful for testing the
MCP surface without booting the whole API:

    python -m src.autobots.mcp_server

`create_mcp_server` is re-exported here for backward compatibility.
"""

from src.autobots.core.settings import SettingsProvider
from src.autobots.mcp_server import create_mcp_server  # noqa: F401  (back-compat re-export)

# Default port for the standalone server: 8000 is the main app, 8001 the scheduler.
_DEFAULT_PORT = 8002


def main() -> None:
    """Run the MCP server standalone under uvicorn."""
    import uvicorn

    # create_mcp_server() reads Supabase settings, so settings must be loaded first.
    SettingsProvider.set()

    uvicorn.run(
        create_mcp_server(),
        host="0.0.0.0",
        port=_DEFAULT_PORT,
        lifespan="on",
    )


if __name__ == "__main__":
    main()
