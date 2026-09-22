#!/usr/bin/env python3
"""Codanna-primary MCP relay with asynchronous CocoIndex shadow logging.

The relay preserves Codanna's tool definitions and primary result. For the
semantic search tools, it starts a bounded background CocoIndex comparison and
writes both responses plus request metadata to a JSONL file. A shadow failure
never changes the result returned to the MCP client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import re
import subprocess
import sys
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool


SHADOWED_TOOLS = frozenset({"semantic_search_docs", "semantic_search_with_context"})
DEFAULT_SHADOW_TIMEOUT_SECONDS = 30.0
log = logging.getLogger("codanna-shadow")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def _git_metadata(workspace: pathlib.Path) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {"workspace": str(workspace), "branch": None, "commit": None}
    for key, command in (
        ("branch", ["git", "-C", str(workspace), "branch", "--show-current"]),
        ("commit", ["git", "-C", str(workspace), "rev-parse", "HEAD"]),
    ):
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            metadata[key] = completed.stdout.strip()
    return metadata


def _write_jsonl(path: pathlib.Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, default=str, separators=(",", ":")) + "\n")


def _parse_location(value: str) -> dict[str, Any]:
    match = re.match(r"(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$", value.strip())
    if not match:
        return {"file_path": value.strip()}
    return {
        "file_path": match.group("path"),
        "start_line": int(match.group("start")),
        "end_line": int(match.group("end") or match.group("start")),
    }


def _parse_codanna_semantic_text(tool: str, query: str, text: str) -> dict[str, Any]:
    """Turn Codanna's current human-readable MCP output into stable records.

    Codanna's direct CLI supports JSON, but its MCP server currently renders
    semantic results as text. Keep this parser deliberately narrow and retain
    the raw response in the comparison log if the format ever changes.
    """
    results: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    mode: str | None = None
    for line in text.splitlines():
        docs_header = re.match(
            r"^\s*(\d+)\.\s+(.+?)\s+\((.+?)\)\s+-\s+Similarity:\s+([0-9.]+)$",
            line,
        )
        context_header = re.match(
            r"^\s*(\d+)\.\s+(.+?)\s+-\s+(.+?)\s+at\s+(.+?):(\d+)-(\d+)\s+\[symbol_id:(\d+)\]$",
            line,
        )
        if docs_header or context_header:
            if current is not None:
                results.append(current)
            if docs_header:
                current = {
                    "rank": int(docs_header.group(1)),
                    "name": docs_header.group(2),
                    "kind": docs_header.group(3),
                    "score": float(docs_header.group(4)),
                }
                mode = "docs"
            else:
                current = {
                    "rank": int(context_header.group(1)),
                    "name": context_header.group(2),
                    "kind": context_header.group(3),
                    "file_path": context_header.group(4),
                    "start_line": int(context_header.group(5)),
                    "end_line": int(context_header.group(6)),
                    "symbol_id": int(context_header.group(7)),
                }
                mode = "context"
            continue
        if current is None:
            continue

        if mode == "docs":
            match = re.match(r"^\s+File:\s+(.+)$", line)
            if match:
                current.update(_parse_location(match.group(1)))
                continue
            for label, key in (("Doc", "doc_comment"), ("Signature", "signature")):
                match = re.match(rf"^\s+{label}:\s*(.*)$", line)
                if match:
                    current[key] = match.group(1)
                    break
        else:
            match = re.match(r"^\s+Similarity Score:\s+([0-9.]+)$", line)
            if match:
                current["score"] = float(match.group(1))
                continue
            match = re.match(r"^\s+Documentation:\s*$", line)
            if match:
                current["doc_comment"] = ""
                continue
            match = re.match(r"^\s+Signature:\s*(.*)$", line)
            if match:
                current["signature"] = match.group(1)
                continue
            if "doc_comment" in current and line.startswith("     ") and "signature" not in current:
                current["doc_comment"] = f"{current['doc_comment']} {line.strip()}".strip()
            elif line.strip() and not line.lstrip().startswith("---"):
                current.setdefault("context_lines", []).append(line.strip())

    if current is not None:
        results.append(current)
    return {
        "schema_version": "codanna-shadow.v1",
        "engine": "codanna",
        "tool": tool,
        "query": query,
        "results": results,
    }


def _structured_primary_result(name: str, arguments: dict[str, Any], result: CallToolResult) -> CallToolResult:
    if name not in SHADOWED_TOOLS or result.isError or result.structuredContent is not None:
        return result
    text = "\n".join(content.text for content in result.content if isinstance(content, TextContent))
    if not text:
        return result
    query = arguments.get("query", "")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        structured = _parse_codanna_semantic_text(name, query, text)
    else:
        structured = decoded if isinstance(decoded, dict) else _parse_codanna_semantic_text(name, query, text)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(structured, separators=(",", ":")))],
        structuredContent=structured,
        isError=result.isError,
    )


def _cocoindex_shadow_search(
    query: str,
    limit: int,
    repo: str | None,
    branch: str | None,
) -> dict[str, Any]:
    # Import lazily so the Codanna relay can still start if the optional
    # CocoIndex runtime is unavailable.
    from engram.search import kubernaut as cocoindex

    results = cocoindex.search_code(
        query,
        limit=min(max(limit, 1), 20),
        repo=repo,
        branch=branch,
    )
    return {
        "results": results,
        "formatted": cocoindex._format_results(query, results),
    }


class CodannaShadowRelay:
    def __init__(
        self,
        *,
        codanna: ClientSession,
        log_path: pathlib.Path,
        workspace: pathlib.Path,
        cocoindex_repo: str | None,
        shadow_timeout: float,
    ) -> None:
        self.codanna = codanna
        self.log_path = log_path
        self.workspace = workspace
        self.cocoindex_repo = cocoindex_repo
        self.shadow_timeout = shadow_timeout
        self._shadow_tasks: set[asyncio.Task[None]] = set()
        self._shadow_lock = asyncio.Lock()

    async def list_tools(self) -> list[Tool]:
        result = await self.codanna.list_tools()
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        started = time.perf_counter()
        raw_primary = await self.codanna.call_tool(name, arguments)
        primary = _structured_primary_result(name, arguments, raw_primary)
        primary_elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        if name in SHADOWED_TOOLS:
            task = asyncio.create_task(
                self._shadow_call(
                    name=name,
                    arguments=arguments,
                    primary=primary,
                    raw_primary=raw_primary,
                    primary_elapsed_ms=primary_elapsed_ms,
                )
            )
            self._shadow_tasks.add(task)
            task.add_done_callback(self._shadow_tasks.discard)

        return primary

    async def _shadow_call(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        primary: CallToolResult,
        raw_primary: CallToolResult,
        primary_elapsed_ms: float,
    ) -> None:
        started = time.perf_counter()
        entry: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": name,
            "arguments": arguments,
            "primary": {
                "engine": "codanna",
                "elapsed_ms": primary_elapsed_ms,
                "response": _jsonable(primary),
                "raw_response": _jsonable(raw_primary),
            },
            "shadow": {"engine": "cocoindex", "tool": "cocoindex_search"},
            **_git_metadata(self.workspace),
        }

        try:
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("Codanna semantic search call had no non-empty query")
            limit = arguments.get("limit", 5)
            if not isinstance(limit, int):
                raise ValueError("Codanna semantic search limit was not an integer")
            repo = arguments.get("repo", self.cocoindex_repo)
            branch = arguments.get("branch")
            async with self._shadow_lock:
                shadow = await asyncio.wait_for(
                    asyncio.to_thread(_cocoindex_shadow_search, query, limit, repo, branch),
                    timeout=self.shadow_timeout,
                )
            entry["shadow"].update({
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "response": shadow,
            })
        except Exception as exc:  # noqa: BLE001 - shadowing must never affect primary behavior
            entry["shadow"].update({
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": f"{type(exc).__name__}: {exc}",
            })
            log.warning("CocoIndex shadow call failed for %s: %s", name, exc)

        try:
            await asyncio.to_thread(_write_jsonl, self.log_path, entry)
        except Exception:  # noqa: BLE001 - logging must never affect primary behavior
            log.warning("failed to write shadow comparison to %s", self.log_path, exc_info=True)

    async def drain(self) -> None:
        """Finish already-started comparisons during graceful MCP shutdown."""
        if self._shadow_tasks:
            await asyncio.gather(*self._shadow_tasks, return_exceptions=True)


async def _run(args: argparse.Namespace) -> None:
    server = Server("codanna-shadow")
    async with AsyncExitStack() as stack:
        params = StdioServerParameters(
            command=args.codanna,
            args=["--config", args.config, "serve", "--watch", "--watch-interval", str(args.watch_interval)],
            env=None,
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        codanna = await stack.enter_async_context(ClientSession(read, write))
        await codanna.initialize()
        relay = CodannaShadowRelay(
            codanna=codanna,
            log_path=args.log,
            workspace=pathlib.Path.cwd(),
            cocoindex_repo=args.cocoindex_repo,
            shadow_timeout=args.shadow_timeout,
        )

        @server.list_tools()
        async def list_tools() -> list[Tool]:
            return await relay.list_tools()

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
            return await relay.call_tool(name, arguments)

        read_stream, write_stream = await stack.enter_async_context(stdio_server())
        try:
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
        finally:
            await relay.drain()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codanna", default="codanna")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log", type=pathlib.Path, required=True)
    parser.add_argument("--cocoindex-repo", default="kubernaut")
    parser.add_argument("--shadow-timeout", type=float, default=DEFAULT_SHADOW_TIMEOUT_SECONDS)
    parser.add_argument("--watch-interval", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
