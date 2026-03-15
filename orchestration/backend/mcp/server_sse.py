#  Orchestration Engine - MCP SSE Server
#
#  Runs the MCP server over SSE transport on port 5201
#  for remote Claude Code agents to connect over the network.
#
#  Usage: python backend/mcp/server_sse.py
#
#  Depends on: backend/mcp/server.py (create_server)
#  Used by:    Remote Claude Code agents, Hekate VSCode extension

import logging
import os
import sys

from server import create_server

logging.basicConfig(
    level=logging.INFO,
    format="[orchestration-mcp-sse] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

if __name__ == "__main__":
    host = os.environ.get("MCP_SSE_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_SSE_PORT", "5201"))
    logging.info("Starting MCP SSE server on %s:%d", host, port)
    server = create_server()
    server.run(transport="sse", host=host, port=port)
