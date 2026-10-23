"""MCP server exposing the read-only tools to Copilot CLI over stdio.

Started by Copilot CLI from the MCP config the adapter passes; the config file, run id and
ledger come from environment variables so one server process serves one run.
"""

from __future__ import annotations

import inspect
import os
import sys
import typing

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError

from patchmig.config import load_config
from patchmig.ledger import LedgerStore
from patchmig.mcp_server.tools import TOOLS, ToolContext, ToolError

SERVER_NAME = "patchmig"


def build_server(ctx: ToolContext) -> MCPServer:
    server = MCPServer(
        SERVER_NAME,
        instructions="Read-only access to the migration's source and target trees, "
        "the patch ledger and cached Jira tickets.",
        log_level="WARNING",
    )
    for fn in TOOLS:
        # Resolve string annotations here: the wrapper lives in this module, not in tools.py.
        hints = typing.get_type_hints(fn)
        params = [
            p.replace(annotation=hints.get(p.name, p.annotation))
            for p in list(inspect.signature(fn).parameters.values())[1:]  # drop ctx
        ]

        def bound(*args, _fn=fn, **kwargs):
            try:
                return _fn(ctx, *args, **kwargs)
            except ToolError as e:  # shown to the agent, unlike unexpected crashes
                raise McpToolError(str(e)) from e

        bound.__signature__ = inspect.Signature(params, return_annotation=hints.get("return"))
        bound.__name__ = fn.__name__
        server.add_tool(bound, name=fn.__name__, description=inspect.getdoc(fn))
    return server


def server_config(config_path: str, run_id: int) -> dict:
    """The MCP server entry the adapter passes to Copilot CLI."""
    return {
        "type": "local",
        "command": sys.executable,
        "args": ["-m", "patchmig.mcp_server"],
        "env": {"PATCHMIG_CONFIG": str(config_path), "PATCHMIG_RUN": str(run_id)},
        "tools": ["*"],
    }


def main() -> None:
    config = load_config(os.environ["PATCHMIG_CONFIG"])
    ctx = ToolContext(
        config=config,
        store=LedgerStore.open(config.ledger_path),
        run_id=int(os.environ["PATCHMIG_RUN"]),
    )
    build_server(ctx).run("stdio")
