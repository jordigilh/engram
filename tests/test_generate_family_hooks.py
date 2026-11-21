"""Generator coverage for the restart/invalidate hook variants (issue #204)."""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GENERATOR = REPO / "git-hooks/generate-hooks.sh"


def _run(tmp_path: Path, vars_text: str):
    config = tmp_path / "family.vars"
    config.write_text(vars_text)
    out = tmp_path / "out"
    return subprocess.run(
        [str(GENERATOR), str(config), str(out)],
        capture_output=True,
        text=True,
        check=False,
    ), out


def test_invalidate_variant_renders_suffixed_hooks(tmp_path):
    result, out = _run(
        tmp_path,
        "\n".join(
            [
                'FAMILY_NAME="test-family"',
                'HOOK_VARIANT="invalidate"',
                'INVALIDATION_URL="http://127.0.0.1:8893/admin/invalidate"',
                'INVALIDATION_TOKEN_FILE="/tmp/token"',
                'INVALIDATION_SPOOL_DIR="/tmp/spool"',
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    assert (out / "_invalidate-test-family-workspaces.sh").is_file()
    assert (out / "serena_barrier.py").is_file()
    for hook in ("post-checkout", "post-merge", "post-rewrite", "reference-transaction"):
        path = out / f"{hook}-test-family-invalidate-mcp.sh"
        assert path.is_file()
        assert path.read_text().startswith("#!/bin/sh")


def test_invalidate_variant_requires_deployment_keys(tmp_path):
    result, _ = _run(
        tmp_path,
        'FAMILY_NAME="test-family"\nHOOK_VARIANT="invalidate"\n',
    )
    assert result.returncode != 0
    assert "INVALIDATION_URL" in (result.stderr + result.stdout)


def test_restart_variant_still_renders_legacy_names(tmp_path):
    result, out = _run(
        tmp_path,
        'FAMILY_NAME="test-family"\nDAEMON_LABELS="io.vectorize.serena.test"\n',
    )
    assert result.returncode == 0, result.stderr
    assert (out / "_restart-test-family-daemons.sh").is_file()
    assert (out / "post-checkout-test-family-mcp.sh").is_file()


def test_rendered_hook_uses_same_barrier_key_as_python(tmp_path):
    import hashlib
    import os
    import subprocess

    result, out = _run(
        tmp_path,
        "\n".join(
            [
                'FAMILY_NAME="test-family"',
                'HOOK_VARIANT="invalidate"',
                'INVALIDATION_URL="http://127.0.0.1:9/admin/invalidate"',
                f'INVALIDATION_TOKEN_FILE="{tmp_path / "token"}"',
                f'INVALIDATION_SPOOL_DIR="{tmp_path / "spool"}"',
            ]
        ),
    )
    assert result.returncode == 0, result.stderr

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    env = os.environ.copy()
    env["INVALIDATION_URL"] = "http://127.0.0.1:9/admin/invalidate"
    env["INVALIDATION_TOKEN_FILE"] = str(tmp_path / "token")
    env["INVALIDATION_SPOOL_DIR"] = str(tmp_path / "spool")
    script = out / "_invalidate-test-family-workspaces.sh"
    begin = subprocess.run(
        ["sh", str(script), "begin"], cwd=repo, env=env, check=False
    )
    assert begin.returncode == 0

    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    lock = tmp_path / "spool" / ".barrier" / f"{key}.lock"
    assert lock.is_file()
    subprocess.run(
        ["sh", str(script), "worktree_completed"], cwd=repo, env=env, check=False
    )
