#!/usr/bin/env python3
"""Configuration-driven code-search MCP server.

This is a thin project-aware launcher around the shared search implementation
in :mod:`engram.search.engram`.  A project is selected at runtime with
``--project`` and all paths/table names come from ``projects.toml``.
"""
from __future__ import annotations

import argparse

from engram.project_config import ProjectAdapterConfig, load_adapter_config
from engram.search import engram as engine


def configure(config: ProjectAdapterConfig) -> None:
    if not config.code_table or not config.code_sources:
        raise ValueError(f"project {config.project!r} has no code-search configuration")
    roots = [
        (
            source.tag,
            source.root,
            list(source.code_include),
            list(source.code_exclude),
        )
        for source in config.code_sources
    ]
    engine.configure_project(
        project=config.project,
        pg_url=config.pg_dsn,
        code_table=config.code_table,
        embedding_model=config.embedding_model,
        pattern_roots=roots,
        call_graph_root=roots[0][1],
        call_graph_language=config.code_sources[0].language,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Configuration-driven Engram code search")
    parser.add_argument("--project", required=True, help="Project key in projects.toml")
    parser.add_argument("--config", help="Path to projects.toml (defaults to ENGRAM_PROJECTS_CONFIG)")
    parser.add_argument("--query", "-q", help="Run one query and exit")
    parser.add_argument("--pattern", help="Run one structural pattern query and exit")
    parser.add_argument("--language", default="python")
    parser.add_argument("--limit", "-n", type=int, default=10)
    parser.add_argument("--mode", "-m", default="hybrid", choices=["hybrid", "dense", "bm25"])
    parser.add_argument("--blast-radius")
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--shortest-path", nargs=2, metavar=("SOURCE", "TARGET"))
    parser.add_argument("--cluster")
    parser.add_argument("--port", "-p", type=int, default=8890)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    config = load_adapter_config(args.project, args.config)
    configure(config)
    limit = min(max(args.limit, 1), 20)
    if args.pattern:
        engine._run_cli_pattern_query(args.pattern, args.language, limit=limit)
    elif args.blast_radius:
        engine._run_cli_blast_radius(args.blast_radius, depth=args.depth)
    elif args.shortest_path:
        engine._run_cli_shortest_path(*args.shortest_path)
    elif args.cluster:
        engine._run_cli_cluster(args.cluster)
    elif args.query:
        engine._run_cli_query(args.query, limit=limit, mode=args.mode)
    else:
        engine._run_mcp_server(host=args.host, port=args.port, project=config.project)


if __name__ == "__main__":
    main()
