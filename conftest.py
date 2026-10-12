"""Shared pytest fixtures for the tests/ suite.

Everything under src/engram/ is a real, `pip install -e .`-installed
package now (see pyproject.toml) and gets imported through its package name.

`load_hyphenated_module()` below still exists for the genuinely unpackaged,
hyphenated-filename scripts this repo intentionally keeps outside the engram
package (hooks/*.py, check-rule-sync.py -- see docs/findings/2026-08.md and
the package-restructure plan's "explicitly out of scope" section).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent
SPIKE_DIR = REPO_ROOT / "spike"
HOOKS_DIR = REPO_ROOT / "hooks"
SRC_DIR = REPO_ROOT / "src"
# Keep module-import tests hermetic. Production uses ~/.engram/projects.toml;
# the fixture contains only synthetic paths and the repository mappings needed
# to preserve the existing regression coverage.
os.environ.setdefault("ENGRAM_PROJECTS_CONFIG", str(REPO_ROOT / "tests/fixtures/projects.toml"))
for path in (REPO_ROOT, SPIKE_DIR, HOOKS_DIR, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_hyphenated_module(filename: str, module_name: str) -> ModuleType:
    """Load a hyphenated-filename script (e.g. "check-rule-sync.py") that's
    deliberately not part of the engram package as an importable module
    object."""
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def nightly_learn() -> ModuleType:
    from engram.pipeline import nightly_learn
    return nightly_learn


@pytest.fixture(scope="session")
def review_contradictions() -> ModuleType:
    """The contradiction reviewer uses the generic retain helper."""
    from engram.maintenance import review_contradictions
    return review_contradictions


@pytest.fixture(scope="session")
def purge_script() -> ModuleType:
    from engram.maintenance import purge_out_of_scope_memories
    return purge_out_of_scope_memories


@pytest.fixture(scope="session")
def check_rule_sync() -> ModuleType:
    return load_hyphenated_module("check-rule-sync.py", "check_rule_sync")


@pytest.fixture(scope="session")
def post_plan_hindsight_check() -> ModuleType:
    """hooks/post-plan-hindsight-check.py -- the preToolUse enforcer half of
    the Deterministic Correction Enforcement hook pair (see
    docs/findings/2026-08.md)."""
    return load_hyphenated_module("hooks/post-plan-hindsight-check.py", "post_plan_hindsight_check")


@pytest.fixture(scope="session")
def post_plan_checklist_reminder() -> ModuleType:
    """hooks/post-plan-checklist-reminder.py -- the postToolUse reminder,
    third member of the Deterministic Correction Enforcement hook family
    (see the "Hook-delivered PR review checklist" plan)."""
    return load_hyphenated_module("hooks/post-plan-checklist-reminder.py", "post_plan_checklist_reminder")


@pytest.fixture(scope="session")
def generate_dashboard() -> ModuleType:
    from engram.pipeline import generate_dashboard
    return generate_dashboard


@pytest.fixture(scope="session")
def ingest_docs() -> ModuleType:
    from engram.pipeline import ingest_docs
    return ingest_docs


@pytest.fixture(scope="session")
def hindsight_proxy() -> ModuleType:
    from engram.pipeline import hindsight_proxy
    return hindsight_proxy


@pytest.fixture(scope="session")
def serena_multiplex() -> ModuleType:
    from engram.pipeline import serena_multiplex
    return serena_multiplex


@pytest.fixture(scope="session")
def engram_gateway() -> ModuleType:
    from engram.pipeline import engram_gateway
    return engram_gateway
