"""Generic branch/worktree overlay planning for code-search backends.

This module intentionally stops at a backend-neutral manifest. Codanna and
CocoIndex can materialize the same plan into different index formats without
reimplementing Git/worktree handling or freshness metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal


ChangeStatus = Literal["added", "modified", "deleted", "renamed", "untracked"]
COCOINDEX_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
    ".json", ".kt", ".md", ".mod", ".php", ".proto", ".py", ".rego", ".rs", ".sh",
    ".sql", ".sum", ".swift", ".toml", ".ts", ".tsx", ".tpl", ".yaml", ".yml",
})
CODANNA_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
    ".kt", ".php", ".py", ".rb", ".rs", ".swift", ".ts", ".tsx",
})


class OverlayPlanError(RuntimeError):
    """Raised when the current worktree cannot produce a safe overlay plan."""


@dataclass(frozen=True)
class OverlayFile:
    path: str
    status: ChangeStatus
    sha256: str | None = None
    old_path: str | None = None


@dataclass(frozen=True)
class OverlayPlan:
    repository: str
    worktree: str
    branch: str
    head_commit: str
    base_ref: str
    base_commit: str
    files: tuple[OverlayFile, ...]
    includes_uncommitted: bool
    working_tree_digest: str
    overlay_id: str
    generated_at: str

    @property
    def indexed_files(self) -> tuple[OverlayFile, ...]:
        return tuple(item for item in self.files if item.status != "deleted")

    @property
    def cocoindex_files(self) -> tuple[OverlayFile, ...]:
        return tuple(item for item in self.indexed_files if _is_indexable_path(item.path, COCOINDEX_SUFFIXES))

    @property
    def codanna_files(self) -> tuple[OverlayFile, ...]:
        return tuple(item for item in self.indexed_files if _is_indexable_path(item.path, CODANNA_SUFFIXES))

    @property
    def tombstones(self) -> tuple[OverlayFile, ...]:
        return tuple(item for item in self.files if item.status == "deleted")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update({
            "indexed_files": [asdict(item) for item in self.indexed_files],
            "tombstones": [asdict(item) for item in self.tombstones],
            "includes_uncommitted": self.includes_uncommitted,
        })
        return payload


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OverlayBuildResult:
    backend: str
    overlay_id: str
    output_dir: str
    indexed_files: int
    tombstones: int
    elapsed_seconds: float
    command: tuple[str, ...]
    table_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_git(worktree: pathlib.Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if check and completed.returncode != 0:
        raise OverlayPlanError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_indexable_path(path: str, suffixes: frozenset[str]) -> bool:
    name = pathlib.PurePosixPath(path).name
    return pathlib.PurePosixPath(path).suffix.lower() in suffixes or name in {
        "Dockerfile", "Makefile", "Containerfile",
    }


def _parse_name_status_z(output: str) -> list[tuple[str, str, str | None]]:
    fields = output.split("\0")
    changes: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        if index >= len(fields):
            break
        path = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            if index >= len(fields):
                break
            new_path = fields[index]
            index += 1
            changes.append((status[0], new_path, path))
        else:
            changes.append((status[0], path, None))
    return changes


def _status_to_change(status: str, path: pathlib.Path) -> ChangeStatus:
    if status == "A":
        return "added"
    if status == "D" or not path.exists():
        return "deleted"
    return "modified"


def _collect_changes(worktree: pathlib.Path, base_commit: str) -> tuple[dict[str, OverlayFile], bool]:
    changes: dict[str, OverlayFile] = {}
    includes_uncommitted = False

    def record(status: str, path_text: str, old_path: str | None = None) -> None:
        path = pathlib.PurePosixPath(path_text)
        if path.is_absolute() or ".." in path.parts:
            raise OverlayPlanError(f"Git reported an unsafe path: {path_text!r}")
        absolute = worktree / pathlib.Path(*path.parts)
        final_status = "renamed" if old_path else _status_to_change(status, absolute)
        changes[path.as_posix()] = OverlayFile(
            path=path.as_posix(),
            status=final_status,
            sha256=_sha256(absolute) if final_status != "deleted" and absolute.is_file() else None,
            old_path=old_path,
        )
        if old_path:
            old = pathlib.PurePosixPath(old_path)
            changes[old.as_posix()] = OverlayFile(path=old.as_posix(), status="deleted", old_path=old_path)

    for index, args in enumerate((
        ("diff", "--name-status", "--find-renames", "-z", f"{base_commit}...HEAD"),
        ("diff", "--name-status", "--find-renames", "-z"),
        ("diff", "--cached", "--name-status", "--find-renames", "-z"),
    )):
        output = _run_git(worktree, *args)
        if index and output:
            includes_uncommitted = True
        for status, path, old_path in _parse_name_status_z(output):
            record(status, path, old_path)

    untracked = _run_git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked:
        includes_uncommitted = True
    for path in filter(None, untracked.split("\0")):
        record("?", path)
        item = changes[path]
        changes[path] = OverlayFile(path=item.path, status="untracked", sha256=item.sha256)
    return changes, includes_uncommitted


def build_overlay_plan(
    worktree: str | pathlib.Path,
    *,
    base_ref: str = "origin/main",
    repository: str | None = None,
) -> OverlayPlan:
    root = pathlib.Path(worktree).expanduser().resolve()
    if not root.is_dir():
        raise OverlayPlanError(f"Worktree does not exist: {root}")
    git_root = pathlib.Path(_run_git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if git_root != root:
        raise OverlayPlanError(f"Worktree must be the Git root, got {root}; Git root is {git_root}")

    head = _run_git(root, "rev-parse", "HEAD").strip()
    branch = _run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    if not branch:
        branch = f"detached:{head[:12]}"
    base_commit = _run_git(root, "merge-base", base_ref, "HEAD").strip()
    changes, includes_uncommitted = _collect_changes(root, base_commit)
    files = tuple(changes[path] for path in sorted(changes))
    digest_input = [asdict(item) for item in files]
    working_tree_digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    repo = repository or root.name
    overlay_id = f"{repo}-{working_tree_digest[:16]}"
    generated_at = datetime.now(timezone.utc).isoformat()
    return OverlayPlan(
        repository=repo,
        worktree=str(root),
        branch=branch,
        head_commit=head,
        base_ref=base_ref,
        base_commit=base_commit,
        files=files,
        includes_uncommitted=includes_uncommitted,
        working_tree_digest=working_tree_digest,
        overlay_id=overlay_id,
        generated_at=generated_at,
    )


def _command_available(command: str) -> bool:
    return pathlib.Path(command).is_file() or shutil.which(command) is not None


def _codanna_index_path(config: pathlib.Path) -> pathlib.Path | None:
    try:
        settings = tomllib.loads(config.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    index_path = settings.get("index_path")
    if not isinstance(index_path, str):
        return None
    path = pathlib.Path(index_path).expanduser()
    if path.is_absolute():
        return path
    workspace_root = settings.get("workspace_root")
    root = pathlib.Path(workspace_root).expanduser() if isinstance(workspace_root, str) else config.parent
    return (root / path).resolve()


def _read_base_manifest(path: str | pathlib.Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        payload = json.loads(pathlib.Path(path).expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def preflight_overlay(
    plan: OverlayPlan,
    *,
    backends: tuple[str, ...] = ("codanna", "cocoindex"),
    codanna_binary: str = "codanna",
    codanna_config: str | pathlib.Path | None = None,
    cocoindex_command: str = sys.executable,
    pg_url: str | None = None,
    base_manifest: str | pathlib.Path | None = None,
) -> PreflightReport:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "worktree_exists": pathlib.Path(plan.worktree).is_dir(),
        "base_commit": plan.base_commit,
        "changed_files": len(plan.indexed_files),
        "codanna_files": len(plan.codanna_files),
        "cocoindex_files": len(plan.cocoindex_files),
        "ignored_changed_files": len(plan.indexed_files) - len(plan.cocoindex_files),
        "tombstones": len(plan.tombstones),
        "base_provenance": False,
    }
    if not checks["worktree_exists"]:
        errors.append(f"worktree does not exist: {plan.worktree}")
    if not plan.indexed_files and not plan.tombstones:
        warnings.append("overlay has no branch or working-tree changes")

    if "codanna" in backends:
        config = pathlib.Path(codanna_config).expanduser() if codanna_config else None
        checks["codanna_binary"] = _command_available(codanna_binary)
        checks["codanna_config"] = str(config) if config else None
        if not checks["codanna_binary"]:
            errors.append(f"Codanna binary is unavailable: {codanna_binary}")
        if config is None or not config.is_file():
            errors.append(f"Codanna config is unavailable: {config}")
        else:
            index_path = _codanna_index_path(config)
            checks["codanna_base_index"] = str(index_path) if index_path else None
            if index_path is None or not index_path.exists():
                warnings.append("Codanna base index is not present; build the base snapshot before overlaying")

    manifest = _read_base_manifest(base_manifest)
    if manifest is not None:
        manifest_commit = manifest.get("commit") or manifest.get("head_commit") or manifest.get("base_commit")
        checks["base_manifest"] = str(base_manifest)
        checks["base_manifest_commit"] = manifest_commit
        checks["base_provenance"] = manifest_commit == plan.base_commit
        if not checks["base_provenance"]:
            errors.append(
                f"base manifest commit {manifest_commit!r} does not match merge-base {plan.base_commit!r}"
            )
    else:
        warnings.append("base index provenance is unverified; do not merge overlay results yet")

    if "cocoindex" in backends:
        checks["cocoindex_command"] = _command_available(cocoindex_command)
        checks["cocoindex_pg_configured"] = bool(
            pg_url or os.environ.get("COCOINDEX_PG_URL")
            or "postgresql://hindsight:hindsight@localhost:5432/hindsight"
        )
        if not checks["cocoindex_command"]:
            errors.append(f"CocoIndex flow command is unavailable: {cocoindex_command}")
        if not checks["cocoindex_pg_configured"]:
            warnings.append("CocoIndex database connectivity was not checked")

    return PreflightReport(not errors, tuple(errors), tuple(warnings), checks)


def write_plan(plan: OverlayPlan, output_dir: str | pathlib.Path) -> pathlib.Path:
    root = pathlib.Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "overlay-plan.json"
    path.write_text(json.dumps(plan.to_dict(), indent=2) + "\n")
    return path


def stage_overlay_files(
    plan: OverlayPlan,
    output_dir: str | pathlib.Path,
    files: tuple[OverlayFile, ...] | None = None,
) -> pathlib.Path:
    """Copy only current changed files into an isolated backend input tree."""
    root = pathlib.Path(output_dir).expanduser().resolve() / "staged" / plan.repository
    root.mkdir(parents=True, exist_ok=True)
    for item in files if files is not None else plan.cocoindex_files:
        source = pathlib.Path(plan.worktree) / pathlib.Path(*item.path.split("/"))
        destination = root / pathlib.Path(*item.path.split("/"))
        if not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return root


def _limited(items: tuple[OverlayFile, ...], max_files: int | None) -> tuple[OverlayFile, ...]:
    return items if max_files is None else items[:max_files]


def _codanna_scope(
    items: tuple[OverlayFile, ...], *, exclude_tests: bool, exclude_generated: bool,
) -> tuple[OverlayFile, ...]:
    def keep(item: OverlayFile) -> bool:
        path = pathlib.PurePosixPath(item.path)
        name = path.name.lower()
        if exclude_tests and (
            any(part.lower() in {"test", "tests", "e2e", "integration"} for part in path.parts)
            or name.endswith(("_test.go", ".test.ts", ".spec.ts"))
        ):
            return False
        if exclude_generated and (
            name.endswith(("_gen.go", ".generated.ts", ".generated.js"))
            or "generated" in name
        ):
            return False
        return True

    return tuple(item for item in items if keep(item))


def _overlay_table_name(plan: OverlayPlan) -> str:
    return f"code_embeddings_overlay_{plan.working_tree_digest[:12]}"


def build_codanna_overlay(
    plan: OverlayPlan,
    output_dir: str | pathlib.Path,
    *,
    binary: str = "codanna",
    timeout: float = 600,
    max_files: int | None = None,
    semantic_search: bool = True,
    exclude_tests: bool = False,
    exclude_generated: bool = False,
) -> OverlayBuildResult:
    import time

    root = pathlib.Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    write_plan(plan, root)
    index_path = root / "codanna-index"
    config_path = root / "codanna-settings.toml"
    selected_files = _limited(
        _codanna_scope(
            plan.codanna_files,
            exclude_tests=exclude_tests,
            exclude_generated=exclude_generated,
        ),
        max_files,
    )
    indexed_paths = [str(pathlib.Path(plan.worktree) / pathlib.Path(*item.path.split("/"))) for item in selected_files]
    config_lines = [
        "version = 1",
        f"index_path = {json.dumps(str(index_path))}",
        f"workspace_root = {json.dumps(plan.worktree)}",
        "",
        "[indexing]",
        "show_progress = false",
        "",
        "[semantic_search]",
        f"enabled = {'true' if semantic_search else 'false'}",
        "model = \"AllMiniLML6V2\"",
        "embedding_threads = 3",
        "",
        "[file_watch]",
        "enabled = false",
    ]
    config_path.write_text("\n".join(config_lines) + "\n")
    command = (binary, "index", "--config", str(config_path), "--no-progress", *indexed_paths)
    started = time.perf_counter()
    if indexed_paths:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
        if completed.returncode != 0:
            raise OverlayPlanError(completed.stderr.strip() or completed.stdout.strip() or "Codanna overlay build failed")
    return OverlayBuildResult(
        backend="codanna",
        overlay_id=plan.overlay_id,
        output_dir=str(root),
        indexed_files=len(selected_files),
        tombstones=len(plan.tombstones),
        elapsed_seconds=round(time.perf_counter() - started, 3),
        command=command,
    )


def build_cocoindex_overlay(
    plan: OverlayPlan,
    output_dir: str | pathlib.Path,
    *,
    pg_url: str | None = None,
    python: str | None = None,
    timeout: float = 900,
    max_files: int | None = None,
) -> OverlayBuildResult:
    import time

    root = pathlib.Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    write_plan(plan, root)
    selected_files = _limited(plan.cocoindex_files, max_files)
    staged_root = stage_overlay_files(plan, root, selected_files)
    table_name = _overlay_table_name(plan)
    command = (
        python or sys.executable,
        "-m",
        "engram.code_overlay_cocoindex",
        "--root",
        str(staged_root),
        "--repo-tag",
        plan.repository,
        "--table",
        table_name,
    )
    env = os.environ.copy()
    if pg_url:
        env["COCOINDEX_PG_URL"] = pg_url
    env["COCOINDEX_DB"] = str(root / "cocoindex.db")
    started = time.perf_counter()
    if selected_files:
        completed = subprocess.run(command, env=env, capture_output=True, text=True, check=False, timeout=timeout)
        if completed.returncode != 0:
            raise OverlayPlanError(completed.stderr.strip() or completed.stdout.strip() or "CocoIndex overlay build failed")
    return OverlayBuildResult(
        backend="cocoindex",
        overlay_id=plan.overlay_id,
        output_dir=str(root),
        indexed_files=len(selected_files),
        tombstones=len(plan.tombstones),
        elapsed_seconds=round(time.perf_counter() - started, 3),
        command=command,
        table_name=table_name,
    )
