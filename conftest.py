"""Shared pytest fixtures for the tests/ suite.

Adds the repo root to sys.path so bare `import correction_gate`,
`import contradiction_resolution`, `import project_scope` etc. work from
tests/, and provides a fixture for loading the hyphenated production scripts
(nightly-learn.py, cocoindex-flows.py) as modules -- they can't be `import`ed
normally because Python identifiers can't contain hyphens. Mirrors the same
importlib pattern already used in review-contradictions.py.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent
SPIKE_DIR = REPO_ROOT / "spike"
HOOKS_DIR = REPO_ROOT / "hooks"
SRC_DIR = REPO_ROOT / "src"
for path in (REPO_ROOT, SPIKE_DIR, HOOKS_DIR, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_hyphenated_module(filename: str, module_name: str) -> ModuleType:
    """Load a hyphenated-filename script (e.g. "nightly-learn.py") as an
    importable module object."""
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def nightly_learn() -> ModuleType:
    return load_hyphenated_module("src/engram/pipeline/nightly_learn.py", "nightly_learn")


@pytest.fixture(scope="session")
def cocoindex_flows() -> ModuleType:
    return load_hyphenated_module("src/engram/flows/kubernaut.py", "cocoindex_flows")


@pytest.fixture(scope="session")
def review_contradictions(cocoindex_flows: ModuleType) -> ModuleType:
    """review-contradictions.py does its own independent importlib exec of
    cocoindex-flows.py internally. CocoIndex registers global ContextKeys
    (e.g. "pg_pool") at module-exec time and raises if the same key is
    registered twice in one process, so exec'ing cocoindex-flows.py a second
    time in the same session (once via the cocoindex_flows fixture above,
    once inside review-contradictions.py's own module code) throws and
    leaves review-contradictions.py's internal `_cf` half-initialized with
    `_HAS_RETAIN=False`. Depending on the cocoindex_flows fixture first
    doesn't avoid this (review-contradictions.py always execs its own copy
    regardless of sys.modules state) -- so after loading, we replace its
    broken `_cf`/`_HAS_RETAIN` with the one canonical, already-working
    module instance instead.
    """
    module = load_hyphenated_module("review-contradictions.py", "review_contradictions")
    module._cf = cocoindex_flows
    module._HAS_RETAIN = True
    return module


@pytest.fixture(scope="session")
def purge_script() -> ModuleType:
    return load_hyphenated_module("purge-out-of-scope-memories.py", "purge_out_of_scope_memories")


@pytest.fixture(scope="session")
def check_rule_sync() -> ModuleType:
    return load_hyphenated_module("check-rule-sync.py", "check_rule_sync")


@pytest.fixture(scope="session")
def engram_cocoindex_flows() -> ModuleType:
    """engram-cocoindex-flows.py (2026-07-15 Engram onboarding). Its
    PG_POOL ContextKey is deliberately named "engram_repo_pg_pool" (not
    "pg_pool" like cocoindex-flows.py's own) precisely so it can be loaded
    into the same process as the cocoindex_flows fixture above without
    CocoIndex's "Context key already used" ValueError -- see the comment
    next to that ContextKey() call for the full rationale.
    """
    return load_hyphenated_module("src/engram/flows/engram.py", "engram_cocoindex_flows")


@pytest.fixture(scope="session")
def koku_cocoindex_flows() -> ModuleType:
    """koku-cocoindex-flows.py (Koku onboarding). Its PG_POOL ContextKey is
    "koku_repo_pg_pool" for the same process-global-collision reason as
    engram_cocoindex_flows's PG_POOL above."""
    return load_hyphenated_module("src/engram/flows/koku.py", "koku_cocoindex_flows")


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
    return load_hyphenated_module("src/engram/pipeline/generate_dashboard.py", "generate_dashboard")


@pytest.fixture(scope="session")
def dcm_cocoindex_flows() -> ModuleType:
    """dcm-cocoindex-flows.py (DCM onboarding). PG_POOL was renamed from the
    generic "pg_pool" to "dcm_repo_pg_pool" (2026-08-03) specifically to
    unblock this fixture -- see the comment next to that ContextKey() call."""
    return load_hyphenated_module("src/engram/flows/dcm.py", "dcm_cocoindex_flows")


@pytest.fixture(scope="session")
def praxis_cocoindex_flows() -> ModuleType:
    """praxis-cocoindex-flows.py (praxis-proxy org onboarding). PG_POOL is
    "praxis_repo_pg_pool" for the same process-global-collision reason as
    the other *_cocoindex_flows fixtures' PG_POOL above."""
    return load_hyphenated_module("src/engram/flows/praxis.py", "praxis_cocoindex_flows")


@pytest.fixture(scope="session")
def cocoindex_search() -> ModuleType:
    """cocoindex-search.py (the kubernaut-family code search MCP server).
    Unlike the *_cocoindex_flows fixtures, this module registers no
    CocoIndex flow/ContextKey at import time (its `import cocoindex.ops.*`
    calls are all deferred into function bodies), so it's safe to load
    into the same session as any/all of those fixtures with no collision
    handling needed."""
    return load_hyphenated_module("src/engram/search/kubernaut.py", "cocoindex_search")
