"""Unit tests for the Codanna-primary CocoIndex shadow relay."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

from mcp.types import CallToolResult, TextContent, Tool


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "codanna_shadow.py"
SPEC = importlib.util.spec_from_file_location("codanna_shadow", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
codanna_shadow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codanna_shadow)


class FakeCodanna:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return type("ToolResult", (), {"tools": [Tool(name="semantic_search_docs", inputSchema={"type": "object"})]})()

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return CallToolResult(
            content=[TextContent(type="text", text='{"data": [{"symbol": "target"}]}')],
            structuredContent={"data": [{"symbol": "target"}]},
        )


def test_primary_result_is_returned_while_shadow_is_logged(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(
            codanna_shadow,
            "_cocoindex_shadow_search",
            lambda query, limit, repo, branch: {"results": [{"filepath": "target.go"}]},
        )
        log_path = tmp_path / "shadow.jsonl"
        primary_session = FakeCodanna()
        relay = codanna_shadow.CodannaShadowRelay(
            codanna=primary_session,
            log_path=log_path,
            workspace=tmp_path,
            cocoindex_repo="kubernaut",
            shadow_timeout=1,
        )

        result = await relay.call_tool("semantic_search_docs", {"query": "target", "limit": 3})
        assert result.structuredContent == {"data": [{"symbol": "target"}]}
        assert primary_session.calls == [("semantic_search_docs", {"query": "target", "limit": 3})]

        for _ in range(20):
            if log_path.exists():
                break
            await asyncio.sleep(0.01)
        entry = json.loads(log_path.read_text().strip())
        assert entry["tool"] == "semantic_search_docs"
        assert entry["primary"]["engine"] == "codanna"
        assert entry["shadow"]["engine"] == "cocoindex"
        assert entry["shadow"]["response"]["results"] == [{"filepath": "target.go"}]

    asyncio.run(run())


def test_formatted_codanna_semantic_output_becomes_structured_content():
    raw = """Found 1 semantically similar result(s) for 'workflow discovery':

1. CompletionObligation (Struct) - Similarity: 0.742
   File: pkg/apifrontend/session/completion.go:5
   Doc: CompletionObligation describes workflow discovery.
   Signature: CompletionObligation struct
"""
    result = CallToolResult(content=[TextContent(type="text", text=raw)])

    structured = codanna_shadow._structured_primary_result(
        "semantic_search_docs",
        {"query": "workflow discovery"},
        result,
    )

    assert structured.structuredContent["schema_version"] == "codanna-shadow.v1"
    assert structured.structuredContent["results"] == [{
        "rank": 1,
        "name": "CompletionObligation",
        "kind": "Struct",
        "score": 0.742,
        "file_path": "pkg/apifrontend/session/completion.go",
        "start_line": 5,
        "end_line": 5,
        "doc_comment": "CompletionObligation describes workflow discovery.",
        "signature": "CompletionObligation struct",
    }]


def test_shadow_failure_does_not_change_primary_result(tmp_path, monkeypatch):
    async def run():
        def fail(*_args):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(codanna_shadow, "_cocoindex_shadow_search", fail)
        log_path = tmp_path / "shadow.jsonl"
        relay = codanna_shadow.CodannaShadowRelay(
            codanna=FakeCodanna(),
            log_path=log_path,
            workspace=tmp_path,
            cocoindex_repo="kubernaut",
            shadow_timeout=1,
        )

        result = await relay.call_tool("semantic_search_docs", {"query": "target"})
        assert result.isError is False

        for _ in range(20):
            if log_path.exists():
                break
            await asyncio.sleep(0.01)
        entry = json.loads(log_path.read_text().strip())
        assert "database unavailable" in entry["shadow"]["error"]

    asyncio.run(run())


def test_non_semantic_codanna_tools_are_forwarded_without_shadow(tmp_path):
    async def run():
        log_path = tmp_path / "shadow.jsonl"
        relay = codanna_shadow.CodannaShadowRelay(
            codanna=FakeCodanna(),
            log_path=log_path,
            workspace=tmp_path,
            cocoindex_repo="kubernaut",
            shadow_timeout=1,
        )

        await relay.call_tool("find_symbol", {"name": "target"})
        await asyncio.sleep(0)
        assert not log_path.exists()

    asyncio.run(run())
