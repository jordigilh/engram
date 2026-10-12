"""Tests for generic branch/worktree overlay planning and preflight."""

from __future__ import annotations

import subprocess

from engram.code_overlay import (
    build_codanna_overlay,
    build_overlay_plan,
    preflight_overlay,
    stage_overlay_files,
)


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Overlay Test")
    (tmp_path / "base.go").write_text("package base\n\nfunc Base() {}\n")
    (tmp_path / "removed.go").write_text("package removed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "switch", "-c", "feature/overlay")
    return tmp_path


def test_plan_includes_committed_dirty_and_untracked_changes(tmp_path):
    root = _repo(tmp_path)
    (root / "branch.go").write_text("package branch\n")
    _git(root, "add", "branch.go")
    _git(root, "commit", "-m", "branch change")
    (root / "base.go").write_text("package base\n\nfunc Changed() {}\n")
    (root / "removed.go").unlink()
    (root / "untracked.go").write_text("package untracked\n")

    plan = build_overlay_plan(root, base_ref="main", repository="kubernaut")
    by_path = {item.path: item for item in plan.files}

    assert plan.branch == "feature/overlay"
    assert plan.base_commit
    assert plan.includes_uncommitted is True
    assert by_path["branch.go"].status == "added"
    assert by_path["base.go"].status == "modified"
    assert by_path["removed.go"].status == "deleted"
    assert by_path["untracked.go"].status == "untracked"
    assert set(item.path for item in plan.indexed_files) == {"base.go", "branch.go", "untracked.go"}
    assert len(plan.working_tree_digest) == 64


def test_plan_tracks_rename_tombstone(tmp_path):
    root = _repo(tmp_path)
    _git(root, "mv", "base.go", "renamed.go")
    _git(root, "commit", "-m", "rename")

    plan = build_overlay_plan(root, base_ref="main")
    by_path = {item.path: item for item in plan.files}

    assert by_path["renamed.go"].status == "renamed"
    assert by_path["renamed.go"].old_path == "base.go"
    assert by_path["base.go"].status == "deleted"


def test_preflight_reports_backend_requirements(tmp_path):
    root = _repo(tmp_path)
    (root / "changed.go").write_text("package changed\n")
    config = root / "settings.toml"
    index = root / "base-index"
    index.mkdir()
    config.write_text(f'index_path = "{index}"\n')
    plan = build_overlay_plan(root, base_ref="main")

    report = preflight_overlay(
        plan,
        codanna_binary="/bin/sh",
        codanna_config=config,
        cocoindex_command="/bin/sh",
        pg_url="postgresql://example",
    )

    assert report.ok is True
    assert report.errors == ()
    assert report.checks["codanna_base_index"] == str(index)
    assert report.checks["base_provenance"] is False
    assert any("base index provenance" in warning for warning in report.warnings)


def test_stager_copies_only_indexed_files(tmp_path):
    root = _repo(tmp_path)
    (root / "changed.go").write_text("package changed\n")
    (root / ".git" / "info" / "exclude").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("not part of the plan\n")
    plan = build_overlay_plan(root, base_ref="main")
    staged = stage_overlay_files(plan, tmp_path / "output")

    assert (staged / "changed.go").read_text() == "package changed\n"
    assert not (staged / "ignored.txt").exists()


def test_codanna_overlay_can_disable_semantic_embeddings(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (root / "changed.go").write_text("package changed\n")
    plan = build_overlay_plan(root, base_ref="main")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("engram.code_overlay.subprocess.run", fake_run)
    build_codanna_overlay(plan, tmp_path / "overlay", semantic_search=False)
    config = (tmp_path / "overlay" / "codanna-settings.toml").read_text()

    assert "[semantic_search]" in config
    assert "enabled = false" in config
    assert "indexed_paths" not in config
    assert calls


def test_codanna_production_scope_excludes_tests_and_generated_files(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    for path in ("changed.go", "changed_test.go", "api_gen.go"):
        (root / path).write_text("package changed\n")
    plan = build_overlay_plan(root, base_ref="main")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("engram.code_overlay.subprocess.run", fake_run)
    result = build_codanna_overlay(
        plan,
        tmp_path / "overlay",
        semantic_search=False,
        exclude_tests=True,
        exclude_generated=True,
    )

    assert result.indexed_files == 1
    assert result.command[-1].endswith("changed.go")
