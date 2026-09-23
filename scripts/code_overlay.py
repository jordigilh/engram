#!/usr/bin/env python3
"""Plan, preflight, and build branch/worktree code-search overlays.

Examples:

    python scripts/code_overlay.py preflight \
      --worktree /path/to/kubernaut --base origin/main \
      --codanna-config /path/to/.codanna/settings.toml

    python scripts/code_overlay.py build --backend codanna \
      --worktree /path/to/kubernaut --base origin/main \
      --codanna-config /path/to/.codanna/settings.toml
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from engram.code_overlay import (  # noqa: E402
    OverlayPlanError,
    build_codanna_overlay,
    build_cocoindex_overlay,
    build_overlay_plan,
    preflight_overlay,
)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--worktree", type=pathlib.Path, required=True)
    parser.add_argument("--base", default="origin/main", help="Base ref used for the branch delta")
    parser.add_argument("--repository", help="Repository tag; defaults to the worktree directory name")
    parser.add_argument("--codanna", default="codanna")
    parser.add_argument("--codanna-config", type=pathlib.Path)
    parser.add_argument("--base-manifest", type=pathlib.Path)
    parser.add_argument("--cocoindex-pg-url", default=os.environ.get("COCOINDEX_PG_URL"))
    parser.add_argument("--output", type=pathlib.Path)


def _plan(args: argparse.Namespace):
    return build_overlay_plan(
        args.worktree,
        base_ref=args.base,
        repository=args.repository,
    )


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("plan", "preflight", "build"):
        command_parser = subparsers.add_parser(command)
        _common(command_parser)
    subparsers.choices["preflight"].add_argument(
        "--backend", choices=("codanna", "cocoindex", "both"), default="both"
    )
    subparsers.choices["build"].add_argument(
        "--backend", choices=("codanna", "cocoindex", "both"), default="both"
    )
    subparsers.choices["build"].add_argument("--timeout", type=float, default=900)
    subparsers.choices["build"].add_argument(
        "--max-files", type=int, help="Bound a spike build to the first N backend-eligible files"
    )
    subparsers.choices["build"].add_argument(
        "--codanna-no-semantic", action="store_true",
        help="Build Codanna symbols/relationships without generating semantic embeddings",
    )
    subparsers.choices["build"].add_argument(
        "--codanna-production-only", action="store_true",
        help="Exclude test/e2e/integration and generated files from the Codanna overlay",
    )

    args = parser.parse_args()
    try:
        plan = _plan(args)
        if args.command == "plan":
            _print(plan.to_dict())
            return 0

        backends = ("codanna", "cocoindex") if args.backend == "both" else (args.backend,)
        report = preflight_overlay(
            plan,
            backends=backends,
            codanna_binary=args.codanna,
            codanna_config=args.codanna_config,
            pg_url=args.cocoindex_pg_url,
            base_manifest=args.base_manifest,
        )
        if args.command == "preflight":
            payload = {"plan": plan.to_dict(), "preflight": report.to_dict()}
            _print(payload)
            return 0 if report.ok else 2

        if not report.ok:
            _print({"plan": plan.to_dict(), "preflight": report.to_dict()})
            return 2
        output = args.output or pathlib.Path.home() / ".engram" / "code-overlays" / plan.overlay_id
        results = []
        if "codanna" in backends:
            results.append(
                build_codanna_overlay(
                    plan,
                    output,
                    binary=args.codanna,
                    timeout=args.timeout,
                    max_files=args.max_files,
                    semantic_search=not args.codanna_no_semantic,
                    exclude_tests=args.codanna_production_only,
                    exclude_generated=args.codanna_production_only,
                )
            )
        if "cocoindex" in backends:
            results.append(
                build_cocoindex_overlay(
                    plan,
                    output,
                    pg_url=args.cocoindex_pg_url,
                    timeout=args.timeout,
                    max_files=args.max_files,
                )
            )
        _print({"plan": plan.to_dict(), "preflight": report.to_dict(), "builds": [item.to_dict() for item in results]})
        return 0
    except (OverlayPlanError, OSError, ValueError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
