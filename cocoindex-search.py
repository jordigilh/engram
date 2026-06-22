#!/usr/bin/env python3
"""CocoIndex code search MCP server.

Provides semantic code search over the cocoindex.code_embeddings pgvector table
as an MCP tool that Cursor can call during sessions.

Usage:
    python3 cocoindex-search.py                    # Start MCP server on :8889
    python3 cocoindex-search.py --port 9000        # Custom port
    python3 cocoindex-search.py --query "how does the reconciler handle errors"
"""

import argparse
import logging
import os
import sys
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("cocoindex-search")

PG_URL = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_model = None


def _get_model():
    """Lazily load the SentenceTransformer model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL)
        log.info("Loaded embedding model: %s", EMBEDDING_MODEL)
    return _model


def _embed_query(query: str) -> list[float]:
    model = _get_model()
    return model.encode(query, normalize_embeddings=True).tolist()


def search_code(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search the code_embeddings table using cosine similarity."""
    import psycopg2

    embedding = _embed_query(query)
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

    conn = psycopg2.connect(PG_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT filepath, chunk_index, code,
                       1 - (embedding <=> %s::vector) AS score
                FROM cocoindex.code_embeddings
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (embedding_str, embedding_str, limit),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "filepath": row[0],
            "chunk_index": row[1],
            "code": row[2],
            "score": round(float(row[3]), 4),
        }
        for row in rows
    ]


def _format_results(query: str, results: list[dict]) -> str:
    """Format search results as readable text for the agent."""
    if not results:
        return f"No code results found for: {query}"

    lines = [f"Code search: {len(results)} results for \"{query}\"\n"]
    for i, r in enumerate(results, 1):
        filepath = r["filepath"]
        score = r["score"]
        code = r["code"]
        if len(code) > 500:
            code = code[:500] + f"\n... ({len(code)} chars total)"
        lines.append(f"[{i}] {filepath} (score: {score})")
        lines.append(code)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def _run_mcp_server(host: str = "127.0.0.1", port: int = 8889, transport: str = "stdio") -> None:
    from mcp.server import FastMCP

    mcp = FastMCP(
        "cocoindex-code",
        host=host,
        port=port,
    )

    @mcp.tool()
    def cocoindex_search(query: str, limit: int = 10) -> str:
        """Semantic code search over the kubernaut codebase.

        Searches 17,000+ embedded Go code chunks using cosine similarity.
        Use for questions like:
        - "where do we handle rate limiting?"
        - "how does the remediation pipeline work?"
        - "what functions touch RemediationRequest status?"

        Returns ranked code snippets with file paths and relevance scores.
        Prefer this over Grep when searching by concept rather than exact text.
        """
        results = search_code(query, limit=min(limit, 20))
        return _format_results(query, results)

    if transport == "stdio":
        log.info("Starting cocoindex-code MCP server (stdio)")
        mcp.run(transport="stdio")
    else:
        log.info("Starting cocoindex-code MCP server on %s:%d (sse)", host, port)
        mcp.run(transport="sse")


# ---------------------------------------------------------------------------
# CLI query mode
# ---------------------------------------------------------------------------

def _run_cli_query(query: str, limit: int = 10) -> None:
    results = search_code(query, limit=limit)
    print(_format_results(query, results))


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex code search — MCP server + CLI"
    )
    parser.add_argument("--query", "-q", help="Run a single query and exit")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--port", "-p", type=int, default=8889, help="MCP server port (default: 8889)")
    parser.add_argument("--host", default="127.0.0.1", help="MCP server bind address")
    args = parser.parse_args()

    if args.query:
        _run_cli_query(args.query, limit=args.limit)
    else:
        _run_mcp_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
