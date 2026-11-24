#!/usr/bin/env python3
"""Minimal newline-delimited MCP stdio server for Rust adapter tests.

Test-only fixture. Controlled by env vars:
  STDIO_MOCK_MODE: normal | old-protocol | paginate | request-then-respond |
                   slow | die-once | malformed | jsonrpc-error | list-error
  STDIO_MOCK_LOG: optional path for flushed method/pid lines
  STDIO_MOCK_STATE: optional marker path for die-once
"""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("STDIO_MOCK_MODE", "normal")
LOG = os.environ.get("STDIO_MOCK_LOG")
STATE = os.environ.get("STDIO_MOCK_STATE")


def log(message: str) -> None:
    if not LOG:
        return
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{message}\n")
        handle.flush()


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def tool(name: str) -> dict:
    definition = {"name": name, "inputSchema": {"type": "object", "properties": {}}}
    if name == "alpha":
        definition["description"] = "Alpha tool"
    return definition


def tools_page(cursor: object) -> dict:
    if MODE == "paginate":
        if not cursor:
            return {"tools": [tool("alpha")], "nextCursor": "page-2"}
        return {"tools": [tool("beta")]}
    return {"tools": [tool("alpha"), tool("beta")]}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            log(f"start {os.getpid()}")
            protocol = "2024-11-05" if MODE == "old-protocol" else "2025-06-18"
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": protocol,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "stdio-mock", "version": "1"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            log(f"list {os.getpid()}")
            if MODE == "list-error":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "error": {"code": -32601, "message": "tools/list rejected"},
                    }
                )
                continue
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": tools_page(params.get("cursor")),
                }
            )
        elif method == "tools/call":
            log(f"call {os.getpid()} {params.get('name')}")
            if MODE == "die-once" and STATE and not os.path.exists(STATE):
                with open(STATE, "w", encoding="utf-8") as marker:
                    marker.write("1")
                return
            if MODE == "jsonrpc-error":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "error": {"code": -32602, "message": "rejected by mock"},
                    }
                )
                continue
            if MODE == "malformed":
                sys.stdout.write("this-is-not-json\n")
                sys.stdout.flush()
                return
            if MODE == "request-then-respond":
                send({"jsonrpc": "2.0", "id": "server-request-1", "method": "roots/list"})
            if MODE == "slow":
                time.sleep(1.0)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": "echo", "annotations": None}
                        ],
                        "structuredContent": {"ignored": True},
                        "isError": False,
                    },
                }
            )
        elif message_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )


if __name__ == "__main__":
    main()
