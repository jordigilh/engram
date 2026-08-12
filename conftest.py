"""Shared pytest fixtures for the tests/ suite.

Everything under src/engram/ is a real, `pip install -e .`-installed
package now (see pyproject.toml) and gets imported the plain way --
`import engram.flows.kubernaut`, etc. Python's sys.modules cache means each
of these only ever executes once per test session no matter how many
fixtures/modules import it, which is also what makes it safe for the
*_cocoindex_flows fixtures below to coexist: CocoIndex registers global
ContextKeys (e.g. "pg_pool") at module-exec time and raises if the same key
is registered twice in one process, which import caching prevents (each
project's flow module has its own uniquely-named PG_POOL key besides, as a
second line of defense -- see the comment next to each ContextKey() call).

`load_hyphenated_module()` below still exists for the three genuinely
unpackaged, hyphenated-filename scripts this repo intentionally keeps
outside the engram package (hooks/*.py, check-rule-sync.py -- see
docs/findings/2026-08.md and the package-restructure plan's "explicitly out
of scope" section) -- those can't be `import`ed normally since Python
identifiers can't contain hyphens, and packaging them isn't in scope.
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
def cocoindex_flows() -> ModuleType:
    from engram.flows import kubernaut
    return kubernaut


@pytest.fixture(scope="session")
def review_contradictions(cocoindex_flows: ModuleType) -> ModuleType:
    """review_contradictions.py imports engram.flows.kubernaut itself (as
    `_cf`) the plain way now -- see its own module comment -- so this
    fixture no longer needs to patch anything post-hoc; depending on the
    cocoindex_flows fixture here just documents (and enforces via pytest's
    fixture graph) that both share the one cached kubernaut module."""
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
def engram_cocoindex_flows() -> ModuleType:
    """engram.py (2026-07-15 Engram onboarding). Its PG_POOL ContextKey is
    deliberately named "engram_repo_pg_pool" (not "pg_pool" like
    kubernaut.py's own) precisely so it can be loaded into the same process
    as the cocoindex_flows fixture above without CocoIndex's "Context key
    already used" ValueError -- see the comment next to that ContextKey()
    call for the full rationale.
    """
    from engram.flows import engram as engram_cocoindex_flows
    return engram_cocoindex_flows


@pytest.fixture(scope="session")
def koku_cocoindex_flows() -> ModuleType:
    """koku.py (Koku onboarding). Its PG_POOL ContextKey is
    "koku_repo_pg_pool" for the same process-global-collision reason as
    engram_cocoindex_flows's PG_POOL above."""
    from engram.flows import koku as koku_cocoindex_flows
    return koku_cocoindex_flows


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
def dcm_cocoindex_flows() -> ModuleType:
    """dcm.py (DCM onboarding). PG_POOL was renamed from the generic
    "pg_pool" to "dcm_repo_pg_pool" (2026-08-03) specifically to unblock
    this fixture -- see the comment next to that ContextKey() call."""
    from engram.flows import dcm as dcm_cocoindex_flows
    return dcm_cocoindex_flows


@pytest.fixture(scope="session")
def praxis_cocoindex_flows() -> ModuleType:
    """praxis.py (praxis-proxy org onboarding). PG_POOL is
    "praxis_repo_pg_pool" for the same process-global-collision reason as
    the other *_cocoindex_flows fixtures' PG_POOL above."""
    from engram.flows import praxis as praxis_cocoindex_flows
    return praxis_cocoindex_flows


@pytest.fixture(scope="session")
def cocoindex_search() -> ModuleType:
    """kubernaut.py's code search module (engram.search.kubernaut) -- the
    kubernaut-family code search MCP server. Unlike the *_cocoindex_flows
    fixtures, this module registers no CocoIndex flow/ContextKey at import
    time (its `import cocoindex.ops.*` calls are all deferred into function
    bodies), so it's safe to load into the same session as any/all of those
    fixtures with no collision handling needed."""
    from engram.search import kubernaut as cocoindex_search
    return cocoindex_search
