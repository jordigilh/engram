#!/usr/bin/env python3
r"""Engram code search MCP server.

Provides hybrid code search (dense vectors + BM25) over the
cocoindex.engram_code_embeddings table (this repo's own Python source).
Results are fused using Reciprocal Rank Fusion (RRF) so both semantic
similarity and exact keyword matches contribute to ranking.

Usage:
    python3 engram-cocoindex-search.py                    # Start MCP server (stdio)
    python3 engram-cocoindex-search.py --query "how does contradiction resolution work"
    python3 engram-cocoindex-search.py --query "resolve_contradiction" --mode dense
    python3 engram-cocoindex-search.py --query "resolve_contradiction" --mode bm25
    python3 engram-cocoindex-search.py --pattern 'def \NAME(\(A*\)):' --language python
"""

import argparse
import logging
import pathlib
import re
import sys
from typing import Any

# This file is part of the engram.search package (src/engram/search/).
# sys.path[0] for a script invoked via a symlink (as launchd does) resolves
# to the symlink's realpath target directory (src/engram/search/), not the
# symlink's own directory -- src/ itself must still be added explicitly so
# `engram` resolves as a top-level package rather than needing this file to
# be run via `-m`/an installed console script (not yet true in this repo).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))
from engram import callgraph, chunking  # noqa: E402
from engram.project_config import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_PG_DSN,
    default_project_path,
    load_project_settings,
    sql_identifier,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("engram-cocoindex-search")

PROJECT_SETTINGS = load_project_settings("engram")
PG_URL = PROJECT_SETTINGS.text("pg_dsn", DEFAULT_PG_DSN)
assert PG_URL is not None
CODE_TABLE = sql_identifier(
    PROJECT_SETTINGS.text("code_table", "engram_code_embeddings") or "engram_code_embeddings",
    "project 'engram' code_table",
)
SEARCH_PROJECT = "engram"
EMBEDDING_MODEL = PROJECT_SETTINGS.text("embedding_model", DEFAULT_EMBEDDING_MODEL)
assert EMBEDDING_MODEL is not None
RRF_K = 60  # RRF constant — standard value from the original paper

# Same env var (and default) as engram-cocoindex-flows.py, so pattern search
# walks the exact same checkout the ingestion flow indexes.
ENGRAM_REPO_DIR = PROJECT_SETTINGS.path("repo_dir", str(default_project_path("engram")))
assert ENGRAM_REPO_DIR is not None
CALL_GRAPH_ROOT = ENGRAM_REPO_DIR
CALL_GRAPH_LANGUAGE = "python"
_DEFAULT_BRANCH: str | None = None

_EXCLUDED_PY_PATTERNS = [
    "**/__pycache__/**", "**/.pytest_cache/**", "**/.git/**",
    "**/venv/**", "**/.venv/**", "**/node_modules/**",
]

# (repo_tag, root, included_patterns, excluded_patterns) -- mirrors
# engram-cocoindex-flows.py's localfs.walk_dir(path_matcher=
# PatternFilePathMatcher(...)) call exactly.
_PATTERN_SEARCH_ROOTS = [
    ("engram", ENGRAM_REPO_DIR, ["**/*.py"], _EXCLUDED_PY_PATTERNS),
]
_CALL_GRAPH_ROOTS: list[tuple] = list(_PATTERN_SEARCH_ROOTS)

_model = None


def configure_project(
    *,
    project: str,
    pg_url: str,
    code_table: str,
    embedding_model: str,
    pattern_roots: list[tuple],
    call_graph_root: pathlib.Path | None = None,
    call_graph_language: str = "python",
    call_graph_roots: list[tuple] | None = None,
    default_branch: str | None = None,
) -> None:
    """Configure this reusable search engine for a deployment project.

    The historical ``engram`` entrypoint keeps its original defaults.  The
    generic configured entrypoint calls this once at startup with values from
    ``projects.toml``; keeping the algorithm here avoids one source module per
    project.
    """
    global CODE_TABLE, EMBEDDING_MODEL, PG_URL, SEARCH_PROJECT
    global CALL_GRAPH_ROOT, CALL_GRAPH_LANGUAGE, _PATTERN_SEARCH_ROOTS, _CALL_GRAPH_ROOTS, _DEFAULT_BRANCH, _model
    sql_identifier(code_table, "CocoIndex table name")
    CODE_TABLE = code_table
    EMBEDDING_MODEL = embedding_model
    PG_URL = pg_url
    SEARCH_PROJECT = project
    _PATTERN_SEARCH_ROOTS = pattern_roots
    CALL_GRAPH_ROOT = call_graph_root or (pattern_roots[0][1] if pattern_roots else ENGRAM_REPO_DIR)
    CALL_GRAPH_LANGUAGE = call_graph_language
    # ``[]`` is meaningful: a project may have code-search roots whose
    # language is not supported by the call-graph extractor.  Only fall back
    # to all pattern roots for callers that omit the argument entirely.
    _CALL_GRAPH_ROOTS = list(pattern_roots) if call_graph_roots is None else list(call_graph_roots)
    _DEFAULT_BRANCH = default_branch
    _model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL)
        log.info("Loaded embedding model: %s", EMBEDDING_MODEL)
    return _model


def _embed_query(query: str) -> list[float]:
    model = _get_model()
    return model.encode(query, normalize_embeddings=True).tolist()


def _rrf_fuse(
    dense_results: list[dict],
    bm25_results: list[dict],
    limit: int,
) -> list[dict]:
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for rank, r in enumerate(dense_results):
        key = r["id"]
        scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank + 1)
        if key not in items:
            items[key] = r

    for rank, r in enumerate(bm25_results):
        key = r["id"]
        scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank + 1)
        if key not in items:
            items[key] = r

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [{**items[key], "rrf_score": round(score, 6)} for key, score in ranked]


def _root_details(root: tuple) -> tuple[str, pathlib.Path, list[str], list[str], str, str | None]:
    """Normalize configured and historical four-item search-root tuples."""
    if len(root) < 4:
        raise ValueError("search roots must contain tag, path, include, and exclude values")
    tag, path, included, excluded = root[:4]
    language = root[4] if len(root) > 4 and root[4] else "auto"
    branch = root[5] if len(root) > 5 else None
    return tag, pathlib.Path(path), list(included), list(excluded), str(language), branch


def _scope_token(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]*", value):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _normalized_branch(branch: str | None) -> str | None:
    effective = _DEFAULT_BRANCH if branch is None else branch
    if effective is None:
        return None
    effective = effective.strip()
    if not effective or effective in {"main", "default"}:
        return "main"
    if effective.startswith("release/"):
        return effective.removeprefix("release/")
    if effective.startswith("release-"):
        return effective.removeprefix("release-")
    return effective


def _branch_tag(tag: str, branch: str) -> str:
    base = tag.split("@", 1)[0]
    if branch == "main":
        return base
    if re.fullmatch(r"v\d+\.\d+", branch):
        return f"{base}@release-{branch}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "unknown"
    return f"{base}@branch-{safe}"


def _root_matches(tag: str, repo: str | None, branch: str | None) -> bool:
    if repo is not None:
        repo = _scope_token(repo, "repo")
        if "@" in repo:
            if tag != repo:
                return False
        elif tag.split("@", 1)[0] != repo:
            return False
    if branch is None:
        return True
    if repo is not None and "@" in repo:
        return tag == repo
    return tag == _branch_tag(tag, branch)


def _select_roots(roots: list[tuple], repo: str | None, branch: str | None) -> list[tuple]:
    effective_branch = _normalized_branch(branch)
    return [
        root for root in roots
        if _root_matches(_root_details(root)[0], repo, effective_branch)
    ]


def _select_pattern_roots(repo: str | None, branch: str | None) -> list[tuple]:
    """Select configured filesystem roots for structural and graph queries."""
    return _select_roots(_PATTERN_SEARCH_ROOTS, repo=repo, branch=branch)


def _branch_where(repo: str | None, branch: str | None) -> tuple[str, list[str]]:
    """Build parameterized filepath scope for indexed branch/source tags."""
    effective_branch = _normalized_branch(branch)
    clauses: list[str] = []
    params: list[str] = []
    if repo is not None:
        repo = _scope_token(repo, "repo")
        if "@" in repo:
            clauses.append("filepath LIKE %s")
            params.append(f"{repo}/%")
            return " AND " + " AND ".join(clauses), params
    if effective_branch is None:
        if repo:
            clauses.append("filepath LIKE %s")
            params.append(f"{repo}/%")
    elif effective_branch == "main":
        clauses.append("filepath NOT LIKE %s")
        params.append("%@%")
        if repo:
            clauses.append("filepath LIKE %s")
            params.append(f"{repo}/%")
    else:
        suffix = _branch_tag("source", effective_branch).removeprefix("source")
        clauses.append("filepath LIKE %s")
        params.append(f"{repo or '%'}{suffix}/%")
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def search_code(
    query: str,
    limit: int = 10,
    mode: str = "hybrid",
    repo: str | None = None,
    branch: str | None = None,
    table: str | None = None,
) -> list[dict[str, Any]]:
    import psycopg2

    if mode not in {"hybrid", "dense", "bm25"}:
        raise ValueError(f"unsupported search mode: {mode!r}")
    limit = max(1, int(limit))
    candidate_pool = limit * 3
    table_name = sql_identifier(table or CODE_TABLE, "CocoIndex table name")
    branch_where, branch_params = _branch_where(repo, branch)

    conn = psycopg2.connect(PG_URL)
    try:
        with conn.cursor() as cur:
            dense_results = []
            bm25_results = []

            if mode in ("hybrid", "dense"):
                embedding = _embed_query(query)
                embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
                cur.execute(
                    f"""
                    SELECT id, filepath, chunk_index, code,
                           1 - (embedding <=> %s::vector) AS score
                    FROM cocoindex.{table_name}
                    WHERE TRUE{branch_where}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (embedding_str, *branch_params, embedding_str, candidate_pool),
                )
                dense_results = [
                    {"id": r[0], "filepath": r[1], "chunk_index": r[2],
                     "code": r[3], "dense_score": round(float(r[4]), 4)}
                    for r in cur.fetchall()
                ]

            if mode in ("hybrid", "bm25"):
                # `to_tsquery` treats punctuation such as `/`, `:`, `&`, and
                # parentheses as query-language operators. Build prefix terms
                # only from lexical tokens so ordinary user text like
                # "storage/index error" cannot turn into a tsquery syntax error.
                tsquery = " & ".join(f"{token}:*" for token in re.findall(r"\w+", query))
                if tsquery:
                    cur.execute(
                        f"""
                        SELECT id, filepath, chunk_index, code,
                               ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS score
                        FROM cocoindex.{table_name}
                        WHERE search_vector @@ to_tsquery('simple', %s){branch_where}
                        ORDER BY score DESC
                        LIMIT %s
                        """,
                        (tsquery, tsquery, *branch_params, candidate_pool),
                    )
                    bm25_results = [
                        {"id": r[0], "filepath": r[1], "chunk_index": r[2],
                         "code": r[3], "bm25_score": round(float(r[4]), 4)}
                        for r in cur.fetchall()
                    ]
    finally:
        conn.close()

    if mode == "dense":
        return [
            {**r, "score": r["dense_score"]} for r in dense_results[:limit]
        ]
    if mode == "bm25":
        return [
            {**r, "score": r["bm25_score"]} for r in bm25_results[:limit]
        ]

    fused = _rrf_fuse(dense_results, bm25_results, limit)
    return [{**r, "score": r["rrf_score"]} for r in fused]


def _format_results(query: str, results: list[dict], mode: str = "hybrid") -> str:
    if not results:
        return f"No code results found for: {query}"

    label = {"hybrid": "hybrid (dense+BM25)", "dense": "dense only", "bm25": "BM25 only"}
    lines = [f"Code search [{label.get(mode, mode)}]: {len(results)} results for \"{query}\"\n"]
    for i, r in enumerate(results, 1):
        filepath = r["filepath"]
        score = r["score"]
        sources = []
        if r.get("dense_score") is not None:
            sources.append(f"dense:{r['dense_score']}")
        if r.get("bm25_score") is not None:
            sources.append(f"bm25:{r['bm25_score']}")
        source_info = f" [{', '.join(sources)}]" if sources else ""
        code = r["code"]
        if len(code) > 500:
            code = code[:500] + f"\n... ({len(code)} chars total)"
        lines.append(f"[{i}] {filepath} (score: {score}{source_info})")
        lines.append(code)
        lines.append("")
    return "\n".join(lines)


def pattern_search_code(
    pattern: str,
    language: str,
    limit: int = 10,
    repo: str | None = None,
    branch: str | None = None,
) -> list[dict[str, Any]]:
    """Structural ("by-example") code search via CocoIndex's CodePattern --
    tree-sitter AST matching against this repo's own live checkout.

    Unlike search_code() above, there is no structural-pattern index to
    query: CodePattern.match_file() parses source directly, so this walks
    the same file set engram-cocoindex-flows.py already ingests (see
    _PATTERN_SEARCH_ROOTS / chunking.find_code_files()) for every call.
    Complements, not replaces, search_code() (semantic/BM25 "what does X
    do"): this is purely syntactic "find code shaped like X", with no type
    resolution and no cross-file symbol graph (see docs/FINDINGS.md
    2026-08-07).
    """
    from cocoindex.ops.code import CodePattern, render_match
    from cocoindex.ops.text import detect_code_language

    cp = CodePattern(pattern, language)
    results: list[dict[str, Any]] = []
    for root_spec in _select_pattern_roots(repo=repo, branch=branch):
        repo_tag, root, included, excluded, root_language, _root_branch = _root_details(root_spec)
        if root_language not in {"auto", language}:
            continue
        if len(results) >= limit:
            break
        for path in chunking.find_code_files(root, included, excluded):
            if len(results) >= limit:
                break
            if detect_code_language(filename=path.name) != language:
                continue
            file_match = cp.match_file(str(path))
            if file_match is None:
                continue
            rel_path = path.relative_to(root)
            for match in file_match.matches:
                if len(results) >= limit:
                    break
                view = render_match(file_match.source, match)
                results.append({
                    "repo": repo_tag,
                    "filepath": f"{repo_tag}/{rel_path}",
                    "line": match.chunks[0].start.line,
                    "text": view.text,
                })
    return results


def _format_pattern_results(pattern: str, language: str, results: list[dict]) -> str:
    """Format structural pattern matches as readable text for the agent."""
    if not results:
        return f'No structural matches for language={language} pattern: {pattern}'

    lines = [f"Structural pattern search [{language}]: {len(results)} matches for: {pattern}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['filepath']}:{r['line']}")
        lines.append(r["text"])
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Call-graph queries (spike -- see docs/CALL_GRAPH_CLUSTERING.md, issue #43)
# ---------------------------------------------------------------------------
#
# Thin wrappers around callgraph.py's generic multi-org query/format layer
# (Phase 0 of the rollout -- see docs/CALL_GRAPH_CLUSTERING.md): this module
# only supplies *which* root/language to build from. No behavior change from
# when this logic lived here directly.

def _build_graph_with_timing(
    repo: str | None = None,
    branch: str | None = None,
    language: str | None = None,
):
    requested_language = language or CALL_GRAPH_LANGUAGE
    roots = _select_roots(_CALL_GRAPH_ROOTS, repo=repo, branch=branch)
    if requested_language != "auto":
        roots = [root for root in roots if _root_details(root)[4] in {"auto", requested_language}]

    # The call-graph builder resolves one grammar per graph.  A configured
    # project may legitimately mix Go, Rust, Python, and TypeScript roots, so
    # build one graph per language and merge the independent results rather
    # than silently dropping every non-Python root.
    groups: dict[str, list[tuple]] = {}
    for root in roots:
        root_language = _root_details(root)[4]
        graph_language = requested_language if requested_language != "auto" else root_language
        if graph_language == "auto":
            graph_language = "python"
        groups.setdefault(graph_language, []).append(root)

    if not groups:
        fallback_language = requested_language if requested_language != "auto" else "python"
        return callgraph.build_multi_repo_call_graph_with_stats(
            [], language=fallback_language, logger=log,
        )

    graphs = []
    for graph_language, language_roots in groups.items():
        normalized = [
            (
                _root_details(root)[0],
                _root_details(root)[1],
                _root_details(root)[2],
                _root_details(root)[3],
            )
            for root in language_roots
        ]
        # Use the multi-repository wrapper even for one root so the source
        # tag remains part of every qualified function name.  Without that
        # prefix a single-root project and a release/repository-scoped query
        # would disagree about the node's identity.
        graphs.append(
            callgraph.build_multi_repo_call_graph_with_stats(
                normalized,
                language=graph_language,
                logger=log,
            )
        )

    if len(graphs) == 1:
        return graphs[0]
    merged = callgraph.nx.DiGraph()
    merged.graph["unresolved_calls"] = 0
    merged.graph["total_calls"] = 0
    merged.graph["ambiguous_calls"] = []
    for graph in graphs:
        merged.add_nodes_from(graph.nodes(data=True))
        merged.add_edges_from(graph.edges(data=True))
        merged.graph["unresolved_calls"] += graph.graph.get("unresolved_calls", 0)
        merged.graph["total_calls"] += graph.graph.get("total_calls", 0)
        merged.graph["ambiguous_calls"].extend(graph.graph.get("ambiguous_calls", []))
    return merged


def call_graph_blast_radius(
    function: str,
    depth: int = 2,
    repo: str | None = None,
    branch: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Who (transitively) calls `function`, up to `depth` hops -- "what
    breaks if I change this." See docs/CALL_GRAPH_CLUSTERING.md for the
    accuracy ceiling (name-based resolution, no type info)."""
    graph = _build_graph_with_timing(repo=repo, branch=branch, language=language)
    return callgraph.query_blast_radius(graph, function, depth=depth)


def call_graph_shortest_path(
    source: str,
    target: str,
    repo: str | None = None,
    branch: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Does `source` ever reach `target` through a chain of calls, and how."""
    graph = _build_graph_with_timing(repo=repo, branch=branch, language=language)
    return callgraph.query_shortest_path(graph, source, target)


def call_graph_get_cluster(
    function: str,
    repo: str | None = None,
    branch: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Which Leiden community `function` belongs to, and its other members.

    Clustering quality depends entirely on the underlying graph's structure:
    a small, centralized codebase may legitimately produce one dominant
    cluster or many singletons -- that reflects the codebase, not a broken
    clustering step (see docs/CALL_GRAPH_CLUSTERING.md)."""
    graph = _build_graph_with_timing(repo=repo, branch=branch, language=language)
    return callgraph.query_get_cluster(graph, function)


def _format_blast_radius_result(result: dict) -> str:
    return callgraph.format_blast_radius_result(result)


def _format_shortest_path_result(result: dict) -> str:
    return callgraph.format_shortest_path_result(result)


def _format_cluster_result(result: dict) -> str:
    return callgraph.format_cluster_result(result)


def _format_lookup_error(result: dict) -> str:
    return callgraph.format_lookup_error(result)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def _run_mcp_server(
    host: str = "127.0.0.1",
    port: int = 8890,
    transport: str = "stdio",
    project: str | None = None,
) -> None:
    # mcp==2.0.0 (2026-08-22 dependabot bump) renamed FastMCP to MCPServer
    # and moved host/port from the constructor to run(). See
    # docs/findings/2026-08.md's 2026-08-27 entry.
    from engram import mcp_compat  # 1.x/2.x compat (mcp<2.0 pinned)

    project_name = project or SEARCH_PROJECT
    tool_prefix = f"{project_name}_"
    mcp = mcp_compat.make_server(f"{project_name}-code", host=host, port=port)

    @mcp.tool(name=f"{tool_prefix}code_search")
    def engram_code_search(
        query: str,
        limit: int = 10,
        repo: str | None = None,
        branch: str | None = None,
    ) -> str:
        """Hybrid code search over the Engram tooling codebase.

        Combines dense vector similarity and BM25 keyword matching via
        Reciprocal Rank Fusion for best results.  Works equally well for:
        - conceptual queries: "how does contradiction resolution work?"
        - exact identifiers: "resolve_contradiction"

        Returns ranked code snippets with file paths and relevance scores.
        Prefer this over Grep when searching by concept rather than exact text.
        """
        results = search_code(query, limit=min(limit, 20), repo=repo, branch=branch)
        return _format_results(query, results)

    @mcp.tool(name=f"{tool_prefix}code_pattern_search")
    def engram_code_pattern_search(
        pattern: str,
        language: str | None = None,
        limit: int = 10,
        repo: str | None = None,
        branch: str | None = None,
    ) -> str:
        r"""Structural ("by-example") code search over the Engram tooling codebase.

        For "find code shaped like X" -- e.g. every function matching a
        signature -- not "find code about X" (use engram_code_search for
        that). Matches by tree-sitter AST shape, not text/regex.

        Pattern syntax: write an example of the shape you want, using `\`
        + a name for a metavariable (matches one node) or `\(NAME*\)`
        (matches zero or more, e.g. an argument list). Omit a body entirely
        to mean "don't care what's inside" -- e.g. `def \NAME(\(A*\)):`
        matches any Python function/method regardless of body or args.

        This is purely syntactic: it does NOT resolve types and can't find
        references/callers or diagnostics.
        """
        effective_language = language or (CALL_GRAPH_LANGUAGE if CALL_GRAPH_LANGUAGE != "auto" else "python")
        results = pattern_search_code(
            pattern, effective_language, limit=min(limit, 20), repo=repo, branch=branch,
        )
        return _format_pattern_results(pattern, effective_language, results)

    @mcp.tool(name=f"{tool_prefix}call_graph_blast_radius")
    def engram_call_graph_blast_radius(
        function: str,
        depth: int = 2,
        repo: str | None = None,
        branch: str | None = None,
        language: str | None = None,
    ) -> str:
        """What (transitively) calls `function` in the Engram tooling codebase,
        up to `depth` hops -- "what breaks if I change this."

        `function` may be a bare name ("pattern_search_code") if unambiguous,
        or a qualified name ("search/engram.py::pattern_search_code").

        SPIKE, engram-only (docs/CALL_GRAPH_CLUSTERING.md, issue #43): call
        resolution is purely name-based (no type info), so common method
        names shared across unrelated functions can produce false-positive
        edges, and dynamic dispatch/external calls can't be seen at all.
        Rebuilds the call graph fresh on every call (no persisted index).
        """
        result = call_graph_blast_radius(
            function, depth=depth, repo=repo, branch=branch, language=language,
        )
        return _format_blast_radius_result(result)

    @mcp.tool(name=f"{tool_prefix}call_graph_shortest_path")
    def engram_call_graph_shortest_path(
        source: str,
        target: str,
        repo: str | None = None,
        branch: str | None = None,
        language: str | None = None,
    ) -> str:
        """Does `source` ever reach `target` through a chain of calls in the
        Engram tooling codebase, and how.

        Same name-based-resolution caveat as engram_call_graph_blast_radius
        applies (see its docstring) -- this is a SPIKE, engram-only.
        """
        result = call_graph_shortest_path(
            source, target, repo=repo, branch=branch, language=language,
        )
        return _format_shortest_path_result(result)

    @mcp.tool(name=f"{tool_prefix}call_graph_get_cluster")
    def engram_call_graph_get_cluster(
        function: str,
        repo: str | None = None,
        branch: str | None = None,
        language: str | None = None,
    ) -> str:
        """Which cluster of related functions (via Leiden community detection
        over the call graph) `function` belongs to in the Engram tooling
        codebase, and its other members.

        Same name-based-resolution caveat as engram_call_graph_blast_radius
        applies (see its docstring) -- this is a SPIKE, engram-only. A small,
        centralized codebase may legitimately produce one dominant cluster or
        many singletons; that reflects the codebase, not a broken tool.
        """
        result = call_graph_get_cluster(
            function, repo=repo, branch=branch, language=language,
        )
        return _format_cluster_result(result)

    if transport == "stdio":
        log.info("Starting engram-code MCP server (stdio)")
        mcp_compat.run_server(mcp, transport="stdio")
    else:
        log.info("Starting engram-code MCP server on %s:%d (%s)", host, port, transport)
        mcp_compat.run_server(mcp, transport=transport, host=host, port=port)


# ---------------------------------------------------------------------------
# CLI query mode
# ---------------------------------------------------------------------------

def _run_cli_query(
    query: str,
    limit: int = 10,
    mode: str = "hybrid",
    repo: str | None = None,
    branch: str | None = None,
) -> None:
    results = search_code(query, limit=limit, mode=mode, repo=repo, branch=branch)
    print(_format_results(query, results, mode=mode))


def _run_cli_pattern_query(
    pattern: str,
    language: str,
    limit: int = 10,
    repo: str | None = None,
    branch: str | None = None,
) -> None:
    results = pattern_search_code(pattern, language, limit=limit, repo=repo, branch=branch)
    print(_format_pattern_results(pattern, language, results))


def _run_cli_blast_radius(
    function: str,
    depth: int,
    repo: str | None = None,
    branch: str | None = None,
    language: str | None = None,
) -> None:
    result = call_graph_blast_radius(function, depth=depth, repo=repo, branch=branch, language=language)
    print(_format_blast_radius_result(result))


def _run_cli_shortest_path(
    source: str,
    target: str,
    repo: str | None = None,
    branch: str | None = None,
    language: str | None = None,
) -> None:
    result = call_graph_shortest_path(source, target, repo=repo, branch=branch, language=language)
    print(_format_shortest_path_result(result))


def _run_cli_cluster(
    function: str,
    repo: str | None = None,
    branch: str | None = None,
    language: str | None = None,
) -> None:
    result = call_graph_get_cluster(function, repo=repo, branch=branch, language=language)
    print(_format_cluster_result(result))


def main():
    parser = argparse.ArgumentParser(
        description="Engram code search — MCP server + CLI"
    )
    parser.add_argument("--query", "-q", help="Run a single query and exit")
    parser.add_argument("--pattern", help="Run a single structural pattern query and exit")
    parser.add_argument("--language", default="python", help="Language for --pattern (default: python)")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--mode", "-m", default="hybrid", choices=["hybrid", "dense", "bm25"],
                        help="Search mode (default: hybrid)")
    parser.add_argument("--repo", help="Scope results to one configured source tag")
    parser.add_argument("--branch", help="Scope results to main or a configured release/branch tag")
    parser.add_argument("--blast-radius", help="Call-graph spike: who (transitively) calls this function")
    parser.add_argument("--depth", type=int, default=2, help="Depth for --blast-radius (default: 2)")
    parser.add_argument("--shortest-path", nargs=2, metavar=("SOURCE", "TARGET"),
                        help="Call-graph spike: shortest call chain SOURCE -> TARGET")
    parser.add_argument("--cluster", help="Call-graph spike: which Leiden cluster this function belongs to")
    parser.add_argument("--port", "-p", type=int, default=8890, help="MCP server port (default: 8890)")
    parser.add_argument("--host", default="127.0.0.1", help="MCP server bind address")
    args = parser.parse_args()

    if args.pattern:
        _run_cli_pattern_query(args.pattern, args.language, limit=args.limit, repo=args.repo, branch=args.branch)
    elif args.blast_radius:
        _run_cli_blast_radius(args.blast_radius, depth=args.depth, repo=args.repo, branch=args.branch, language=args.language)
    elif args.shortest_path:
        _run_cli_shortest_path(*args.shortest_path, repo=args.repo, branch=args.branch, language=args.language)
    elif args.cluster:
        _run_cli_cluster(args.cluster, repo=args.repo, branch=args.branch, language=args.language)
    elif args.query:
        _run_cli_query(args.query, limit=args.limit, mode=args.mode, repo=args.repo, branch=args.branch)
    else:
        _run_mcp_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
