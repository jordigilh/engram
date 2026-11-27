#!/usr/bin/env python3
"""Capture the real CocoIndex docs flow for the shadow gate (issue #214).

This is developer tooling, not a pytest module and not a service entrypoint.
It starts a loopback Hindsight capture server, writes a temporary
`projects.toml`, and runs the deployed flow exactly as production does:

    python -m engram.flows.configured --project shadow --mode backfill --apps docs

Every retain POST is captured verbatim (bank, document_id, content, metadata,
tags, timestamp). The script then annotates each capture with the source file
and chunk key from the flow's own `_relative_path` / `_document_base_id`
helpers. File facts come from the same CocoIndex `walk_dir` matcher used by
`docs_main`.

Run it with the deployed flow interpreter:

    ~/.engram/venv/bin/python \\
        rust/crates/engram-ingest/fixtures/docs_shadow_oracle.py \\
        --root /tmp/frozen/docs --out /tmp/shadow-before.json

The companion driver (`docs_shadow_run.py`) freezes the tree, mutates a second
copy, captures both snapshots, and invokes the Rust comparator.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import pathlib
import platform
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
DEFAULT_PG_DSN = "postgresql://hindsight@/hindsight?host=/tmp"
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


class CaptureServer:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        handler = self._handler()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _handler(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # noqa: D401
                pass

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return
                bank = "?"
                if "/banks/" in self.path:
                    bank = self.path.split("/banks/", 1)[1].split("/", 1)[0]
                with owner._lock:
                    for item in body.get("items", []):
                        owner.records.append({"bank": bank, "item": item})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        return Handler


def toml_string(value: str) -> str:
    return json.dumps(value)


def git_commit(repo: pathlib.Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def flow_source_hashes(repo: pathlib.Path) -> dict[str, str]:
    paths = [
        repo / "src/engram/flows/configured.py",
        repo / "src/engram/chunking.py",
        repo / "src/engram/synthesis.py",
    ]
    return {path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def flow_source_dirty(repo: pathlib.Path) -> bool:
    try:
        changed = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "status",
                "--porcelain",
                "--",
                "src/engram/flows",
                "src/engram/chunking.py",
                "src/engram/synthesis.py",
            ],
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return True
    return bool(changed.strip())


def list_tree_files(
    root: pathlib.Path,
    tag: str,
    includes: list[str],
    excludes: list[str],
) -> list[dict[str, Any]]:
    """Use CocoIndex's own walker and the flow's own path helpers."""
    import asyncio

    import engram.flows.configured as configured
    from cocoindex.connectors import localfs
    from cocoindex.resources.file import PatternFilePathMatcher

    async def collect() -> list[dict[str, Any]]:
        walker = localfs.walk_dir(
            root,
            recursive=True,
            path_matcher=PatternFilePathMatcher(included_patterns=includes, excluded_patterns=excludes),
            live=False,
        )
        result = []
        async for file in walker:
            content = await file.read_text()
            relative = configured._relative_path(file, root)
            path = file.file_path.resolve()
            result.append(
                {
                    "relative_path": relative,
                    "base_id": configured._document_base_id(tag, relative),
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "size_bytes": path.stat().st_size,
                    "timestamp": configured._file_timestamp(path),
                }
            )
        return sorted(result, key=lambda item: item["relative_path"])

    return asyncio.run(collect())


def attribute(records: list[dict[str, Any]], files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_base = sorted(files, key=lambda item: len(item["base_id"]), reverse=True)
    retains = []
    for sequence, record in enumerate(records):
        item = record["item"]
        document_id = item["document_id"]
        matches = [
            file for file in by_base if document_id == file["base_id"] or document_id.startswith(file["base_id"] + "--")
        ]
        exact = [file for file in matches if document_id == file["base_id"]]
        if len(exact) == 1:
            matches = exact
        elif len(matches) > 1:
            raise SystemExit(
                f"ambiguous document id {document_id!r} matches {[file['relative_path'] for file in matches]}"
            )
        relative = ""
        chunk_key = ""
        if matches:
            file = matches[0]
            relative = file["relative_path"]
            if document_id != file["base_id"]:
                chunk_key = document_id[len(file["base_id"]) + 2 :]
        if not relative:
            raise SystemExit(f"cannot attribute document id {document_id!r} to a tree file")
        retains.append(
            {
                "sequence": sequence,
                "bank_id": record["bank"],
                "document_id": document_id,
                "relative_path": relative,
                "chunk_key": chunk_key,
                "content": item["content"],
                "content_sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
                "metadata": item.get("metadata") or {},
                "tags": item.get("tags") or [],
                "timestamp": item.get("timestamp"),
            }
        )
    return retains


def capture(
    *,
    root: pathlib.Path,
    tag: str,
    bank: str,
    includes: list[str],
    excludes: list[str],
    out: pathlib.Path,
    pg_dsn: str,
    repo: pathlib.Path,
    work_dir: pathlib.Path,
) -> dict[str, Any]:
    import cocoindex
    import networkx
    import sklearn

    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"root is not a directory: {root}")

    server = CaptureServer()
    server.start()
    try:
        config = work_dir / "projects.toml"
        config.write_text(
            "\n".join(
                [
                    "[defaults]",
                    f"hindsight_url = {toml_string(server.url)}",
                    f"pg_dsn = {toml_string(pg_dsn)}",
                    "pg_pool_min_size = 1",
                    "pg_pool_max_size = 2",
                    "",
                    "[projects.shadow]",
                    f"docs_bank = {toml_string(bank)}",
                    "docs_live = false",
                    "",
                    "[projects.shadow.paths]",
                    f"cocoindex_db = {toml_string(str(work_dir / 'cocoindex.db'))}",
                    "",
                    "[[projects.shadow.sources]]",
                    f"tag = {toml_string(tag)}",
                    f"root = {toml_string(str(root))}",
                    f"docs_include = {json.dumps(includes)}",
                    f"docs_exclude = {json.dumps(excludes)}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo / "src")
        env["ENGRAM_PROJECTS_CONFIG"] = str(config)
        env["HINDSIGHT_URL"] = server.url
        env["COCOINDEX_PG_URL"] = pg_dsn
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "engram.flows.configured",
                "--project",
                "shadow",
                "--config",
                str(config),
                "--mode",
                "backfill",
                "--apps",
                "docs",
            ],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=1800,
        )
        if completed.returncode:
            raise SystemExit(
                "configured docs flow failed:"
                f"\nstdout:\n{completed.stdout[-4000:]}"
                f"\nstderr:\n{completed.stderr[-4000:]}"
            )
    finally:
        server.stop()

    files = list_tree_files(root, tag, includes, excludes)
    retains = attribute(server.records, files)
    if not retains:
        raise SystemExit(f"the real flow emitted no retains for {root}")

    tree_hash = hashlib.sha256()
    for file in files:
        tree_hash.update(f"{file['relative_path']}\0{file['content_sha256']}\n".encode("utf-8"))

    dump = {
        "provenance": {
            "python": platform.python_version(),
            "cocoindex": cocoindex.__version__,
            "sklearn": sklearn.__version__,
            "networkx": networkx.__version__,
            "flow_source_commit": git_commit(repo),
            "flow_source_dirty": flow_source_dirty(repo),
            "flow_source_sha256": flow_source_hashes(repo),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tree": {
                "root": str(root),
                "source_tag": tag,
                "bank_id": bank,
                "docs_include": includes,
                "docs_exclude": excludes,
                "files": len(files),
                "bytes": sum(file["size_bytes"] for file in files),
                "sha256": tree_hash.hexdigest(),
            },
        },
        "files": files,
        "retains": retains,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(dump, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"oracle: {len(retains)} retains over {len(files)} files -> {out}",
        file=sys.stderr,
    )
    return dump


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--tag", default="engram-docs")
    parser.add_argument("--bank", default="engram-docs")
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("ENGRAM_SHADOW_PG_DSN", DEFAULT_PG_DSN),
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT)
    parser.add_argument("--work-dir", type=pathlib.Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    includes = args.include or ["**/*.md"]
    temporary = None
    if args.work_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="engram-shadow-oracle-")
        work_dir = pathlib.Path(temporary.name)
    else:
        work_dir = args.work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
    try:
        capture(
            root=args.root,
            tag=args.tag,
            bank=args.bank,
            includes=includes,
            excludes=args.exclude,
            out=args.out,
            pg_dsn=args.pg_dsn,
            repo=args.repo_root.resolve(),
            work_dir=work_dir,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    main()
