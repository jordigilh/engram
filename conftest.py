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
for path in (REPO_ROOT, SPIKE_DIR):
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
    return load_hyphenated_module("nightly-learn.py", "nightly_learn")


@pytest.fixture(scope="session")
def cocoindex_flows() -> ModuleType:
    return load_hyphenated_module("cocoindex-flows.py", "cocoindex_flows")


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
