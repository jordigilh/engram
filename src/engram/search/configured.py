#!/usr/bin/env python3
"""Configuration-driven code-search MCP server.

This is a thin project-aware launcher around the shared search implementation
in :mod:`engram.search.engram`.  A project is selected at runtime with
``--project`` and all paths/table names come from ``projects.toml``.
"""
from __future__ import annotations

import argparse

from engram.project_config import (
    ProjectAdapterConfig,
    effective_source_tag,
    load_adapter_config,
    source_index_tags,
    source_scope_aliases,
)
from engram.search import engram as engine


def configure(config: ProjectAdapterConfig) -> None:
    if not config.code_table or not config.code_sources:
        raise ValueError(f"project {config.project!r} has no code-search configuration")
    roots = [
        (
            effective_source_tag(source),
            source.root,
            list(source.code_include),
            list(source.code_exclude),
            source.language,
            source.branch,
        )
        for source in config.code_sources
    ]
    scope_aliases = {
        effective_source_tag(source): source_scope_aliases(source)
        for source in config.code_sources
    }
    scope_index_tags = {
        effective_source_tag(source): source_index_tags(source)
        for source in config.code_sources
    }
    # Call-graph queries were retired to the zvec-grep MCP daemon (issue
    # #199); this launcher no longer configures name-based graph roots.
    engine.configure_project(
        project=config.project,
        pg_url=config.pg_dsn,
        code_table=config.code_table,
        embedding_model=config.embedding_model,
        pattern_roots=roots,
        default_branch=config.default_branch,
        scope_aliases=scope_aliases,
        scope_index_tags=scope_index_tags,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Configuration-driven Engram code search")
    parser.add_argument("--project", required=True, help="Project key in projects.toml")
    parser.add_argument("--config", help="Path to projects.toml (defaults to ENGRAM_PROJECTS_CONFIG)")
    parser.add_argument("--query", "-q", help="Run one query and exit")
    parser.add_argument("--pattern", help="Run one structural pattern query and exit")
    parser.add_argument("--language", default=None)
    parser.add_argument("--repo")
    parser.add_argument("--branch")
    parser.add_argument("--limit", "-n", type=int, default=10)
    parser.add_argument("--mode", "-m", default="hybrid", choices=["hybrid", "dense", "bm25"])
    parser.add_argument("--port", "-p", type=int, default=8890)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http"],
        help="MCP server transport (default: stdio)",
    )
    args = parser.parse_args()

    config = load_adapter_config(args.project, args.config)
    configure(config)
    limit = min(max(args.limit, 1), 20)
    default_language = next(
        (source.language for source in config.code_sources if source.language != "auto"),
        "python",
    )
    language = args.language or default_language
    if args.pattern:
        engine._run_cli_pattern_query(args.pattern, language, limit=limit, repo=args.repo, branch=args.branch)
    elif args.query:
        engine._run_cli_query(args.query, limit=limit, mode=args.mode, repo=args.repo, branch=args.branch)
    else:
        engine._run_mcp_server(
            host=args.host,
            port=args.port,
            transport=args.transport,
            project=config.project,
        )


if __name__ == "__main__":
    main()
