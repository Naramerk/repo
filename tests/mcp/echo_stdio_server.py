"""Minimal stdio MCP echo server for ToolRouter / mcp_live integration tests."""

from __future__ import annotations

import json
import sys


def reply(req: dict[str, object], result: dict[str, object]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        req = json.loads(line)
        method = req.get("method")
        if method == "initialize":
            reply(
                req,
                {
                    "protocolVersion": req["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo", "version": "0"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(
                req,
                {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "echo",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                            },
                        }
                    ]
                },
            )
        elif method == "tools/call":
            text = req["params"]["arguments"]["text"]
            reply(
                req,
                {"content": [{"type": "text", "text": str(text)}], "isError": False},
            )
        else:
            raise SystemExit(f"unexpected {method}")


if __name__ == "__main__":
    main()
